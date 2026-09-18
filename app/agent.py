"""Research-support assistant for the WSI viewer.

The assistant can inspect a caller-supplied viewport and propose reversible
viewer or annotation actions.  It never writes annotations directly: every
proposal is persisted as ``pending`` and must be approved by the caller.
Annotation approval is committed atomically with its proposal state.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
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
import boto3
from botocore.exceptions import ClientError
try:
    from agents import Agent, ModelSettings, RunContextWrapper, Runner, function_tool
except ModuleNotFoundError:  # Bedrock deployments do not need the legacy SDK.
    class RunContextWrapper:  # type: ignore[no-redef]
        def __init__(self, context: Any):
            self.context = context

    class ModelSettings:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any):
            self.settings = kwargs

    class Agent:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any):
            self.settings = kwargs

    class Runner:  # type: ignore[no-redef]
        @staticmethod
        def run_streamed(*args: Any, **kwargs: Any):
            raise RuntimeError("The OpenAI agent provider is not installed")

    def function_tool(function):  # type: ignore[no-redef]
        return function
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import annotations as annotation_store
from .auth import scoped_user_dependency
from .annotation_db import connection, migrate_agent, open_pool
from .config import settings
from .research import _load_manifest, _similar_slides_for_model, search_regions_for_agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])
_AGENT_READ_USER = Depends(
    scoped_user_dependency({"agent:chat", "research:read", "annotations:read"})
)
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
    # These values bind a proposal to the exact displayed slide source and
    # viewer frame used to create its image/transform pair.
    source_fingerprint: str | None = Field(default=None, min_length=8, max_length=512)
    capture_id: str | None = Field(default=None, min_length=1, max_length=128)
    viewer_generation: int | None = Field(default=None, ge=0)


class EmbeddingContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["quiltnet"]
    scope: Literal["study"]
    slide_ids: list[str] = Field(default_factory=list, max_length=200)


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
    embedding_context: EmbeddingContext | None = None
    viewport: ViewportContext


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    request_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_.:-]{1,128}$"
    )
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)
    context: AgentContext


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    action_type: Literal[
        "create_annotation",
        "annotation_batch",
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


class CommitAnnotationsRequest(BaseModel):
    source_fingerprint: str = Field(min_length=8, max_length=512)
    viewer_generation: int | None = Field(default=None, ge=0)
    slide_id: str = Field(min_length=1, max_length=200)


@dataclass
class AgentRunContext:
    user_sub: str
    session_id: str
    context: AgentContext
    request_id: str | None = None
    proposal_ids: list[str] = field(default_factory=list)
    retrieval_candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    retrieval_runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_tool_error: dict[str, Any] | None = None


class AgentCommitResponse(BaseModel):
    action: AgentAction
    annotations: list[annotation_store.AnnotationOut]
    idempotent: bool = False


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
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_retrieval_candidates (
                session_id TEXT NOT NULL,
                user_sub TEXT NOT NULL,
                study_id TEXT NOT NULL,
                slide_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (session_id, user_sub, study_id, slide_id, candidate_id)
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_retrieval_candidates_session "
            "ON agent_retrieval_candidates(session_id, user_sub, study_id, slide_id)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_retrieval_runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_sub TEXT NOT NULL,
                study_id TEXT NOT NULL,
                slide_id TEXT NOT NULL,
                source_fingerprint TEXT,
                viewer_generation INTEGER,
                model TEXT NOT NULL,
                query TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_retrieval_runs_scope "
            "ON agent_retrieval_runs(session_id, user_sub, study_id, slide_id, created_at)"
        )
        await db.commit()


async def _init_postgres(dsn: str) -> None:
    await open_pool(dsn)
    async with connection(dsn) as conn:
        await migrate_agent(conn)


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
    payload = dict(payload)
    logical_payload_json = json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    )
    request_fingerprint = hashlib.sha256(logical_payload_json.encode()).hexdigest()
    if run_context.request_id:
        existing = await _find_existing_action(
            run_context,
            action_type,
            run_context.request_id,
            request_fingerprint,
        )
        if existing:
            run_context.proposal_ids.append(existing.id)
            return existing
        payload.update(
            {
                "_request_id": run_context.request_id,
                "_request_fingerprint": request_fingerprint,
            }
        )
    payload_json = json.dumps(payload, separators=(",", ":"))
    if _storage_kind() == "postgres":
        async with connection(_get_db_url()) as conn:
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
        async with connection(_get_db_url()) as conn:
            row = await conn.fetchrow(
                f"SELECT {columns} FROM agent_actions WHERE id = $1 AND user_sub = $2",
                action_id,
                user_sub,
            )
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
        async with connection(_get_db_url()) as conn:
            rows = await conn.fetch(
                f"SELECT {columns} FROM agent_actions "
                "WHERE session_id = $1 AND user_sub = $2 AND study_id = $3 "
                "ORDER BY created_at ASC",
                session_id,
                user_sub,
                study_id,
            )
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


async def _find_existing_action(
    run_context: AgentRunContext,
    action_type: str,
    request_id: str,
    request_fingerprint: str,
) -> AgentAction | None:
    actions = await _list_actions(
        run_context.session_id,
        run_context.user_sub,
        run_context.context.study_id,
    )
    for action in actions:
        if (
            action.action_type == action_type
            and action.payload.get("_request_id") == request_id
            and action.payload.get("_request_fingerprint") == request_fingerprint
        ):
            return action
    return None


async def _store_retrieval_candidates(
    run_context: AgentRunContext,
    regions: list[dict[str, Any]],
) -> None:
    candidates = [
        region
        for region in regions
        if isinstance(region, dict)
        and isinstance(region.get("candidate_id"), str)
        and region["candidate_id"]
        and isinstance(region.get("points"), list)
    ]
    if not candidates:
        return

    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    values = [
        (
            run_context.session_id,
            run_context.user_sub,
            run_context.context.study_id,
            run_context.context.slide_id,
            region["candidate_id"],
            json.dumps(region, separators=(",", ":")),
            created_at,
        )
        for region in candidates
    ]
    if _storage_kind() == "postgres":
        conn = await asyncpg.connect(_get_db_url())
        try:
            await conn.executemany(
                """
                INSERT INTO agent_retrieval_candidates
                    (session_id, user_sub, study_id, slide_id, candidate_id,
                     payload_json, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (session_id, user_sub, study_id, slide_id, candidate_id)
                DO UPDATE SET payload_json = EXCLUDED.payload_json,
                              created_at = EXCLUDED.created_at
                """,
                values,
            )
            await conn.execute(
                """
                DELETE FROM agent_retrieval_candidates
                WHERE session_id = $1 AND user_sub = $2
                  AND study_id = $3 AND slide_id = $4
                  AND candidate_id NOT IN (
                      SELECT candidate_id
                      FROM agent_retrieval_candidates
                      WHERE session_id = $1 AND user_sub = $2
                        AND study_id = $3 AND slide_id = $4
                      ORDER BY created_at DESC
                      LIMIT 100
                  )
                """,
                run_context.session_id,
                run_context.user_sub,
                run_context.context.study_id,
                run_context.context.slide_id,
            )
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(_get_db_path()) as db:
            await _apply_sqlite_pragmas(db)
            await db.executemany(
                """
                INSERT INTO agent_retrieval_candidates
                    (session_id, user_sub, study_id, slide_id, candidate_id,
                     payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, user_sub, study_id, slide_id, candidate_id)
                DO UPDATE SET payload_json = excluded.payload_json,
                              created_at = excluded.created_at
                """,
                values,
            )
            await db.execute(
                """
                DELETE FROM agent_retrieval_candidates
                WHERE session_id = ? AND user_sub = ?
                  AND study_id = ? AND slide_id = ?
                  AND candidate_id NOT IN (
                      SELECT candidate_id
                      FROM agent_retrieval_candidates
                      WHERE session_id = ? AND user_sub = ?
                        AND study_id = ? AND slide_id = ?
                      ORDER BY created_at DESC
                      LIMIT 100
                  )
                """,
                (
                    run_context.session_id,
                    run_context.user_sub,
                    run_context.context.study_id,
                    run_context.context.slide_id,
                    run_context.session_id,
                    run_context.user_sub,
                    run_context.context.study_id,
                    run_context.context.slide_id,
                ),
            )
            await db.commit()


async def _load_retrieval_candidates(
    run_context: AgentRunContext,
) -> dict[str, dict[str, Any]]:
    if _storage_kind() == "postgres":
        conn = await asyncpg.connect(_get_db_url())
        try:
            rows = await conn.fetch(
                """
                SELECT candidate_id, payload_json
                FROM agent_retrieval_candidates
                WHERE session_id = $1 AND user_sub = $2
                  AND study_id = $3 AND slide_id = $4
                ORDER BY created_at DESC
                LIMIT 100
                """,
                run_context.session_id,
                run_context.user_sub,
                run_context.context.study_id,
                run_context.context.slide_id,
            )
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(_get_db_path()) as db:
            await _apply_sqlite_pragmas(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT candidate_id, payload_json
                FROM agent_retrieval_candidates
                WHERE session_id = ? AND user_sub = ?
                  AND study_id = ? AND slide_id = ?
                ORDER BY created_at DESC
                LIMIT 100
                """,
                (
                    run_context.session_id,
                    run_context.user_sub,
                    run_context.context.study_id,
                    run_context.context.slide_id,
                ),
            )
            rows = await cursor.fetchall()

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        candidate_id = row["candidate_id"] if isinstance(row, dict) else row[0]
        payload_json = row["payload_json"] if isinstance(row, dict) else row[1]
        payload = _decode_json(payload_json)
        if isinstance(candidate_id, str) and isinstance(payload, dict):
            result[candidate_id] = payload
    return result


