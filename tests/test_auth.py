import base64
import hashlib
import hmac
import json
import time

import pytest

from app.auth import InvalidWsiToken, validate_wsi_token


def make_token(secret: str, **claims) -> str:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    header = encode({"alg": "HS256", "typ": "JWT"})
    payload = encode(claims)
    signing_input = f"{header}.{payload}".encode()
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def make_raw_token(secret: str, header, payload) -> str:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    encoded_header = encode(header)
    encoded_payload = encode(payload)
    signing_input = f"{encoded_header}.{encoded_payload}".encode()
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{encoded_header}.{encoded_payload}.{signature}"


def valid_claims(**overrides):
    now = int(time.time())
    claims = {
        "sub": "user@example.org",
        "aud": "cbioportal-wsi",
        "scope": "wsi:read",
        "study_id": "study-a",
        "wsi_auth_version": 1,
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return claims


def test_valid_wsi_token():
    secret = "s" * 32
    assert validate_wsi_token(
        make_token(secret, **valid_claims()), secret, "cbioportal-wsi"
    )["sub"] == "user@example.org"


@pytest.mark.parametrize("change", [
    {"scope": "wsi:write"},
    {"aud": "other-service"},
    {"exp": int(time.time()) - 1},
])
def test_invalid_claims_are_rejected(change):
    secret = "s" * 32
    claims = valid_claims(**change)
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(make_token(secret, **claims), secret, "cbioportal-wsi")


def test_wrong_secret_is_rejected():
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(
            make_token(secret, **valid_claims()), "x" * 32, "cbioportal-wsi"
        )


def test_non_object_header_and_payload_are_rejected():
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(make_raw_token(secret, [], {}), secret, "cbioportal-wsi")
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(
            make_raw_token(secret, {"alg": "HS256", "typ": "JWT"}, []),
            secret,
            "cbioportal-wsi",
        )


@pytest.mark.parametrize("change", [
    {"study_id": ""},
    {"wsi_auth_version": 2},
    {"exp": int(time.time()) + 1000},
])
def test_resource_binding_and_max_ttl_claims_are_required(change):
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(
            make_token(secret, **valid_claims(**change)), secret, "cbioportal-wsi"
        )


def test_token_lifetime_cannot_exceed_configured_limit():
    secret = "s" * 32
    now = int(time.time())
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(
            make_token(
                secret,
                **valid_claims(iat=now, exp=now + 11),
            ),
            secret,
            "cbioportal-wsi",
            max_ttl=10,
        )
