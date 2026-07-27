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


def test_valid_wsi_token():
    secret = "s" * 32
    token = make_token(
        secret,
        sub="user@example.org",
        aud="cbioportal-wsi",
        scope="wsi:read",
        iat=int(time.time()),
        exp=int(time.time()) + 300,
    )
    assert validate_wsi_token(token, secret, "cbioportal-wsi")["sub"] == "user@example.org"


@pytest.mark.parametrize("change", [
    {"scope": "wsi:write"},
    {"aud": "other-service"},
    {"exp": int(time.time()) - 1},
])
def test_invalid_claims_are_rejected(change):
    secret = "s" * 32
    claims = {
        "sub": "user@example.org",
        "aud": "cbioportal-wsi",
        "scope": "wsi:read",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    claims.update(change)
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(make_token(secret, **claims), secret, "cbioportal-wsi")


def test_wrong_secret_is_rejected():
    secret = "s" * 32
    token = make_token(
        secret,
        sub="user@example.org",
        aud="cbioportal-wsi",
        scope="wsi:read",
        iat=int(time.time()),
        exp=int(time.time()) + 300,
    )
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(token, "x" * 32, "cbioportal-wsi")


def test_non_object_header_and_payload_are_rejected():
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(
            make_raw_token(secret, [], {}), secret, "cbioportal-wsi"
        )
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(
            make_raw_token(secret, {"alg": "HS256", "typ": "JWT"}, []),
            secret,
            "cbioportal-wsi",
        )


def test_token_lifetime_cannot_exceed_configured_limit():
    secret = "s" * 32
    now = int(time.time())
    token = make_token(
        secret,
        sub="user@example.org",
        aud="cbioportal-wsi",
        scope="wsi:read",
        iat=now,
        exp=now + 11,
    )
    with pytest.raises(InvalidWsiToken):
        validate_wsi_token(token, secret, "cbioportal-wsi", max_ttl=10)