def _utc_string(offset_seconds: int = 0) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_seconds)
    )


async def _store_retrieval_run(
    run_context: AgentRunContext,
    run_id: str,
    model: str,
    query: str,
    regions: list[dict[str, Any]],
) -> None:
    payload = {"regions": regions, "normalized_coordinate_space": "0..1000"}
    created_at = _utc_string()
    expires_at = _utc_string(24 * 60 * 60)
    values = (
        run_id,
        run_context.session_id,
        run_context.user_sub,
        run_context.context.study_id,
        run_context.context.slide_id,
        run_context.context.viewport.source_fingerprint,
        run_context.context.viewport.viewer_generation,
        model,
        query,
        json.dumps(payload, separators=(",", ":")),
        created_at,
        expires_at,
    )
    if _storage_kind() == "postgres":
        conn = await asyncpg.connect(_get_db_url())
        try:
            await conn.execute(
                """
                DELETE FROM agent_retrieval_runs
                WHERE session_id = $1 AND user_sub = $2
                  AND expires_at <= timezone('utc', now())
                """,
                run_context.session_id,
                run_context.user_sub,
            )
            await conn.execute(
                """
                INSERT INTO agent_retrieval_runs
                    (run_id, session_id, user_sub, study_id, slide_id,
                     source_fingerprint, viewer_generation, model, query,
                     payload_json, created_at, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (run_id) DO UPDATE SET payload_json = EXCLUDED.payload_json,
                    created_at = EXCLUDED.created_at, expires_at = EXCLUDED.expires_at
                """,
                *values,
            )
            await conn.execute(
                """
                DELETE FROM agent_retrieval_runs
                WHERE session_id = $1 AND user_sub = $2
                  AND study_id = $3 AND slide_id = $4
                  AND run_id NOT IN (
                      SELECT run_id FROM agent_retrieval_runs
                      WHERE session_id = $1 AND user_sub = $2
                        AND study_id = $3 AND slide_id = $4
                      ORDER BY created_at DESC LIMIT 100
                  )
                """,
                run_context.session_id,
                run_context.user_sub,
                run_context.context.study_id,
                run_context.context.slide_id,
            )
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(_get_db_path()) as db:
            await _apply_sqlite_pragmas(db)
            await db.execute(
                "DELETE FROM agent_retrieval_runs WHERE expires_at <= ?",
                (_utc_string(),),
            )
            await db.execute(
                """
                INSERT INTO agent_retrieval_runs
                    (run_id, session_id, user_sub, study_id, slide_id,
                     source_fingerprint, viewer_generation, model, query,
                     payload_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET payload_json = excluded.payload_json,
                    created_at = excluded.created_at, expires_at = excluded.expires_at
                """,
                values,
            )
            await db.execute(
                """
                DELETE FROM agent_retrieval_runs
                WHERE session_id = ? AND user_sub = ?
                  AND study_id = ? AND slide_id = ?
                  AND run_id NOT IN (
                      SELECT run_id FROM agent_retrieval_runs
                      WHERE session_id = ? AND user_sub = ?
                        AND study_id = ? AND slide_id = ?
                      ORDER BY created_at DESC LIMIT 100
                  )
                """,
                (
                    run_context.session_id,
                    run_context.user_sub,
                    run_context.context.study_id,
                    run_context.context.slide_id,
                    run_context.session_id,
                    run_context.user_sub,
                    run_context.context.study_id,
                    run_context.context.slide_id,
                ),
            )
            await db.commit()


