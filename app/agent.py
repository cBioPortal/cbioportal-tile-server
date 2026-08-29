"""Research-support assistant for the WSI viewer.

The assistant can inspect a caller-supplied viewport and propose reversible
viewer or annotation actions.  It never writes annotations directly: every
proposal is persisted as ``pending`` and must be approved by the caller
before the frontend applies it through the existing annotation API.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import os
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import aiosqlite
import asyncpg
from agents import Agent, ModelSettings, RunContextWrapper, Runner, function_tool
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .auth import scoped_user_dependency
from .config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])
_AGENT_READ_USER = Depends(scoped_user_dependency({"annotations:read"}))
_AGENT_READ_WRITE_USER = Depends(
    scoped_user_dependency({"annotations:read", "annotations:write"})
)

_db_path = ""
_db_url = ""
_rate_lock = asyncio.Lock()
_rate_windows: defaultdict[str, deque[float]] = defaultdict(deque)


class Point(BaseModel):
    x: float = Field(ge=0, le=1000)
    y: float = Field(ge=0, le=1000)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ViewportContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_data_url: str | None = Field(default=None, max_length=2_800_000)
    image_width: int | None = Field(default=None, ge=1, le=1600)
    image_height: int | None = Field(default=None, ge=1, le=1600)
    image_transform: list[float] | None = Field(
        default=None, min_length=6, max_length=6
    )
    slide_width: int = Field(ge=1, le=2_000_000)
    slide_height: int = Field(ge=1, le=2_000_000)
    center_x: float | None = Field(default=None, ge=0)
    center_y: float | None = Field(default=None, ge=0)
    zoom: float | None = Field(default=None, gt=0)


class AgentContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    study_id: str = Field(min_length=1, max_length=200)
    patient_id: str = Field(min_length=1, max_length=200)
    sample_id: str | None = Field(default=None, max_length=200)
    slide_id: str = Field(min_length=1, max_length=200)
    stain_name: str | None = Field(default=None, max_length=100)
    match_level: str | None = Field(default=None, max_length=100)
    filters: dict[str, Any] = Field(default_factory=dict)
    slide_metadata: dict[str, Any] = Field(default_factory=dict)
    patient_context: dict[str, Any] = Field(default_factory=dict)
    existing_annotations: list[dict[str, Any]] = Field(
        default_factory=list, max_length=100
    )
    viewport: ViewportContext


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)
    context: AgentContext


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    action_type: Literal[
        "create_annotation",
        "update_annotation",
        "delete_annotation",
        "viewer_action",
    ]
    study_id: str
    slide_id: str
    payload: dict[str, Any]
    status: Literal["pending", "approved", "rejected", "completed", "failed", "expired"]
    created_at: str
    decided_at: str | None = None
    outcome: dict[str, Any] | None = None


class ActionOutcome(BaseModel):
    success: bool
    detail: str = Field(default="", max_length=1000)


@dataclass
class AgentRunContext:
    user_sub: str
    session_id: str
    context: AgentContext
    proposal_ids: list[str] = field(default_factory=list)


def _settings_db_url() -> str:
    return getattr(settings, "annotation_database_url", "")


def _storage_kind() -> str:
    return "postgres" if (_db_url or _settings_db_url()) else "sqlite"


def _get_db_path() -> str:
    return _db_path or settings.annotation_db_path


def _get_db_url() -> str:
    return _db_url or _settings_db_url()


async def _apply_sqlite_pragmas(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA busy_timeout = 5000")


async def _init_sqlite(path: str) -> None:
    async with aiosqlite.connect(path) as db:
        await _apply_sqlite_pragmas(db)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_actions (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_sub TEXT NOT NULL,
                study_id TEXT NOT NULL,
                slide_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                outcome_json TEXT
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_actions_session "
            "ON agent_actions(session_id, user_sub)"
        )
        await db.commit()


async def _init_postgres(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_actions (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_sub TEXT NOT NULL,
                study_id TEXT NOT NULL,
                slide_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
                decided_at TIMESTAMPTZ,
                outcome_json TEXT
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_actions_session "
            "ON agent_actions(session_id, user_sub)"
        )
    finally:
        await conn.close()


async def init_db(db_path: str | None = None, db_url: str | None = None) -> None:
    global _db_path, _db_url
    _db_path = db_path or settings.annotation_db_path
    _db_url = db_url or _settings_db_url()
    if _get_db_url():
        await _init_postgres(_get_db_url())
    else:
        await _init_sqlite(_get_db_path())


def _decode_json(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _row_to_action(row: Any) -> AgentAction:
    def value(name: str) -> Any:
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            return getattr(row, name)

    return AgentAction(
        id=value("id"),
        session_id=value("session_id"),
        action_type=value("action_type"),
        study_id=value("study_id"),
        slide_id=value("slide_id"),
        payload=_decode_json(value("payload_json"), {}),
        status=value("status"),
        created_at=str(value("created_at")),
        decided_at=str(value("decided_at")) if value("decided_at") else None,
        outcome=_decode_json(value("outcome_json")) if value("outcome_json") else None,
    )


async def _insert_action(
    run_context: AgentRunContext,
    action_type: str,
    payload: dict[str, Any],
) -> AgentAction:
    action_id = str(uuid.uuid4())
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload_json = json.dumps(payload, separators=(",", ":"))
    if _storage_kind() == "postgres":
        conn = await asyncpg.connect(_get_db_url())
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_actions
                    (id, session_id, user_sub, study_id, slide_id, action_type,
                     payload_json, status, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8)
                RETURNING id, session_id, action_type, study_id, slide_id,
                          payload_json, status, created_at, decided_at, outcome_json
                """,
                action_id,
                run_context.session_id,
                run_context.user_sub,
                run_context.context.study_id,
                run_context.context.slide_id,
                action_type,
                payload_json,
                created_at,
            )
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(_get_db_path()) as db:
            await _apply_sqlite_pragmas(db)
            await db.execute(
                """
                INSERT INTO agent_actions
                    (id, session_id, user_sub, study_id, slide_id, action_type,
                     payload_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    action_id,
                    run_context.session_id,
                    run_context.user_sub,
                    run_context.context.study_id,
                    run_context.context.slide_id,
                    action_type,
                    payload_json,
                    created_at,
                ),
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, session_id, action_type, study_id, slide_id, "
                "payload_json, status, created_at, decided_at, outcome_json "
                "FROM agent_actions WHERE id = ?",
                (action_id,),
            )
            row = await cursor.fetchone()
    action = _row_to_action(row)
    run_context.proposal_ids.append(action.id)
    return action


async def _get_action(action_id: str, user_sub: str) -> AgentAction | None:
    columns = (
        "id, session_id, action_type, study_id, slide_id, payload_json, status, "
        "created_at, decided_at, outcome_json"
    )
    if _storage_kind() == "postgres":
        conn = await asyncpg.connect(_get_db_url())
        try:
            row = await conn.fetchrow(
                f"SELECT {columns} FROM agent_actions WHERE id = $1 AND user_sub = $2",
                action_id,
                user_sub,
            )
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(_get_db_path()) as db:
            await _apply_sqlite_pragmas(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT {columns} FROM agent_actions WHERE id = ? AND user_sub = ?",
                (action_id, user_sub),
            )
            row = await cursor.fetchone()
    return _row_to_action(row) if row else None


async def _list_actions(
    session_id: str, user_sub: str, study_id: str
) -> list[AgentAction]:
    columns = (
        "id, session_id, action_type, study_id, slide_id, payload_json, status, "
        "created_at, decided_at, outcome_json"
    )
    if _storage_kind() == "postgres":
        conn = await asyncpg.connect(_get_db_url())
        try:
            rows = await conn.fetch(
                f"SELECT {columns} FROM agent_actions "
                "WHERE session_id = $1 AND user_sub = $2 AND study_id = $3 "
                "ORDER BY created_at ASC",
                session_id,
                user_sub,
                study_id,
            )
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(_get_db_path()) as db:
            await _apply_sqlite_pragmas(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT {columns} FROM agent_actions "
                "WHERE session_id = ? AND user_sub = ? AND study_id = ? "
                "ORDER BY created_at ASC",
                (session_id, user_sub, study_id),
            )
            rows = await cursor.fetchall()
    return [_row_to_action(row) for row in rows]


async def _change_action_status(
    action_id: str,
    user_sub: str,
    expected_status: str,
    new_status: str,
    outcome: dict[str, Any] | None = None,
) -> AgentAction | None:
    decided_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    outcome_json = json.dumps(outcome) if outcome is not None else None
    if _storage_kind() == "postgres":
        conn = await asyncpg.connect(_get_db_url())
        try:
            row = await conn.fetchrow(
                """
                UPDATE agent_actions
                SET status = $1, decided_at = $2, outcome_json = $3
                WHERE id = $4 AND user_sub = $5 AND status = $6
                RETURNING id, session_id, action_type, study_id, slide_id,
                          payload_json, status, created_at, decided_at, outcome_json
                """,
                new_status,
                decided_at,
                outcome_json,
                action_id,
                user_sub,
                expected_status,
            )
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(_get_db_path()) as db:
            await _apply_sqlite_pragmas(db)
            cursor = await db.execute(
                """
                UPDATE agent_actions
                SET status = ?, decided_at = ?, outcome_json = ?
                WHERE id = ? AND user_sub = ? AND status = ?
                """,
                (
                    new_status,
                    decided_at,
                    outcome_json,
                    action_id,
                    user_sub,
                    expected_status,
                ),
            )
            await db.commit()
            if cursor.rowcount != 1:
                return None
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, session_id, action_type, study_id, slide_id, "
                "payload_json, status, created_at, decided_at, outcome_json "
                "FROM agent_actions WHERE id = ?",
                (action_id,),
            )
            row = await cursor.fetchone()
    return _row_to_action(row) if row else None


def _read_api_key_file() -> str | None:
    configured_path = getattr(settings, "agent_api_key_file", "")
    if not configured_path:
        return None
    try:
        value = (
            Path(configured_path)
            .expanduser()
            .read_text(encoding="utf-8")
            .splitlines()[0]
            .strip()
        )
    except (OSError, IndexError):
        return None
    return value if value.startswith("sk-") else None


def _ensure_openai_credentials() -> bool:
    if os.environ.get("OPENAI_API_KEY", "").startswith("sk-"):
        return True
    value = _read_api_key_file()
    if value:
        os.environ["OPENAI_API_KEY"] = value
        return True
    return False


def _validate_context(context: AgentContext) -> None:
    if context.viewport.image_data_url:
        data_url = context.viewport.image_data_url
        if not data_url.startswith("data:image/jpeg;base64,"):
            raise HTTPException(
                status_code=422, detail="Viewport image must be a JPEG data URL"
            )
        encoded = data_url.split(",", 1)[1]
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(
                status_code=422, detail="Viewport image is not valid base64"
            ) from exc
        if len(image_bytes) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Viewport image exceeds 2 MiB")
    context_bytes = len(
        json.dumps(context.patient_context, separators=(",", ":")).encode()
    )
    if context_bytes > 64 * 1024:
        raise HTTPException(status_code=413, detail="Patient context exceeds 64 KiB")
    structured_context_bytes = len(
        json.dumps(
            context.model_dump(exclude={"viewport": {"image_data_url"}}),
            separators=(",", ":"),
        ).encode()
    )
    if structured_context_bytes > 256 * 1024:
        raise HTTPException(status_code=413, detail="Agent context exceeds 256 KiB")
    if context.viewport.image_data_url and (
        context.viewport.image_width is None or context.viewport.image_height is None
    ):
        raise HTTPException(
            status_code=422, detail="Viewport dimensions are required with an image"
        )


async def _check_rate_limit(user_sub: str) -> None:
    now = time.monotonic()
    cutoff = now - 60
    async with _rate_lock:
        window = _rate_windows[user_sub]
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= max(1, settings.agent_rate_limit_per_minute):
            raise HTTPException(
                status_code=429,
                detail="Agent rate limit exceeded",
                headers={"Retry-After": "60"},
            )
        window.append(now)


def _tool_context(wrapper: RunContextWrapper[AgentRunContext]) -> AgentRunContext:
    return wrapper.context


def _safe_rationale(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("A rationale is required for every proposal")
    return value[:2000]


def _context_snapshot(context: AgentContext) -> dict[str, Any]:
    return {
        "study_id": context.study_id,
        "patient_id": context.patient_id,
        "sample_id": context.sample_id,
        "slide_id": context.slide_id,
        "stain_name": context.stain_name,
        "match_level": context.match_level,
        "filters": context.filters,
        "slide_metadata": context.slide_metadata,
        "viewport": context.viewport.model_dump(exclude={"image_data_url"}),
    }


@function_tool
async def propose_annotation(
    context: RunContextWrapper[AgentRunContext],
    geometry_type: Literal["rectangle", "polygon"],
    points: list[Point],
    label: str,
    layer_name: str,
    color: str,
    confidence: float,
    rationale: str,
) -> str:
    """Propose a coarse normalized rectangle or polygon for user review."""
    run_context = _tool_context(context)
    if len(points) < (3 if geometry_type == "polygon" else 2) or len(points) > 100:
        raise ValueError("Annotation geometry has an invalid number of points")
    if not label.strip() or len(label.strip()) > 200:
        raise ValueError("Annotation label must contain 1-200 characters")
    if not layer_name.strip() or len(layer_name.strip()) > 100:
        raise ValueError("Annotation layer must contain 1-100 characters")
    if not color.startswith("#") or len(color) not in {4, 7, 9}:
        raise ValueError("Annotation color must be a hex color")
    if not 0 <= confidence <= 1:
        raise ValueError("Annotation confidence must be between 0 and 1")
    action = await _insert_action(
        run_context,
        "create_annotation",
        {
            "geometry_type": geometry_type,
            "points": [point.model_dump() for point in points],
            "label": label.strip(),
            "layer_name": layer_name.strip(),
            "color": color,
            "confidence": confidence,
            "rationale": _safe_rationale(rationale),
            "context": _context_snapshot(run_context.context),
        },
    )
    return json.dumps({"proposal_id": action.id, "status": action.status})


@function_tool
async def propose_annotation_update(
    context: RunContextWrapper[AgentRunContext],
    annotation_id: str,
    version: int,
    label: str | None = None,
    layer_name: str | None = None,
    color: str | None = None,
    comment: str | None = None,
    rationale: str = "",
) -> str:
    """Propose a metadata update using optimistic annotation concurrency."""
    run_context = _tool_context(context)
    if not annotation_id.strip() or version < 1:
        raise ValueError("Annotation id and positive version are required")
    changes = {
        key: value.strip() if isinstance(value, str) else value
        for key, value in {
            "label": label,
            "layer_name": layer_name,
            "color": color,
            "comment": comment,
        }.items()
        if value is not None
    }
    if not changes:
        raise ValueError("At least one annotation field must be changed")
    action = await _insert_action(
        run_context,
        "update_annotation",
        {
            "annotation_id": annotation_id.strip(),
            "version": version,
            "changes": changes,
            "rationale": _safe_rationale(rationale),
            "context": _context_snapshot(run_context.context),
        },
    )
    return json.dumps({"proposal_id": action.id, "status": action.status})


@function_tool
async def propose_annotation_delete(
    context: RunContextWrapper[AgentRunContext],
    annotation_id: str,
    version: int,
    rationale: str,
) -> str:
    """Propose deleting an annotation after explicit user approval."""
    run_context = _tool_context(context)
    if not annotation_id.strip() or version < 1:
        raise ValueError("Annotation id and positive version are required")
    action = await _insert_action(
        run_context,
        "delete_annotation",
        {
            "annotation_id": annotation_id.strip(),
            "version": version,
            "rationale": _safe_rationale(rationale),
            "context": _context_snapshot(run_context.context),
        },
    )
    return json.dumps({"proposal_id": action.id, "status": action.status})


@function_tool
async def propose_viewer_action(
    context: RunContextWrapper[AgentRunContext],
    action: Literal["select_slide", "set_filters", "go_to_coordinates", "zoom"],
    parameters_json: str,
    rationale: str,
) -> str:
    """Propose a reviewable navigation or filter change."""
    run_context = _tool_context(context)
    try:
        parameters = json.loads(parameters_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Viewer action parameters must be a JSON object") from exc
    if not isinstance(parameters, dict):
        raise TypeError("Viewer action parameters must be a JSON object")
    if len(json.dumps(parameters, separators=(",", ":"))) > 4000:
        raise ValueError("Viewer action parameters are too large")
    if not parameters:
        raise ValueError("Viewer action parameters are required")
    if action == "select_slide":
        slide_id = parameters.get("slide_id", parameters.get("slideId"))
        if not isinstance(slide_id, str) or not slide_id.strip():
            raise ValueError("select_slide requires a slide_id")
    elif action == "set_filters":
        timepoint_days = parameters.get("timepoint_days")
        valid_filter = (
            parameters.get("stain_filter") in {"all", "hne", "ihc"}
            or parameters.get("match_filter") in {"all", "part", "block", "unmatched"}
            or (
                isinstance(timepoint_days, (int, float))
                and not isinstance(timepoint_days, bool)
                and math.isfinite(timepoint_days)
            )
        )
        if not valid_filter:
            raise ValueError("set_filters requires a supported filter")
    elif action == "go_to_coordinates":
        if not all(
            isinstance(parameters.get(name), (int, float))
            and not isinstance(parameters.get(name), bool)
            and math.isfinite(parameters[name])
            for name in ("x", "y")
        ):
            raise ValueError("go_to_coordinates requires finite x and y")
    elif action == "zoom":
        zoom = parameters.get("zoom")
        if (
            not isinstance(zoom, (int, float))
            or isinstance(zoom, bool)
            or not math.isfinite(zoom)
            or zoom <= 0
        ):
            raise ValueError("zoom requires a positive finite zoom")
    proposal = {
        "action": action,
        "parameters": parameters,
        "rationale": _safe_rationale(rationale),
        "context": _context_snapshot(run_context.context),
    }
    created = await _insert_action(run_context, "viewer_action", proposal)
    return json.dumps({"proposal_id": created.id, "status": created.status})


def _agent_instructions() -> str:
    return """You are the cBioPortal WSI research-support assistant.

Use the supplied current viewport image and structured portal context to help
the researcher inspect the slide, summarize visible findings, navigate, and
organize annotations.  You may describe visual patterns, but do not diagnose,
recommend treatment, assign clinical significance, or state a clinical
conclusion.  Use calibrated language and state uncertainty when the image or
context is insufficient.

Every state-changing request requires a proposal tool call.  Never claim that
an annotation, navigation, or filter change has been applied: tools only
create pending proposals and the user must approve them in the UI.  Annotation
coordinates must be normalized to the supplied slide image as x/y values from
0 through 1000.  Propose only coarse rectangles or polygons and include a
short rationale and confidence.  Do not infer or invent patient facts.
"""


def _build_agent() -> Agent:
    return Agent(
        name="WSI research assistant",
        instructions=_agent_instructions(),
        model=settings.agent_model,
        model_settings=ModelSettings(
            store=False,
            max_tokens=1500,
            timeout=settings.agent_timeout_seconds,
        ),
        tools=[
            propose_annotation,
            propose_annotation_update,
            propose_annotation_delete,
            propose_viewer_action,
        ],
    )


def _agent_input(request: ChatRequest) -> list[dict[str, Any]]:
    context = request.context.model_dump(exclude={"viewport": {"image_data_url"}})
    history = [message.model_dump() for message in request.history]
    prompt = json.dumps(
        {
            "current_context": context,
            "conversation": history,
            "current_request": request.message,
        },
        separators=(",", ":"),
    )
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    image_url = request.context.viewport.image_data_url
    if image_url:
        content.append({"type": "input_image", "image_url": image_url, "detail": "low"})
    return [{"role": "user", "content": content}]


def _sse(event: str, payload: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def _stream_agent(request: ChatRequest, user_sub: str):
    run_context = AgentRunContext(
        user_sub=user_sub,
        session_id=request.session_id,
        context=request.context,
    )
    try:
        result = Runner.run_streamed(
            _build_agent(),
            _agent_input(request),
            context=run_context,
            max_turns=6,
        )
        async for event in result.stream_events():
            if event.type == "raw_response_event":
                data = event.data
                if getattr(data, "type", "") == "response.output_text.delta":
                    yield _sse("message.delta", {"text": data.delta})
            elif event.type == "run_item_stream_event":
                item = getattr(event, "item", None)
                raw = getattr(item, "raw_item", None)
                tool_name = raw.get("name", "") if isinstance(raw, dict) else ""
                if tool_name:
                    yield _sse("tool.called", {"name": tool_name})
        for proposal_id in run_context.proposal_ids:
            proposal = await _get_action(proposal_id, user_sub)
            if proposal:
                yield _sse("proposal", proposal.model_dump())
        yield _sse("complete", {"proposal_ids": run_context.proposal_ids})
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("WSI agent run failed")
        yield _sse(
            "error",
            {"message": "The research assistant could not complete this request."},
        )


def _require_study(user: dict[str, Any], study_id: str) -> None:
    if user.get("study_id") and user["study_id"] != study_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token study scope does not match request",
        )


@router.get("/health")
async def agent_health() -> dict[str, Any]:
    return {
        "enabled": bool(settings.agent_enabled),
        "configured": bool(
            os.environ.get("OPENAI_API_KEY", "").startswith("sk-")
            or _read_api_key_file()
        ),
        "model": settings.agent_model,
    }


@router.post("/chat")
async def agent_chat(
    request: ChatRequest,
    user: dict[str, Any] = _AGENT_READ_USER,
) -> StreamingResponse:
    if not settings.agent_enabled:
        raise HTTPException(
            status_code=404, detail="WSI research assistant is disabled"
        )
    if not _ensure_openai_credentials():
        raise HTTPException(
            status_code=503, detail="WSI research assistant is not configured"
        )
    _require_study(user, request.context.study_id)
    _validate_context(request.context)
    await _check_rate_limit(user["sub"])
    return StreamingResponse(
        _stream_agent(request, user["sub"]),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/actions", response_model=list[AgentAction])
async def list_agent_actions(
    session_id: str = Query(..., min_length=1, max_length=128),
    study_id: str = Query(..., min_length=1, max_length=200),
    user: dict[str, Any] = _AGENT_READ_USER,
) -> list[AgentAction]:
    _require_study(user, study_id)
    return await _list_actions(session_id, user["sub"], study_id)


async def _transition_action(
    action_id: str,
    user: dict[str, Any],
    expected: str,
    new_status: str,
    outcome: dict[str, Any] | None = None,
) -> AgentAction:
    action = await _get_action(action_id, user["sub"])
    if not action:
        raise HTTPException(status_code=404, detail="Agent proposal not found")
    _require_study(user, action.study_id)
    updated = await _change_action_status(
        action_id,
        user["sub"],
        expected,
        new_status,
        outcome,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Proposal is no longer {expected}",
        )
    return updated


@router.post("/actions/{action_id}/apply", response_model=AgentAction)
async def apply_agent_action(
    action_id: str,
    user: dict[str, Any] = _AGENT_READ_WRITE_USER,
) -> AgentAction:
    return await _transition_action(action_id, user, "pending", "approved")


@router.post("/actions/{action_id}/reject", response_model=AgentAction)
async def reject_agent_action(
    action_id: str,
    user: dict[str, Any] = _AGENT_READ_USER,
) -> AgentAction:
    return await _transition_action(action_id, user, "pending", "rejected")


@router.post("/actions/{action_id}/complete", response_model=AgentAction)
async def complete_agent_action(
    action_id: str,
    outcome: ActionOutcome,
    user: dict[str, Any] = _AGENT_READ_WRITE_USER,
) -> AgentAction:
    return await _transition_action(
        action_id,
        user,
        "approved",
        "completed" if outcome.success else "failed",
        outcome.model_dump(),
    )
