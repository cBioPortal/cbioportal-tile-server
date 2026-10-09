import base64
import hashlib
import hmac
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.auth import (
    InvalidWsiToken,
    decode_source_seal_key,
    decrypt_sealed_claims,
    source_cache_identity,
    source_digest,
    validate_wsi_auth_configuration,
    validate_wsi_token,
)

SLIDE_KEY = "0123456789abcdef0123456789abcdef"
TILE_SOURCE = "s3://slides/slide-a.svs"
THUMBNAIL_SOURCE = "s3://thumbs/slide-a.jpg"
SEAL_KEY = bytes(range(32))
SEAL_KEY_B64 = base64.b64encode(SEAL_KEY).decode()

# Contract test vector (docs/wsi-deid-slide-key-contract.md, V6.1).
VECTOR_SEAL_KEY_B64 = "8XaidWVtlFzV7hwyAwtTNjN13MNLqnenz5Sy7Y+cbAs="
VECTOR_SLIDE_KEY = "0123456789abcdef0123456789abcdef"
VECTOR_KEY_HEX = "f176a275656d945cd5ee1c32030b53363375dcc34baa77a7cf94b2ed8f9c6c0b"
VECTOR_KEY = bytes.fromhex(VECTOR_KEY_HEX)
VECTOR_PLAINTEXT = (
    '{"image_id":"1","tile_source":"file:///app/testdata/1.svs",'
    '"thumbnail_source":"file:///app/testdata/1.jpg"}'
)
VECTOR_ENC = (
    "AAECAwQFBgcICQoLNpOTYnY8HVb-KcPzu1F5HPykr7D0YY_UhVbbyjOFRlxC63fxCt09YO1aYC-phb85"
    "wDhN5PPPpC0X46RSD0K0bRRgSptRb9wDiqMLtftFQ6VpBGfGILaddEV_s-Zsmpp28fG5Z3XTNWnyoBRa"
    "9qfr9t209wS7V-LhGl8i"
)


def seal_claims(
    seal_key: bytes,
    slide_key: str,
    sealed: dict | str,
    nonce: bytes = bytes(range(12)),
) -> str:
    """Produce an `enc` claim exactly as the data provider seals it (contract V6.1)."""
    plaintext = sealed if isinstance(sealed, str) else json.dumps(sealed, separators=(",", ":"))
    ciphertext = AESGCM(seal_key).encrypt(
        nonce, plaintext.encode("utf-8"), slide_key.encode("utf-8")
    )
    return base64.urlsafe_b64encode(nonce + ciphertext).rstrip(b"=").decode()


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


def valid_claims(seal_key: bytes = SEAL_KEY, **overrides):
    now = int(time.time())
    slide_key = overrides.get("slide_key", SLIDE_KEY)
    claims = {
        "sub": "user@example.org",
        "aud": "cbioportal-wsi",
        "scope": "wsi:read",
        "study_id": "study-a",
        "slide_key": slide_key,
        "wsi_auth_version": 4,
        "thumbnail_width": 1024,
        "thumbnail_height": 768,
        "iat": now,
        "exp": now + 300,
        "enc": seal_claims(
            seal_key,
            slide_key,
            {
                "image_id": "slide-a",
                "tile_source": TILE_SOURCE,
                "thumbnail_source": THUMBNAIL_SOURCE,
            },
        ),
    }
    claims.update(overrides)
    return claims


def make_v4_token(secret: str = "s" * 32, seal_key: bytes = SEAL_KEY, **overrides) -> str:
    return make_token(secret, **valid_claims(seal_key, **overrides))


def validate(token: str, secret: str = "s" * 32, audience: str = "cbioportal-wsi", **kwargs) -> dict:
    kwargs.setdefault("seal_key", SEAL_KEY)
    return validate_wsi_token(token, secret, audience, **kwargs)


def test_valid_wsi_token():
    secret = "s" * 32
    claims = validate(make_v4_token(secret), secret, "cbioportal-wsi")
    assert claims["sub"] == "user@example.org"
    assert claims["slide_key"] == SLIDE_KEY
    assert claims["image_id"] == "slide-a"
    assert claims["tile_source"] == TILE_SOURCE
    assert claims["thumbnail_source"] == THUMBNAIL_SOURCE
    assert "enc" not in claims