async def _load_retrieval_runs(
    run_context: AgentRunContext,
) -> dict[str, dict[str, Any]]:
    if _storage_kind() == "postgres":
        conn = await asyncpg.connect(_get_db_url())
        try:
            rows = await conn.fetch(
                """
                SELECT run_id, source_fingerprint, viewer_generation, model,
                       query, payload_json, created_at
                FROM agent_retrieval_runs
                WHERE session_id = $1 AND user_sub = $2 AND study_id = $3
                  AND slide_id = $4 AND expires_at > timezone('utc', now())
                ORDER BY created_at DESC LIMIT 5
                """,
                run_context.session_id,
                run_context.user_sub,
                run_context.context.study_id,
                run_context.context.slide_id,
            )
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(_get_db_path()) as db:
            await _apply_sqlite_pragmas(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT run_id, source_fingerprint, viewer_generation, model,
                       query, payload_json, created_at
                FROM agent_retrieval_runs
                WHERE session_id = ? AND user_sub = ? AND study_id = ?
                  AND slide_id = ? AND expires_at > ?
                ORDER BY created_at DESC LIMIT 5
                """,
                (
                    run_context.session_id,
                    run_context.user_sub,
                    run_context.context.study_id,
                    run_context.context.slide_id,
                    _utc_string(),
                ),
            )
            rows = await cursor.fetchall()

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        def value(name: str, index: int) -> Any:
            try:
                return row[name]
            except (KeyError, IndexError, TypeError):
                return row[index]

        run_id = value("run_id", 0)
        payload = _decode_json(value("payload_json", 5), {})
        if isinstance(run_id, str) and isinstance(payload, dict):
            result[run_id] = {
                "run_id": run_id,
                "source_fingerprint": value("source_fingerprint", 1),
                "viewer_generation": value("viewer_generation", 2),
                "model": value("model", 3),
                "query": value("query", 4),
                "created_at": str(value("created_at", 6)),
                **payload,
            }
    return result


def _retrieval_prompt_context(
    retrieval_runs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for run in retrieval_runs.values():
        regions = run.get("regions", [])
        candidates = []
        if isinstance(regions, list):
            for region in regions:
                if not isinstance(region, dict):
                    continue
                candidates.append(
                    {
                        "rank": region.get("rank"),
                        "candidate_id": region.get("candidate_id"),
                        "score": region.get("score"),
                    }
                )
        summaries.append(
            {
                "retrieval_run_id": run.get("run_id"),
                "query": run.get("query"),
                "model": run.get("model"),
                "created_at": run.get("created_at"),
                "candidates": candidates[:10],
            }
        )
    return summaries


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
        async with connection(_get_db_url()) as conn:
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
    if (
        not context.viewport.source_fingerprint
        or not context.viewport.capture_id
        or context.viewport.viewer_generation is None
    ):
        raise HTTPException(
            status_code=422,
            detail="WSI source binding is required; reload the slide before using the assistant",
        )
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


def _finite_point(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("x"), (int, float))
        and not isinstance(value.get("x"), bool)
        and isinstance(value.get("y"), (int, float))
        and not isinstance(value.get("y"), bool)
        and math.isfinite(float(value["x"]))
        and math.isfinite(float(value["y"]))
    )


def _canonicalize_annotation(
    value: dict[str, Any],
    run_context: AgentRunContext,
    *,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert one proposal into immutable slide-pixel coordinates.

    The model may describe viewport-normalized points or refer to a retrieval
    candidate.  Only this boundary performs conversion; preview and approval
    consume the resulting slide-pixel payload verbatim.
    """
    defaults = defaults or {}
    raw = {**defaults, **value}
    source = str(raw.get("coordinate_space", "viewport"))
    if source not in {
        "viewport",
        "slide_pixels",
        "retrieved_candidate",
        "high_resolution_region",
    }:
        raise ValueError("Unsupported annotation coordinate space")

    context = run_context.context
    viewport = context.viewport
    source_fingerprint = viewport.source_fingerprint
    capture_id = raw.get("capture_id") or viewport.capture_id
    if not source_fingerprint or not capture_id:
        raise ValueError("Annotation source binding is missing; request a fresh viewport")
    if viewport.capture_id and capture_id != viewport.capture_id:
        raise ValueError("Annotation capture is stale; request a fresh viewport")

    if source == "slide_pixels":
        raw_points = raw.get("points")
        if not isinstance(raw_points, list) or not all(_finite_point(point) for point in raw_points):
            raise ValueError("Slide-pixel annotation points are invalid")
        normalized_input = {
            **raw,
            "points": [
                {
                    "x": min(1000.0, max(0.0, float(point["x"]) / max(viewport.slide_width, 1) * 1000.0)),
                    "y": min(1000.0, max(0.0, float(point["y"]) / max(viewport.slide_height, 1) * 1000.0)),
                }
                for point in raw_points
            ],
        }
    else:
        normalized_input = raw
    normalized = _validate_annotation_input(normalized_input)
    points = normalized["points"]
    slide_points: list[dict[str, float]]
    candidate_id = raw.get("candidate_id")
    if source == "retrieved_candidate":
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("A retrieval candidate_id is required")
        candidate = run_context.retrieval_candidates.get(candidate_id)
        candidate_points = candidate.get("points") if candidate else None
        if not isinstance(candidate_points, list):
            raise ValueError("The retrieval candidate is not available for this run")
        points = [Point.model_validate(point).model_dump() for point in candidate_points]
        slide_points = [
            {
                "x": float(point["x"]) / 1000.0 * viewport.slide_width,
                "y": float(point["y"]) / 1000.0 * viewport.slide_height,
            }
            for point in points
        ]
    elif source == "high_resolution_region":
        region = raw.get("coordinate_region")
        if not isinstance(region, dict):
            raise ValueError("A high-resolution coordinate region is required")
        if not all(
            isinstance(region.get(name), (int, float))
            and not isinstance(region.get(name), bool)
            and math.isfinite(float(region[name]))
            for name in ("x", "y")
        ):
            raise ValueError("The annotation region is invalid")
        if not all(
            isinstance(region.get(name), (int, float))
            and not isinstance(region.get(name), bool)
            and math.isfinite(float(region[name]))
            for name in ("width", "height")
        ):
            raise ValueError("The annotation region is invalid")
        x = float(region["x"])
        y = float(region["y"])
        width = float(region["width"])
        height = float(region["height"])
        if (
            x < 0
            or y < 0
            or width <= 0
            or height <= 0
            or x + width > viewport.slide_width
            or y + height > viewport.slide_height
        ):
            raise ValueError("The annotation region is outside the slide")
        slide_points = [
            {"x": x + float(point["x"]) / 1000.0 * width,
             "y": y + float(point["y"]) / 1000.0 * height}
            for point in points
        ]
    elif source == "slide_pixels":
        raw_points = value.get("points")
        if not isinstance(raw_points, list) or not all(_finite_point(point) for point in raw_points):
            raise ValueError("Slide-pixel annotation points are invalid")
        if len(raw_points) < (3 if normalized["geometry_type"] == "polygon" else 2) or len(raw_points) > 100:
            raise ValueError("Annotation geometry has an invalid number of points")
        slide_points = [{"x": float(point["x"]), "y": float(point["y"])} for point in raw_points]
    else:
        image_width = viewport.image_width or viewport.slide_width
        image_height = viewport.image_height or viewport.slide_height
        transform = viewport.image_transform
        if transform is not None and (
            len(transform) != 6 or not all(math.isfinite(value) for value in transform)
        ):
            raise ValueError("The viewport image transform is invalid")
        slide_points = []
        for point in points:
            u = float(point["x"]) / 1000.0 * image_width
            v = float(point["y"]) / 1000.0 * image_height
            if transform:
                x = transform[0] * u + transform[1] * v + transform[2]
                y = transform[3] * u + transform[4] * v + transform[5]
            else:
                x = float(point["x"]) / 1000.0 * viewport.slide_width
                y = float(point["y"]) / 1000.0 * viewport.slide_height
            slide_points.append({"x": x, "y": y})

    if any(
        point["x"] < 0
        or point["y"] < 0
        or point["x"] > viewport.slide_width
        or point["y"] > viewport.slide_height
        for point in slide_points
    ):
        raise ValueError("Annotation points are outside the slide")
    canonical = {
        **normalized,
        "points": slide_points,
        "geometry_version": 2,
        "coordinate_space": "slide_pixels",
        "source_kind": source,
        "source_fingerprint": source_fingerprint,
        "capture_id": capture_id,
        "viewer_generation": viewport.viewer_generation,
        "slide_dimensions": {
            "width": viewport.slide_width,
            "height": viewport.slide_height,
        },
    }
    if source == "high_resolution_region":
        canonical["coordinate_region"] = raw["coordinate_region"]
    if candidate_id:
        canonical["candidate_id"] = candidate_id
    retrieval_run_id = raw.get("retrieval_run_id")
    if isinstance(retrieval_run_id, str) and retrieval_run_id:
        canonical["retrieval_run_id"] = retrieval_run_id
    return canonical


def _svg_selector(geometry_type: str, points: list[dict[str, float]]) -> str:
    if geometry_type == "rectangle":
        xs = [point["x"] for point in points]
        ys = [point["y"] for point in points]
        x = min(xs)
        y = min(ys)
        return (
            f'<svg><rect x="{x:g}" y="{y:g}" '
            f'width="{max(xs) - x:g}" height="{max(ys) - y:g}" /></svg>'
        )
    values = " ".join(f'{point["x"]:g},{point["y"]:g}' for point in points)
    return f'<svg><polygon points="{values}" /></svg>'


def _canonical_action_annotations(
    action: AgentAction,
) -> list[annotation_store.AnnotationIn]:
    payload = action.payload
    context = payload.get("context")
    viewport = context.get("viewport") if isinstance(context, dict) else None
    if not isinstance(viewport, dict):
        raise HTTPException(status_code=409, detail="Proposal capture context is missing")
    width = viewport.get("slide_width")
    height = viewport.get("slide_height")
    drafts = payload.get("annotations") if action.action_type == "annotation_batch" else [payload]
    source_fingerprint = (
        drafts[0].get("source_fingerprint")
        if isinstance(drafts, list) and drafts and isinstance(drafts[0], dict)
        else payload.get("source_fingerprint")
    )
    if (
        not isinstance(width, (int, float))
        or not isinstance(height, (int, float))
        or width <= 0
        or height <= 0
        or not isinstance(source_fingerprint, str)
        or source_fingerprint != viewport.get("source_fingerprint")
    ):
        raise HTTPException(status_code=409, detail="Proposal source binding is invalid")
    if not isinstance(drafts, list) or not 1 <= len(drafts) <= 100:
        raise HTTPException(status_code=422, detail="Annotation proposal is empty or too large")
    result: list[annotation_store.AnnotationIn] = []
    for draft in drafts:
        if (
            not isinstance(draft, dict)
            or draft.get("geometry_version") != 2
            or draft.get("coordinate_space") != "slide_pixels"
        ):
            raise HTTPException(status_code=409, detail="Proposal coordinates are not canonical")
        if draft.get("source_fingerprint") != source_fingerprint:
            raise HTTPException(status_code=409, detail="Proposal source binding is invalid")
        points = draft.get("points")
        geometry_type = draft.get("geometry_type")
        if (
            geometry_type not in {"rectangle", "polygon"}
            or not isinstance(points, list)
            or len(points) < (3 if geometry_type == "polygon" else 2)
            or len(points) > 100
            or not all(_finite_point(point) for point in points)
            or any(
                float(point["x"]) < 0
                or float(point["y"]) < 0
                or float(point["x"]) > float(width)
                or float(point["y"]) > float(height)
                for point in points
            )
        ):
            raise HTTPException(status_code=422, detail="Proposal geometry is invalid")
        layer_name = str(draft.get("layer_name") or payload.get("layer_name") or "Default").strip()
        color = str(draft.get("color") or payload.get("color") or "#3b82f6")
        label = str(draft.get("label") or "AI proposal").strip()
        if not layer_name or len(layer_name) > 100 or not label or len(label) > 200:
            raise HTTPException(status_code=422, detail="Proposal annotation metadata is invalid")
        provenance = dict(payload.get("provenance") or {})
        provenance.update(
            {
                "source": "agent",
                "proposal_id": action.id,
                "candidate_id": draft.get("candidate_id") or provenance.get("candidate_id"),
                "retrieval_run_id": draft.get("retrieval_run_id")
                or provenance.get("retrieval_run_id"),
                "confidence": draft.get("confidence", provenance.get("confidence")),
                "rationale": payload.get("rationale", ""),
            }
        )
        body = annotation_store.AnnotationBody(
            label=label,
            comment=layer_name,
            type=f"{layer_name}|{color}",
            layer_name=layer_name,
            color=color,
            provenance=provenance,
        )
        result.append(
            annotation_store.AnnotationIn(
                slide_id=action.slide_id,
                study_id=action.study_id,
                body=body,
                target=annotation_store.AnnotationTarget(
                    selector={
                        "type": "SvgSelector",
                        "value": _svg_selector(
                            geometry_type,
                            [{"x": float(point["x"]), "y": float(point["y"])} for point in points],
                        ),
                    }
                ),
            )
        )
    return result


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
            **_canonicalize_annotation(
                {
                    "geometry_type": geometry_type,
                    "points": [point.model_dump() for point in points],
                    "label": label,
                    "layer_name": layer_name,
                    "color": color,
                    "confidence": confidence,
                },
                run_context,
            ),
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
  coordinates must be tied to the supplied capture.  For retrieval results,
  use wsi_propose_retrieval_annotations with the retrieval_run_id and ranks
  supplied in the current context.  Do not invent candidate IDs or copy
  candidate geometry into individual proposal calls.  Otherwise use viewport
  coordinates from 0 through 1000 and include the capture_id.  The server
  converts proposals to immutable slide-pixel coordinates before preview or
  approval.  Propose only coarse rectangles or polygons and include a short
  rationale and confidence.  Do not infer or invent patient facts.

For a request to find tissue or morphology, call wsi_find_regions with the
user's natural-language description.  The retrieval service expands and
tokenizes the description into pathology concepts; do not make the user
guess a model vocabulary.  You may provide positive_concepts and
negative_concepts when the user explicitly states inclusions or exclusions.
Always set model to quiltnet_pmb for region searches; the other published
embeddings are slide-level indexes and do not return tile regions.
Similarity is a research ranking, not a diagnostic confidence score.
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


def _bedrock_client():
    session_kwargs: dict[str, Any] = {
        "profile_name": settings.agent_profile or None,
        "region_name": settings.agent_region or None,
    }
    # Some ECS task definitions keep Bedrock credentials separate from the
    # credentials used for slide storage.  Pass them explicitly when present.
    if not session_kwargs["profile_name"] and os.environ.get("BEDROCK_AWS_ACCESS_KEY_ID"):
        session_kwargs.update(
            aws_access_key_id=os.environ.get("BEDROCK_AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("BEDROCK_AWS_SECRET_ACCESS_KEY"),
            aws_session_token=os.environ.get("BEDROCK_AWS_SESSION_TOKEN"),
        )
    session = boto3.Session(**session_kwargs)
    endpoint_url = os.environ.get("BEDROCK_AWS_ENDPOINT_URL") or (
        f"https://bedrock-runtime.{settings.agent_region}.amazonaws.com"
    )
    return session.client("bedrock-runtime", endpoint_url=endpoint_url)


def _bedrock_configured() -> bool:
    profile_name = settings.agent_profile or os.environ.get("AWS_PROFILE")
    if not profile_name:
        # ECS task credentials and other workload identity providers expose
        # credentials through the standard AWS environment variables.
        return bool(
            (
                os.environ.get("AWS_ACCESS_KEY_ID")
                and os.environ.get("AWS_SECRET_ACCESS_KEY")
            )
            or (
                os.environ.get("BEDROCK_AWS_ACCESS_KEY_ID")
                and os.environ.get("BEDROCK_AWS_SECRET_ACCESS_KEY")
            )
        )
    try:
        session = boto3.Session(
            profile_name=profile_name,
            region_name=settings.agent_region or None,
        )
        return session.get_credentials() is not None
    except Exception:
        logger.exception("Unable to inspect Bedrock credentials")
        return False


def _provider_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", "provider_error"))
        if code in {"ExpiredToken", "ExpiredTokenException", "UnrecognizedClientException"}:
            return {
                "code": "bedrock_credentials_expired",
                "message": "Assistant credentials expired; refresh the dev SAML session.",
                "retryable": True,
            }
        return {
            "code": "bedrock_provider_error",
            "message": "The Bedrock assistant could not complete this request.",
            "retryable": False,
        }
    if isinstance(exc, ValueError):
        return {
            "code": "invalid_tool_request",
            "message": str(exc),
            "retryable": True,
        }
    return {
        "code": "agent_tool_error",
        "message": "The assistant tool could not complete this request.",
        "retryable": True,
    }


async def _bedrock_converse(**kwargs: Any) -> dict[str, Any]:
    client = _bedrock_client()
    for attempt in range(2):
        try:
            return await asyncio.to_thread(client.converse, **kwargs)
        except ClientError as exc:
            error = _provider_error(exc)
            if error["code"] != "bedrock_credentials_expired" or attempt:
                raise
            logger.warning("Bedrock credentials expired; reloading the provider session")
            client = _bedrock_client()
    raise RuntimeError("Bedrock request did not return a response")


def _bedrock_tools() -> list[dict[str, Any]]:
    point = {
        "type": "object",
        "properties": {
            "x": {"type": "number", "minimum": 0, "maximum": 1000},
            "y": {"type": "number", "minimum": 0, "maximum": 1000},
        },
        "required": ["x", "y"],
    }
    return [
        {
            "toolSpec": {
                "name": "wsi_find_regions",
                "description": "Find tumor or tissue regions on the current slide using the live QuiltNet tile-retrieval index. Results are normalized to the full slide and each candidate_id can be used by an annotation proposal.",
                "inputSchema": {"json": {"type": "object", "properties": {
                    "query": {"type": "string"},
                    "positive_concepts": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                    "negative_concepts": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
                    "model": {"type": "string", "enum": ["quiltnet_pmb"]},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                }, "required": ["query", "model"]}},
            }
        },
        {
            "toolSpec": {
                "name": "wsi_propose_retrieval_annotations",
                "description": "Create one atomic, reversible annotation proposal batch from ranked candidates in a server-owned retrieval run. Use this for QuiltNet results; the server resolves candidate coordinates.",
                "inputSchema": {"json": {"type": "object", "properties": {
                    "retrieval_run_id": {"type": "string"},
                    "ranks": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 50},
                    "label": {"type": "string"},
                    "layer_name": {"type": "string"},
                    "color": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "rationale": {"type": "string"},
                }, "required": ["retrieval_run_id", "ranks", "label", "layer_name", "color", "confidence", "rationale"]}},
            }
        },
        {
            "toolSpec": {
                "name": "wsi_find_similar_slides",
                "description": "Find published slides similar to the current slide using a slide embedding model.",
                "inputSchema": {"json": {"type": "object", "properties": {
                    "model": {"type": "string", "enum": ["reef_v1_hoptimus0", "reef_v2_hoptimus1", "reef_v2_optimus", "reef_v2_titan"]},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                }, "required": ["model"]}},
            }
        },
        {
            "toolSpec": {
                "name": "wsi_propose_annotations",
                "description": "Create one reversible annotation proposal for user approval. Prefer a retrieved candidate; otherwise use viewport coordinates and the supplied capture_id.",
                "inputSchema": {"json": {"type": "object", "properties": {
                    "geometry_type": {"type": "string", "enum": ["rectangle", "polygon"]},
                    "points": {"type": "array", "items": point, "minItems": 2, "maxItems": 100},
                    "coordinate_space": {"type": "string", "enum": ["viewport", "retrieved_candidate", "high_resolution_region"]},
                    "capture_id": {"type": "string"},
                    "candidate_id": {"type": "string"},
                    "coordinate_region": {"type": "object"},
                    "label": {"type": "string"},
                    "layer_name": {"type": "string"},
                    "color": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "rationale": {"type": "string"},
                }, "required": ["geometry_type", "points", "label", "layer_name", "color", "confidence", "rationale"]}},
            }
        },
        {
            "toolSpec": {
                "name": "wsi_propose_annotation_batch",
                "description": "Create up to 50 reversible annotation proposals for one approval action. Each item can use a retrieved candidate or the current viewport capture.",
                "inputSchema": {"json": {"type": "object", "properties": {
                    "coordinate_space": {"type": "string", "enum": ["viewport", "retrieved_candidate", "high_resolution_region"]},
                    "capture_id": {"type": "string"},
                    "annotations": {"type": "array", "maxItems": 50, "items": {"type": "object"}},
                    "rationale": {"type": "string"},
                }, "required": ["annotations", "rationale"]}},
            }
        },
        {
            "toolSpec": {
                "name": "wsi_propose_viewer_action",
                "description": "Create a reversible viewer navigation proposal.",
                "inputSchema": {"json": {"type": "object", "properties": {
                    "action": {"type": "string", "enum": ["select_slide", "set_filters", "go_to_coordinates", "zoom"]},
                    "parameters": {"type": "object"},
                    "rationale": {"type": "string"},
                }, "required": ["action", "parameters", "rationale"]}},
            }
        },
    ]


def _validate_annotation_input(value: dict[str, Any]) -> dict[str, Any]:
    geometry_type = value.get("geometry_type")
    if geometry_type not in {"rectangle", "polygon"}:
        raise ValueError("geometry_type must be rectangle or polygon")
    points = [Point.model_validate(point) for point in value.get("points", [])]
    if len(points) < (3 if geometry_type == "polygon" else 2) or len(points) > 100:
        raise ValueError("Annotation geometry has an invalid number of points")
    label = str(value.get("label", "")).strip()
    layer_name = str(value.get("layer_name", "")).strip()
    color = str(value.get("color", ""))
    confidence = value.get("confidence")
    if not label or len(label) > 200 or not layer_name or len(layer_name) > 100:
        raise ValueError("Annotation label and layer are required")
    if not color.startswith("#") or len(color) not in {4, 7, 9}:
        raise ValueError("Annotation color must be a hex color")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise ValueError("Annotation confidence must be between 0 and 1")
    return {
        "geometry_type": geometry_type,
        "points": [point.model_dump() for point in points],
        "label": label,
        "layer_name": layer_name,
        "color": color,
        "confidence": float(confidence),
    }


async def _bedrock_tool(
    name: str, arguments: dict[str, Any], run_context: AgentRunContext
) -> dict[str, Any]:
    if name == "wsi_find_regions":
        manifest = await _load_manifest()
        positive = arguments.get("positive_concepts")
        negative = arguments.get("negative_concepts")
        query_plan = None
        if isinstance(positive, list) or isinstance(negative, list):
            query_plan = {
                "primary": str(arguments.get("query", "")).strip(),
                "positive": positive if isinstance(positive, list) else [],
                "negative": negative if isinstance(negative, list) else [],
            }
        requested_model = str(arguments.get("model", "quiltnet_pmb"))
        if requested_model != "quiltnet_pmb":
            logger.warning(
                "Ignoring unsupported region model %s; using quiltnet_pmb",
                requested_model,
            )
        result = await search_regions_for_agent(
            manifest,
            run_context.context.study_id,
            run_context.context.slide_id,
            "quiltnet_pmb",
            min(10, max(1, int(arguments.get("top_k", 5)))),
            str(arguments.get("query", "")),
            run_context.context.viewport.slide_width,
            run_context.context.viewport.slide_height,
            query_plan,
        )
        retrieval_run_id = str(uuid.uuid4())
        regions = [
            {
                **region,
                "retrieval_run_id": retrieval_run_id,
            }
            for region in result.get("regions", [])
            if isinstance(region, dict)
        ]
        result = {
            **result,
            "regions": regions,
            "retrieval_run_id": retrieval_run_id,
        }
        for region in regions:
            if isinstance(region, dict) and isinstance(region.get("candidate_id"), str):
                run_context.retrieval_candidates[region["candidate_id"]] = region
        await _store_retrieval_candidates(
            run_context,
            regions,
        )
        run_context.retrieval_runs[retrieval_run_id] = {
            "run_id": retrieval_run_id,
            "source_fingerprint": run_context.context.viewport.source_fingerprint,
            "viewer_generation": run_context.context.viewport.viewer_generation,
            "model": "quiltnet_pmb",
            "query": str(arguments.get("query", "")),
            "regions": regions,
        }
        await _store_retrieval_run(
            run_context,
            retrieval_run_id,
            "quiltnet_pmb",
            str(arguments.get("query", "")),
            regions,
        )
        return {**result, "normalized_coordinate_space": "0..1000"}
    if name == "wsi_propose_retrieval_annotations":
        retrieval_run_id = arguments.get("retrieval_run_id")
        if not isinstance(retrieval_run_id, str) or not retrieval_run_id:
            raise ValueError("A retrieval_run_id is required")
        retrieval_run = run_context.retrieval_runs.get(retrieval_run_id)
        if retrieval_run is None:
            run_context.retrieval_runs = await _load_retrieval_runs(run_context)
            retrieval_run = run_context.retrieval_runs.get(retrieval_run_id)
        if retrieval_run is None:
            raise ValueError("The retrieval run is expired or unavailable; run a fresh search")
        source_fingerprint = retrieval_run.get("source_fingerprint")
        current_fingerprint = run_context.context.viewport.source_fingerprint
        if source_fingerprint and source_fingerprint != current_fingerprint:
            raise ValueError("The retrieval run belongs to a changed slide source; run a fresh search")
        ranks = arguments.get("ranks")
        if (
            not isinstance(ranks, list)
            or not 1 <= len(ranks) <= 50
            or any(not isinstance(rank, int) or isinstance(rank, bool) or rank < 1 for rank in ranks)
            or len(set(ranks)) != len(ranks)
        ):
            raise ValueError("ranks must contain 1-50 unique positive integers")
        regions_by_rank = {
            region.get("rank"): region
            for region in retrieval_run.get("regions", [])
            if isinstance(region, dict) and isinstance(region.get("rank"), int)
        }
        selected = []
        for rank in ranks:
            region = regions_by_rank.get(rank)
            if region is None:
                raise ValueError(f"Retrieval rank {rank} is unavailable; run a fresh search")
            candidate_id = region.get("candidate_id")
            if not isinstance(candidate_id, str) or not isinstance(region.get("points"), list):
                raise ValueError("The retrieval run contains an invalid candidate")
            run_context.retrieval_candidates[candidate_id] = region
            selected.append(
                _canonicalize_annotation(
                    {
                        "geometry_type": "rectangle",
                        "points": region["points"],
                        "coordinate_space": "retrieved_candidate",
                        "candidate_id": candidate_id,
                        "retrieval_run_id": retrieval_run_id,
                        "label": arguments["label"],
                        "layer_name": arguments["layer_name"],
                        "color": arguments["color"],
                        "confidence": arguments["confidence"],
                    },
                    run_context,
                )
            )
        payload = {
            "annotations": selected,
            "retrieval_run_id": retrieval_run_id,
            "rationale": _safe_rationale(str(arguments["rationale"])),
            "context": _context_snapshot(run_context.context),
        }
        action = await _insert_action(run_context, "annotation_batch", payload)
        return {
            "proposal_id": action.id,
            "status": action.status,
            "retrieval_run_id": retrieval_run_id,
            "ranks": ranks,
        }
    if name == "wsi_find_similar_slides":
        manifest = await _load_manifest()
        row = next(
            (
                row
                for row in manifest["slides"]
                if isinstance(row, dict)
                and row.get("study_id") == run_context.context.study_id
                and str(row.get("slide_id")) == run_context.context.slide_id
            ),
            None,
        )
        if row is None:
            return {"slides": []}
        model = str(arguments.get("model", "reef_v2_titan"))
        top_k = min(10, max(1, int(arguments.get("top_k", 5))))
        return {"model": model, "slides": _similar_slides_for_model(row, model, top_k)}
    if name == "wsi_propose_annotations":
        payload = _canonicalize_annotation(arguments, run_context)
        payload.update({
            "rationale": _safe_rationale(str(arguments.get("rationale", ""))),
            "context": _context_snapshot(run_context.context),
        })
        action = await _insert_action(run_context, "create_annotation", payload)
        return {"proposal_id": action.id, "status": action.status}
    if name == "wsi_propose_annotation_batch":
        raw_annotations = arguments.get("annotations")
        if not isinstance(raw_annotations, list) or not 1 <= len(raw_annotations) <= 50:
            raise ValueError("annotations must contain 1-50 items")
        payload = {
            "annotations": [
                _canonicalize_annotation(item, run_context, defaults=arguments)
                for item in raw_annotations
            ],
            "rationale": _safe_rationale(str(arguments.get("rationale", ""))),
            "context": _context_snapshot(run_context.context),
        }
        action = await _insert_action(run_context, "annotation_batch", payload)
        return {"proposal_id": action.id, "status": action.status}
    if name == "wsi_propose_viewer_action":
        action_name = arguments.get("action")
        parameters = arguments.get("parameters")
        if action_name not in {"select_slide", "set_filters", "go_to_coordinates", "zoom"} or not isinstance(parameters, dict):
            raise ValueError("Invalid viewer action")
        payload = {
            "action": action_name,
            "parameters": parameters,
            "rationale": _safe_rationale(str(arguments.get("rationale", ""))),
            "context": _context_snapshot(run_context.context),
        }
        action = await _insert_action(run_context, "viewer_action", payload)
        return {"proposal_id": action.id, "status": action.status}
    raise ValueError(f"Unknown Bedrock tool: {name}")


def _bedrock_user_content(
    request: ChatRequest,
    retrieval_runs: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    context = request.context.model_dump(exclude={"viewport": {"image_data_url"}})
    if retrieval_runs:
        context["retrieval_runs"] = _retrieval_prompt_context(retrieval_runs)
    prompt = json.dumps(
        {
            "current_context": context,
            "conversation": [message.model_dump() for message in request.history],
            "current_request": request.message,
        },
        separators=(",", ":"),
    )
    content: list[dict[str, Any]] = [{"text": prompt}]
    image_url = request.context.viewport.image_data_url
    if image_url:
        encoded = image_url.split(",", 1)[1]
        content.append({"image": {"format": "jpeg", "source": {"bytes": base64.b64decode(encoded)}}})
    return content


async def _stream_bedrock(request: ChatRequest, user_sub: str):
    run_context = AgentRunContext(
        user_sub=user_sub,
        session_id=request.session_id,
        context=request.context,
        request_id=request.request_id,
    )
    run_context.retrieval_candidates = await _load_retrieval_candidates(run_context)
    run_context.retrieval_runs = await _load_retrieval_runs(run_context)
    for retrieval_run in run_context.retrieval_runs.values():
        for region in retrieval_run.get("regions", []):
            if isinstance(region, dict) and isinstance(region.get("candidate_id"), str):
                run_context.retrieval_candidates[region["candidate_id"]] = region
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": _bedrock_user_content(request, run_context.retrieval_runs)}
    ]
    for _ in range(6):
        response = await _bedrock_converse(
            modelId=settings.agent_model,
            system=[{"text": _agent_instructions()}],
            messages=messages,
            toolConfig={"tools": _bedrock_tools()},
            inferenceConfig={"maxTokens": 1500, "temperature": 0.1},
        )
        output = response.get("output", {}).get("message", {})
        content = output.get("content", [])
        text_parts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("text")]
        if text_parts:
            yield _sse("message.delta", {"text": "".join(text_parts)})
        tool_uses = [block.get("toolUse") for block in content if isinstance(block, dict) and block.get("toolUse")]
        if not tool_uses:
            break
        messages.append(output)
        results = []
        for tool_use in tool_uses:
            name = str(tool_use.get("name", ""))
            yield _sse("tool.called", {"name": name})
            try:
                result = await _bedrock_tool(name, tool_use.get("input", {}), run_context)
                result_content = {"json": result}
                run_context.last_tool_error = None
            except Exception as exc:
                error = _provider_error(exc)
                run_context.last_tool_error = error
                logger.warning(
                    "Bedrock WSI tool failed (%s): %s: %s",
                    name,
                    type(exc).__name__,
                    str(exc),
                )
                yield _sse("tool.error", {"name": name, **error})
                result_content = {"json": {"error": error}}
            results.append({"toolResult": {"toolUseId": tool_use.get("toolUseId"), "content": [result_content]}})
        messages.append({"role": "user", "content": results})
    for proposal_id in run_context.proposal_ids:
        proposal = await _get_action(proposal_id, user_sub)
        if proposal:
            yield _sse("proposal", proposal.model_dump())
    completion: dict[str, Any] = {
        "proposal_ids": run_context.proposal_ids,
        "success": run_context.last_tool_error is None,
    }
    if run_context.last_tool_error:
        completion["error"] = run_context.last_tool_error
    yield _sse("complete", completion)


