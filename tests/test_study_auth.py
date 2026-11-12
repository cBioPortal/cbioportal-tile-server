import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.study_auth import (
    InsufficientStudyScope,
    InvalidStudyToken,
    require_user,
    settings,
    validate_study_token,
)
from tests.test_auth import SLIDE_KEY, TILE_SOURCE

SECRET = "s" * 32
AUDIENCE = "cbioportal-wsi"
ANNOTATION_SCOPE = "annotations:read annotations:write"
AGENT_SCOPE = "agent:chat research:read annotations:read annotations:write"
ANNOTATION_SCOPES = {"annotations:read", "annotations:write"}


def make_raw_token(secret: str, header, payload) -> str:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    signing_input = f"{encode(header)}.{encode(payload)}"
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{signing_input}.{signature}"


def study_claims(**overrides):
    """Claims shaped like cBioPortal's WsiPurposeTokenController issues them."""
    now = int(time.time())
    claims = {
        "sub": "user@example.org",
        "aud": AUDIENCE,
        "scope": ANNOTATION_SCOPE,
        "study_id": "study-a",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return claims


def make_token(secret: str = SECRET, **overrides) -> str:
    return make_raw_token(secret, {"typ": "JWT", "alg": "HS256"}, study_claims(**overrides))


def test_study_token_returns_study_and_subject():
    claims = validate_study_token(make_token(), SECRET, AUDIENCE, ANNOTATION_SCOPES)
    assert claims["study_id"] == "study-a"
    assert claims["sub"] == "user@example.org"


def test_study_token_accepts_a_superset_of_scopes():
    token = make_token(scope=AGENT_SCOPE)
    assert validate_study_token(token, SECRET, AUDIENCE, {"agent:chat", "research:read"})


def test_study_token_without_a_required_scope_is_insufficient():
    with pytest.raises(InsufficientStudyScope):
        validate_study_token(make_token(), SECRET, AUDIENCE, {"agent:chat"})


@pytest.mark.parametrize("change", [
    {"aud": "other"},
    {"aud": [AUDIENCE]},
    {"scope": None},
    {"sub": ""},
    {"study_id": " "},
    {"exp": int(time.time()) - 1},
    {"exp": int(time.time()) + 1000},
    {"iat": int(time.time()) + 120},
])
def test_study_token_rejects_invalid_claims(change):
    with pytest.raises(InvalidStudyToken) as exc_info:
        validate_study_token(make_token(**change), SECRET, AUDIENCE, ANNOTATION_SCOPES)
    assert not isinstance(exc_info.value, InsufficientStudyScope)


@pytest.mark.parametrize("change", [
    {"slide_key": SLIDE_KEY},
    {"enc": "x"},
    {"wsi_auth_version": 4},
    {"resource_id": "wsi_slides"},
    {"image_id": "slide-a"},
    {"tile_source": TILE_SOURCE},
])
def test_study_token_never_names_a_slide(change):
    with pytest.raises(InvalidStudyToken):
        validate_study_token(make_token(**change), SECRET, AUDIENCE, ANNOTATION_SCOPES)


@pytest.mark.parametrize("header", [
    {"alg": "none", "typ": "JWT"},
    {"alg": "HS512", "typ": "JWT"},
    {"alg": "HS256"},
])
def test_study_token_requires_an_hs256_jwt_header(header):
    token = make_raw_token(SECRET, header, study_claims())
    with pytest.raises(InvalidStudyToken, match="algorithm"):
        validate_study_token(token, SECRET, AUDIENCE, ANNOTATION_SCOPES)


def test_study_token_rejects_wrong_signature():
    with pytest.raises(InvalidStudyToken, match="signature"):
        validate_study_token(make_token("t" * 32), SECRET, AUDIENCE, ANNOTATION_SCOPES)


@pytest.mark.parametrize("secret", ["", "short"])
def test_study_token_needs_a_configured_secret(secret):
    with pytest.raises(InvalidStudyToken, match="not configured"):
        validate_study_token(make_token(secret or "x"), secret, AUDIENCE, ANNOTATION_SCOPES)


@pytest.fixture
def study_auth(monkeypatch):
    monkeypatch.setattr(settings, "annotation_auth_enabled", True)
    monkeypatch.setattr(settings, "wsi_auth_secret", SECRET)
    monkeypatch.setattr(settings, "wsi_auth_previous_secret", "")
    monkeypatch.setattr(settings, "wsi_auth_audience", AUDIENCE)


def _annotation_request(method: str) -> Request:
    return Request({"type": "http", "method": method, "path": "/annotations", "headers": []})


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


async def test_require_user_returns_the_token_study(study_auth):
    user = await require_user(_annotation_request("POST"), _bearer(make_token()))
    assert user == {
        "sub": "user@example.org",
        "groups": [],
        "study_id": "study-a",
        "scopes": ANNOTATION_SCOPES,
    }


async def test_require_user_needs_write_scope_to_modify(study_auth):
    token = make_token(scope="annotations:read")
    assert (await require_user(_annotation_request("GET"), _bearer(token)))["study_id"] == "study-a"
    for method in ("POST", "PUT", "DELETE"):
        with pytest.raises(HTTPException) as exc:
            await require_user(_annotation_request(method), _bearer(token))
        assert exc.value.status_code == 403


async def test_require_user_accepts_the_previous_secret_during_rotation(study_auth, monkeypatch):
    monkeypatch.setattr(settings, "wsi_auth_previous_secret", "p" * 32)
    user = await require_user(_annotation_request("GET"), _bearer(make_token("p" * 32)))
    assert user["sub"] == "user@example.org"
    with pytest.raises(HTTPException) as exc:
        await require_user(_annotation_request("GET"), _bearer(make_token("x" * 32)))
    assert exc.value.status_code == 401


async def test_require_user_rejects_missing_and_foreign_tokens(study_auth):
    for creds in (None, _bearer("generic-keycloak-token")):
        with pytest.raises(HTTPException) as exc:
            await require_user(_annotation_request("GET"), creds)
        assert exc.value.status_code == 401


async def test_require_user_refuses_when_no_secret_is_configured(study_auth, monkeypatch):
    monkeypatch.setattr(settings, "wsi_auth_secret", "")
    with pytest.raises(HTTPException) as exc:
        await require_user(_annotation_request("GET"), _bearer(make_token()))
    assert exc.value.status_code == 503


async def test_dev_flag_disables_study_tokens(study_auth, monkeypatch):
    monkeypatch.setattr(settings, "annotation_auth_enabled", False)
    monkeypatch.setattr(settings, "wsi_auth_secret", "")
    assert (await require_user(_annotation_request("GET"), None))["sub"] == "dev-user"