def test_sealed_claims_are_decrypted_once_per_token(monkeypatch):
    from app import auth

    secret = "s" * 32
    token = make_v4_token(secret)
    calls = []
    real_aesgcm = auth.AESGCM

    class CountingAESGCM:
        def __init__(self, key):
            self._inner = real_aesgcm(key)

        def decrypt(self, *args, **kwargs):
            calls.append(1)
            return self._inner.decrypt(*args, **kwargs)

    auth._decrypt_sealed_claims_cached.cache_clear()
    monkeypatch.setattr(auth, "AESGCM", CountingAESGCM)
    first = validate(token, secret, "cbioportal-wsi")
    for _ in range(5):
        assert validate(token, secret, "cbioportal-wsi") == first
    assert len(calls) == 1
    # Callers get their own copy; mutating it does not poison the cache.
    first["tile_source"] = "tampered"
    assert validate(token, secret, "cbioportal-wsi")["tile_source"] == TILE_SOURCE


def test_rejected_sealed_claims_are_not_cached():
    from app import auth

    auth._decrypt_sealed_claims_cached.cache_clear()
    with pytest.raises(InvalidWsiToken):
        decrypt_sealed_claims("A" * 64, SEAL_KEY, SLIDE_KEY)
    assert auth._decrypt_sealed_claims_cached.cache_info().currsize == 0


def test_source_seal_key_decodes_contract_vector():
    assert decode_source_seal_key(VECTOR_SEAL_KEY_B64).hex() == VECTOR_KEY_HEX


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        base64.b64encode(bytes(31)).decode(),
        base64.b64encode(bytes(33)).decode(),
        base64.b64encode(bytes(16)).decode(),
        VECTOR_SEAL_KEY_B64.replace("+", "-"),
        VECTOR_SEAL_KEY_B64.rstrip("="),
        "not base64 at all!",
        VECTOR_KEY_HEX,
    ],
)
def test_malformed_source_seal_key_is_rejected_without_echo(value):
    with pytest.raises(InvalidWsiToken, match="seal key is not configured") as exc_info:
        decode_source_seal_key(value)
    if value.strip():
        assert value.strip() not in str(exc_info.value)


def test_contract_enc_vector_decrypts_to_exact_plaintext():
    assert seal_claims(VECTOR_KEY, VECTOR_SLIDE_KEY, VECTOR_PLAINTEXT) == VECTOR_ENC
    sealed = base64.urlsafe_b64decode(VECTOR_ENC + "=" * (-len(VECTOR_ENC) % 4))
    assert sealed[:12].hex() == "000102030405060708090a0b"
    plaintext = AESGCM(VECTOR_KEY).decrypt(
        sealed[:12], sealed[12:], VECTOR_SLIDE_KEY.encode()
    )
    assert plaintext.decode("utf-8") == VECTOR_PLAINTEXT
    seal_key = decode_source_seal_key(VECTOR_SEAL_KEY_B64)
    assert decrypt_sealed_claims(VECTOR_ENC, seal_key, VECTOR_SLIDE_KEY) == {
        "image_id": "1",
        "tile_source": "file:///app/testdata/1.svs",
        "thumbnail_source": "file:///app/testdata/1.jpg",
    }


@pytest.mark.parametrize(
    ("enc", "seal_key", "slide_key"),
    [
        (VECTOR_ENC, VECTOR_KEY, "f" * 32),
        (VECTOR_ENC, VECTOR_KEY, VECTOR_SLIDE_KEY.upper()),
        (VECTOR_ENC, SEAL_KEY, VECTOR_SLIDE_KEY),
        (VECTOR_ENC, VECTOR_KEY[:16], VECTOR_SLIDE_KEY),
        (VECTOR_ENC[:-2] + ("AA" if VECTOR_ENC[-2:] != "AA" else "BB"), VECTOR_KEY, VECTOR_SLIDE_KEY),
        (VECTOR_ENC[:16] + ("A" if VECTOR_ENC[16] != "A" else "B") + VECTOR_ENC[17:], VECTOR_KEY, VECTOR_SLIDE_KEY),
        (VECTOR_ENC[:20], VECTOR_KEY, VECTOR_SLIDE_KEY),
        (VECTOR_ENC + "==", VECTOR_KEY, VECTOR_SLIDE_KEY),
        ("", VECTOR_KEY, VECTOR_SLIDE_KEY),
        (None, VECTOR_KEY, VECTOR_SLIDE_KEY),
    ],
)
def test_tampered_or_rebound_enc_is_rejected(enc, seal_key, slide_key):
    with pytest.raises(InvalidWsiToken) as exc_info:
        decrypt_sealed_claims(enc, seal_key, slide_key)
    assert "testdata" not in str(exc_info.value)
    assert VECTOR_SLIDE_KEY not in str(exc_info.value)