def _sse(event: str, payload: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def _stream_agent(request: ChatRequest, user_sub: str):
    # The legacy stream remains available to offline unit tests and explicit
    # OpenAI-provider development runs. Bedrock runs require configured AWS
    # credentials before attempting the provider call.
    if settings.agent_provider.lower() == "bedrock" and _bedrock_configured():
        try:
            async for chunk in _stream_bedrock(request, user_sub):
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Bedrock WSI agent run failed")
            yield _sse("error", _provider_error(exc))
        return
    run_context = AgentRunContext(
        user_sub=user_sub,
        session_id=request.session_id,
        context=request.context,
        request_id=request.request_id,
    )
    try:
        run_context.retrieval_candidates = await _load_retrieval_candidates(run_context)
    except Exception:
        logger.exception("Failed to load persisted WSI retrieval candidates")
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
        "configured": _bedrock_configured()
        if settings.agent_provider.lower() == "bedrock"
        else bool(os.environ.get("OPENAI_API_KEY", "").startswith("sk-") or _read_api_key_file()),
        "provider": settings.agent_provider,
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
    configured = (
        _bedrock_configured()
        if settings.agent_provider.lower() == "bedrock"
        else _ensure_openai_credentials()
    )
    if not configured:
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


async def _committed_annotation_rows(
    ids: list[str],
    db: Any,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for annotation_id in ids:
        if isinstance(db, aiosqlite.Connection):
            cursor = await db.execute(
                "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
            )
            row = await cursor.fetchone()
        else:
            row = await db.fetchrow(
                """
                SELECT
                    id, slide_id, study_id, body, target, created_by, visible_to,
                    version,
                    to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS created_at,
                    to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS updated_at
                FROM annotations WHERE id = $1
                """,
                annotation_id,
            )
        if row is not None:
            rows.append(dict(row))
    return rows


async def _commit_annotations(
    action_id: str,
    user_sub: str,
    request: CommitAnnotationsRequest,
) -> AgentCommitResponse:
    columns = (
        "id, session_id, action_type, study_id, slide_id, payload_json, status, "
        "created_at, decided_at, outcome_json"
    )

    async def finish(
        db: Any,
        action_row: Any,
        *,
        commit: bool = True,
    ) -> AgentCommitResponse:
        action = _row_to_action(action_row)
        if action.slide_id != request.slide_id:
            raise HTTPException(status_code=409, detail="The proposal slide is no longer active")
        if action.status == "completed":
            outcome = action.outcome or {}
            ids = outcome.get("annotation_ids", [])
            if not isinstance(ids, list):
                raise HTTPException(status_code=409, detail="Completed proposal has invalid outcome")
            rows = await _committed_annotation_rows([str(item) for item in ids], db)
            return AgentCommitResponse(
                action=action,
                annotations=[annotation_store._row_to_out(row) for row in rows],
                idempotent=True,
            )
        if action.status != "pending":
            raise HTTPException(status_code=409, detail="Proposal is no longer pending")
        action_drafts = (
            action.payload.get("annotations")
            if action.action_type == "annotation_batch"
            else [action.payload]
        )
        action_source = (
            action_drafts[0].get("source_fingerprint")
            if isinstance(action_drafts, list)
            and action_drafts
            and isinstance(action_drafts[0], dict)
            else None
        )
        if action_source != request.source_fingerprint:
            raise HTTPException(status_code=409, detail="The proposal source is stale")
        if request.viewer_generation is not None:
            generations = {
                draft.get("viewer_generation")
                for draft in action_drafts
                if isinstance(draft, dict)
            }
            if generations != {request.viewer_generation}:
                raise HTTPException(status_code=409, detail="The proposal viewer generation is stale")
        annotations = _canonical_action_annotations(action)
        rows: list[dict[str, Any]] = []
        for annotation in annotations:
            if isinstance(db, aiosqlite.Connection):
                rows.append(
                    await annotation_store._insert_sqlite_connection(
                        db, annotation, user_sub
                    )
                )
            else:
                rows.append(
                    await annotation_store._insert_postgres_connection(
                        db, annotation, user_sub
                    )
                )
        ids = [str(row["id"]) for row in rows]
        decided_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        outcome = {"success": True, "detail": "Annotations committed", "annotation_ids": ids}
        if isinstance(db, aiosqlite.Connection):
            await db.execute(
                """
                UPDATE agent_actions
                SET status = 'completed', decided_at = ?, outcome_json = ?
                WHERE id = ? AND user_sub = ? AND status = 'pending'
                """,
                (decided_at, json.dumps(outcome), action_id, user_sub),
            )
            cursor = await db.execute(
                f"SELECT {columns} FROM agent_actions WHERE id = ?", (action_id,)
            )
            updated_row = await cursor.fetchone()
        else:
            updated_row = await db.fetchrow(
                """
                UPDATE agent_actions
                SET status = 'completed', decided_at = $1, outcome_json = $2
                WHERE id = $3 AND user_sub = $4 AND status = 'pending'
                RETURNING id, session_id, action_type, study_id, slide_id,
                          payload_json, status, created_at, decided_at, outcome_json
                """,
                decided_at,
                json.dumps(outcome),
                action_id,
                user_sub,
            )
        if updated_row is None:
            raise HTTPException(status_code=409, detail="Proposal is no longer pending")
        if commit and isinstance(db, aiosqlite.Connection):
            await db.commit()
        return AgentCommitResponse(
            action=_row_to_action(updated_row),
            annotations=[annotation_store._row_to_out(row) for row in rows],
        )

    if _storage_kind() == "postgres":
        conn = await asyncpg.connect(_get_db_url())
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"SELECT {columns} FROM agent_actions WHERE id = $1 AND user_sub = $2 FOR UPDATE",
                    action_id,
                    user_sub,
                )
                if row is None:
                    raise HTTPException(status_code=404, detail="Agent proposal not found")
                return await finish(conn, row, commit=False)
        finally:
            await conn.close()
    db = await aiosqlite.connect(_get_db_path())
    try:
        await annotation_store._apply_sqlite_pragmas(db)
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            f"SELECT {columns} FROM agent_actions WHERE id = ? AND user_sub = ?",
            (action_id, user_sub),
        )
        row = await cursor.fetchone()
        if row is None:
            await db.rollback()
            raise HTTPException(status_code=404, detail="Agent proposal not found")
        return await finish(db, row)
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


@router.post("/actions/{action_id}/apply", response_model=AgentAction)
async def apply_agent_action(
    action_id: str,
    user: dict[str, Any] = _AGENT_READ_WRITE_USER,
) -> AgentAction:
    action = await _get_action(action_id, user["sub"])
    if action and action.action_type in {"create_annotation", "annotation_batch"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Annotation proposals must be committed through the atomic commit endpoint",
        )
    return await _transition_action(action_id, user, "pending", "approved")


@router.post(
    "/actions/{action_id}/commit-annotations",
    response_model=AgentCommitResponse,
)
async def commit_agent_annotations(
    action_id: str,
    request: CommitAnnotationsRequest,
    user: dict[str, Any] = _AGENT_READ_WRITE_USER,
) -> AgentCommitResponse:
    action = await _get_action(action_id, user["sub"])
    if not action:
        raise HTTPException(status_code=404, detail="Agent proposal not found")
    _require_study(user, action.study_id)
    return await _commit_annotations(action_id, user["sub"], request)


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
