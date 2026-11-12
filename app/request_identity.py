"""Request identity for the companion routes, taken from a trusted proxy.

The companion service (``app.companion``) sits behind a reverse proxy that
authorizes every request against cBioPortal's
``GET /api/studies/{studyId}/access`` with the browser's session cookie.
The portal answers 204 with ``X-Auth-Request-User`` (absent when anonymous)
and ``X-Auth-Request-Study``, 401 when not signed in, and 403 when the user
may not read the study.  The proxy copies those two headers onto the
request, replacing any the client sent.

This module trusts those headers and nothing else, so the service must only
be reachable through that proxy.  ``require_user`` returns
``{"sub", "groups", "study_id"}``:

- no ``X-Auth-Request-Study``: 401;
- ``studyId`` missing, repeated (in any letter case) or different from
  ``X-Auth-Request-Study``: 403;
- no ``X-Auth-Request-User``: the configured anonymous identity when
  ``ANNOTATION_ALLOW_ANONYMOUS=true`` (development only), else 401.

Handlers compare any study a body, path or stored row names with
``require_same_study``.
"""

import re
from typing import Any

from fastapi import HTTPException, Request, status

from .config import settings

USER_HEADER = "X-Auth-Request-User"
STUDY_HEADER = "X-Auth-Request-Study"
STUDY_QUERY_PARAM = "studyId"

# The same study ID rule the pixel routes apply to `studyId`.
STUDY_ID_PATTERN = re.compile(r"^(?!\.+$)[A-Za-z0-9_.-]{1,128}$")
# A user name the portal forwards: printable, no control characters.
USER_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _query_values(request: Request, name: str) -> list[str]:
    """Every value of ``name`` in the query string, matched case-insensitively.

    nginx ``$arg_`` lookups ignore case, so ``StudyID=a&studyId=b`` could be
    authorized for one study at the proxy and read as another here.
    """
    wanted = name.lower()
    return [
        value
        for param, value in request.query_params.multi_items()
        if param.lower() == wanted
    ]


def _anonymous_user() -> str:
    user = settings.annotation_anonymous_user.strip()
    if not USER_PATTERN.fullmatch(user):
        raise _unauthorized("Not signed in")
    return user


def trusted_identity(request: Request) -> dict[str, Any]:
    """Return the proxy-asserted user and study for ``request``."""
    study = request.headers.get(STUDY_HEADER, "").strip()
    if not study or not STUDY_ID_PATTERN.fullmatch(study):
        raise _unauthorized("Request was not authorized for a study")

    if _query_values(request, STUDY_QUERY_PARAM) != [study]:
        raise _forbidden("studyId must be given once and match the authorized study")

    user = request.headers.get(USER_HEADER, "").strip()
    if not user:
        if not settings.annotation_allow_anonymous:
            raise _unauthorized("Not signed in")
        user = _anonymous_user()
    elif not USER_PATTERN.fullmatch(user):
        raise _unauthorized("Malformed user")

    return {"sub": user, "groups": [], "study_id": study}


async def require_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency: the request's user and authorized study."""
    return trusted_identity(request)


def require_same_study(user: dict[str, Any], study_id: Any, what: str = "request") -> None:
    """403 unless ``study_id`` is the study the proxy authorized."""
    if not isinstance(study_id, str) or study_id != user["study_id"]:
        raise _forbidden(f"Authorized study does not match {what}")