def test_token_with_enc_bound_to_another_slide_key_is_rejected():
    secret = "s" * 32
    claims = valid_claims()
    claims["slide_key"] = "f" * 32
    with pytest.raises(InvalidWsiToken, match="source binding"):
        validate(make_token(secret, **claims), secret, "cbioportal-wsi")


@pytest.mark.parametrize(
    "sealed",
    [
        "[]",
        "not json",
        {"image_id": "slide-a", "tile_source": TILE_SOURCE},
        {"image_id": "", "tile_source": TILE_SOURCE, "thumbnail_source": THUMBNAIL_SOURCE},
        {"image_id": 1, "tile_source": TILE_SOURCE, "thumbnail_source": THUMBNAIL_SOURCE},
    ],
)
def test_malformed_sealed_plaintext_is_rejected(sealed):
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken, match="source binding"):
        validate(
            make_v4_token(secret, enc=seal_claims(SEAL_KEY, SLIDE_KEY, sealed)),
            secret,
            "cbioportal-wsi",
        )


def test_v2_and_v3_tokens_are_rejected():
    secret = "s" * 32
    now = int(time.time())
    v2_claims = {
        "sub": "user@example.org",
        "aud": "cbioportal-wsi",
        "scope": "wsi:read",
        "study_id": "study-a",
        "image_id": "slide-a",
        "wsi_auth_version": 2,
        "tile_source_sha256": source_digest(TILE_SOURCE),
        "thumbnail_source_sha256": source_digest(THUMBNAIL_SOURCE),
        "thumbnail_width": 1024,
        "thumbnail_height": 768,
        "iat": now,
        "exp": now + 300,
    }
    with pytest.raises(InvalidWsiToken, match="unsupported WSI authorization contract"):
        validate(make_token(secret, **v2_claims), secret, "cbioportal-wsi")
    with pytest.raises(InvalidWsiToken, match="unsupported WSI authorization contract"):
        validate(make_v4_token(secret, wsi_auth_version=2), secret, "cbioportal-wsi")
    with pytest.raises(InvalidWsiToken, match="unsupported WSI authorization contract"):
        validate(make_v4_token(secret, wsi_auth_version=3), secret, "cbioportal-wsi")
    with pytest.raises(InvalidWsiToken, match="unsupported WSI authorization contract"):
        validate(make_v4_token(secret, wsi_auth_version="4"), secret, "cbioportal-wsi")


def test_enc_sealed_with_another_key_is_rejected():
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken, match="source binding"):
        validate(make_v4_token(secret, seal_key=bytes(32)), secret, "cbioportal-wsi")


def test_enc_sealed_with_key_derived_from_signing_secret_is_rejected():
    secret = "s" * 32
    derived = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"wsi-claim-enc-v3"
    ).derive(secret.encode("utf-8"))
    with pytest.raises(InvalidWsiToken, match="source binding"):
        validate(make_v4_token(secret, seal_key=derived), secret, "cbioportal-wsi")


def test_signing_secret_does_not_open_enc():
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken):
        validate(make_v4_token(secret), secret, "cbioportal-wsi", seal_key=secret.encode())


@pytest.mark.parametrize(
    "claim",
    ["image_id", "tile_source", "thumbnail_source"],
)
def test_token_must_not_expose_sealed_claims_in_plaintext(claim):
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken, match="exposes sealed"):
        validate(
            make_v4_token(secret, **{claim: "a" * 64}), secret, "cbioportal-wsi"
        )


