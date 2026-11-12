"""Companion service: the annotation API, separate from pixel serving.

Run it from the same image as the tile server::

    gunicorn -c python:app.gunicorn_conf app.companion:app

It trusts ``X-Auth-Request-User`` and ``X-Auth-Request-Study``
(``app.request_identity``), so it must only be reachable through the
authenticating reverse proxy: a pod-network Service admitted by a
NetworkPolicy from the ingress controller alone, never ``hostNetwork``.
The pixel app (``app.main``) serves none of these routes.

Every request may carry at most ``COMPANION_MAX_BODY_BYTES``.  Unsafe
methods (POST, PUT, PATCH, DELETE) must send ``X-Requested-With:
XMLHttpRequest``, a JSON ``Content-Type`` when there is a body, and either
no ``Origin`` or one that is the request's own host or listed in
``COMPANION_ALLOWED_ORIGINS``.  The service sends no CORS headers, so a
cross-site page cannot send the custom header.
"""

import json
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Response
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import annotations
from .config import settings

logger = logging.getLogger(__name__)

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REQUESTED_WITH = "XMLHttpRequest"
NO_STORE = {"Cache-Control": "private, no-store"}


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


def _origin_allowed(origin: str, host: str | None) -> bool:
    normalized = origin.strip().rstrip("/").lower()
    allowed = {item.strip().rstrip("/").lower() for item in settings.companion_allowed_origins}
    if normalized in allowed:
        return True
    parsed = urlsplit(normalized)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    return host is not None and parsed.netloc == host.strip().lower()


def csrf_refusal(scope: Scope) -> tuple[int, str] | None:
    """Why an unsafe request must be refused, or None when it may proceed."""
    if scope["method"] not in UNSAFE_METHODS:
        return None
    if _header(scope, b"x-requested-with") != REQUESTED_WITH:
        return 403, f"X-Requested-With: {REQUESTED_WITH} is required"
    origin = _header(scope, b"origin")
    if origin is not None and not _origin_allowed(origin, _header(scope, b"host")):
        return 403, "Cross-origin request refused"
    if _header(scope, b"sec-fetch-site") == "cross-site" and origin is None:
        return 403, "Cross-origin request refused"
    length = _header(scope, b"content-length")
    has_body = _header(scope, b"transfer-encoding") is not None or (
        length is not None and length.strip() not in ("", "0")
    )
    if has_body:
        media_type = (_header(scope, b"content-type") or "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            return 415, "Content-Type must be application/json"
    return None


class CompanionGuard:
    """Strip ``/wsi``, apply the CSRF rules and cap the request body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path == "/wsi" or path.startswith("/wsi/"):
            scope = dict(scope)
            scope["path"] = path[4:] or "/"

        refusal = csrf_refusal(scope)
        if refusal is not None:
            status_code, detail = refusal
            await JSONResponse({"detail": detail}, status_code=status_code, headers=NO_STORE)(
                scope, receive, send
            )
            return

        limit = settings.companion_max_body_bytes
        length = _header(scope, b"content-length")
        if length is not None:
            try:
                too_large = int(length) > limit
            except ValueError:
                too_large = True
            if too_large:
                await JSONResponse(
                    {"detail": "Request body too large"}, status_code=413, headers=NO_STORE
                )(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail="Request body too large")
            return message

        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(key.lower() == b"cache-control" for key, _ in headers):
                    headers.append((b"cache-control", NO_STORE["Cache-Control"].encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, limited_receive, send_no_store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await annotations.init_db()
    logger.info("Annotation DB ready (%s)", annotations.storage_kind())
    try:
        yield
    finally:
        await annotations.close_db()


app = FastAPI(
    title="WSI companion service",
    description="Slide annotations for cBioPortal, behind an authenticating proxy.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(CompanionGuard)
app.include_router(annotations.router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "companion"}


@app.get("/ready")
async def ready():
    payload = {
        "status": "ready",
        "service": "companion",
        "annotation_storage": annotations.storage_kind(),
        "anonymous_allowed": settings.annotation_allow_anonymous,
    }
    try:
        await annotations.ping()
    except Exception as exc:  # readiness reports any storage failure
        logger.warning("Annotation storage is not reachable: %s", type(exc).__name__)
        payload["status"] = "unavailable"
        payload["reason"] = "annotation storage is not reachable"
        return Response(json.dumps(payload), status_code=503, media_type="application/json")
    return payload