@pytest.mark.parametrize(
    "slide_key", [None, "", "0123456789ABCDEF0123456789ABCDEF", "0" * 31, "0" * 33, 12345]
)
def test_malformed_slide_key_is_rejected(slide_key):
    secret = "s" * 32
    claims = valid_claims()
    claims["slide_key"] = slide_key
    with pytest.raises(InvalidWsiToken, match="invalid token slide"):
        validate(make_token(secret, **claims), secret, "cbioportal-wsi")


def test_source_cache_identity_changes_when_manifest_fingerprint_changes():
    source = "s3://slides/slide-a.svs"
    assert source_cache_identity(source) == source_digest(source)
    assert source_cache_identity(source, "a" * 64) != source_cache_identity(source, "b" * 64)
    assert source_cache_identity(source, "a" * 64) == source_cache_identity(source, "a" * 64)


def test_source_fingerprint_is_optional():
    secret = "s" * 32
    claims = validate(make_v4_token(secret), secret, "cbioportal-wsi")
    assert "tile_source_fingerprint" not in claims


@pytest.mark.parametrize("claim", ["tile_source_fingerprint", "thumbnail_source_fingerprint"])
def test_malformed_source_fingerprint_is_rejected(claim):
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken):
        validate(
            make_v4_token(secret, **{claim: "not-a-fingerprint"}),
            secret,
            "cbioportal-wsi",
        )


@pytest.mark.parametrize(
    ("secret", "audience", "max_ttl", "seal_key"),
    [
        ("s" * 31, "cbioportal-wsi", 300, SEAL_KEY),
        ("s" * 32, "   ", 300, SEAL_KEY),
        ("s" * 32, "cbioportal-wsi", 0, SEAL_KEY),
        ("s" * 32, "cbioportal-wsi", 301, SEAL_KEY),
        ("s" * 32, "cbioportal-wsi", 900, SEAL_KEY),
        ("s" * 32, "cbioportal-wsi", 300, b""),
        ("s" * 32, "cbioportal-wsi", 300, SEAL_KEY[:31]),
        ("s" * 32, "cbioportal-wsi", 300, SEAL_KEY + b"x"),
        ("s" * 32, "cbioportal-wsi", 300, SEAL_KEY_B64),
    ],
)
def test_invalid_wsi_auth_configuration_is_rejected(secret, audience, max_ttl, seal_key):
    with pytest.raises(InvalidWsiToken, match="not configured"):
        validate_wsi_auth_configuration(secret, audience, max_ttl, seal_key)


def test_token_validation_requires_seal_key():
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken, match="seal key is not configured"):
        validate(make_v4_token(secret), secret, "cbioportal-wsi", seal_key=b"")


@pytest.mark.parametrize("change", [
    {"scope": "wsi:write"},
    {"aud": "other-service"},
    {"exp": int(time.time()) - 1},
])
def test_invalid_claims_are_rejected(change):
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken):
        validate(make_v4_token(secret, **change), secret, "cbioportal-wsi")


def test_wrong_secret_is_rejected():
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken):
        validate(make_v4_token(secret), "x" * 32, "cbioportal-wsi")


def test_non_object_header_and_payload_are_rejected():
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken):
        validate(make_raw_token(secret, [], {}), secret, "cbioportal-wsi")
    with pytest.raises(InvalidWsiToken):
        validate(
            make_raw_token(secret, {"alg": "HS256", "typ": "JWT"}, []),
            secret,
            "cbioportal-wsi",
        )


@pytest.mark.parametrize("change", [
    {"study_id": ""},
    {"slide_key": ""},
    {"enc": ""},
    {"thumbnail_width": 0},
    {"exp": int(time.time()) + 1000},
])
def test_source_bound_claims_and_max_ttl_are_required(change):
    secret = "s" * 32
    with pytest.raises(InvalidWsiToken):
        validate(make_v4_token(secret, **change), secret, "cbioportal-wsi")


def test_token_lifetime_cannot_exceed_configured_limit():
    secret = "s" * 32
    now = int(time.time())
    with pytest.raises(InvalidWsiToken):
        validate(
            make_v4_token(secret, iat=now, exp=now + 11),
            secret,
            "cbioportal-wsi",
            max_ttl=10,
        )
