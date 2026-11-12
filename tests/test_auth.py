import base64
import json

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import auth
from app.auth import (
    InvalidSealedSource,
    decode_source_seal_key,
    decrypt_sealed_claims,
)

SLIDE_KEY = "0123456789ab"
TILE_SOURCE = "s3://slides/slide-a.svs"
THUMBNAIL_SOURCE = "s3://thumbs/slide-a.jpg"
SEAL_KEY = bytes(range(32))
SEAL_KEY_B64 = base64.b64encode(SEAL_KEY).decode()

# Contract test vector (docs/wsi-deid-slide-key-contract.md, V6.1).
VECTOR_SEAL_KEY_B64 = "8XaidWVtlFzV7hwyAwtTNjN13MNLqnenz5Sy7Y+cbAs="
VECTOR_SLIDE_KEY = "0123456789ab"
VECTOR_KEY_HEX = "f176a275656d945cd5ee1c32030b53363375dcc34baa77a7cf94b2ed8f9c6c0b"
VECTOR_KEY = bytes.fromhex(VECTOR_KEY_HEX)
VECTOR_PLAINTEXT = (
    '{"image_id":"1","tile_source":"file:///app/testdata/1.svs",'
    '"thumbnail_source":"file:///app/testdata/1.jpg"}'
)
VECTOR_ENC = (
    "AAECAwQFBgcICQoLNpOTYnY8HVb-KcPzu1F5HPykr7D0YY_UhVbbyjOFRlxC63fxCt09YO1aYC-phb85"
    "wDhN5PPPpC0X46RSD0K0bRRgSptRb9wDiqMLtftFQ6VpBGfGILaddEV_s-Zsmpp28fG5Z3XTNWnyoBRT"
    "AkaMITic3pNdzkC_lpw5"
)


def seal_claims(
    seal_key: bytes,
    slide_key: str,
    sealed: dict | str,
    nonce: bytes = bytes(range(12)),
) -> str:
    """Produce an `enc` exactly as the data provider seals it (contract V6.1)."""
    plaintext = sealed if isinstance(sealed, str) else json.dumps(sealed, separators=(",", ":"))
    ciphertext = AESGCM(seal_key).encrypt(
        nonce, plaintext.encode("utf-8"), slide_key.encode("utf-8")
    )
    return base64.urlsafe_b64encode(nonce + ciphertext).rstrip(b"=").decode()


def make_enc(
    seal_key: bytes = SEAL_KEY, slide_key: str = SLIDE_KEY, **overrides
) -> str:
    """A sealed source for `slide_key` with the default test sources."""
    return seal_claims(
        seal_key,
        slide_key,
        {
            "image_id": "slide-a",
            "tile_source": TILE_SOURCE,
            "thumbnail_source": THUMBNAIL_SOURCE,
            **overrides,
        },
    )


@pytest.fixture(autouse=True)
def _clear_sealed_source_cache():
    auth._decrypt_sealed_claims_cached.cache_clear()
    yield
    auth._decrypt_sealed_claims_cached.cache_clear()


def test_sealed_source_opens_under_its_key():
    assert decrypt_sealed_claims(make_enc(), SEAL_KEY, SLIDE_KEY) == {
        "image_id": "slide-a",
        "tile_source": TILE_SOURCE,
        "thumbnail_source": THUMBNAIL_SOURCE,
    }


@pytest.mark.parametrize(
    "slide_key",
    ["0123456789ab", "0123456789AB", "cycif-roi_17", "k" * 128],
)
def test_any_resource_key_binds_its_sealed_source(slide_key):
    enc = make_enc(slide_key=slide_key)
    assert decrypt_sealed_claims(enc, SEAL_KEY, slide_key)["tile_source"] == TILE_SOURCE
    with pytest.raises(InvalidSealedSource):
        decrypt_sealed_claims(enc, SEAL_KEY, "other-key")


def test_sealed_source_is_decrypted_once_per_enc(monkeypatch):
    enc = make_enc()
    calls = []
    real_aesgcm = auth.AESGCM

    class CountingAESGCM:
        def __init__(self, key):
            self._inner = real_aesgcm(key)

        def decrypt(self, *args, **kwargs):
            calls.append(1)
            return self._inner.decrypt(*args, **kwargs)

    monkeypatch.setattr(auth, "AESGCM", CountingAESGCM)
    first = decrypt_sealed_claims(enc, SEAL_KEY, SLIDE_KEY)
    for _ in range(5):
        assert decrypt_sealed_claims(enc, SEAL_KEY, SLIDE_KEY) == first
    assert len(calls) == 1
    # Callers get their own copy; mutating it does not poison the cache.
    first["tile_source"] = "tampered"
    assert decrypt_sealed_claims(enc, SEAL_KEY, SLIDE_KEY)["tile_source"] == TILE_SOURCE


@pytest.mark.parametrize(
    "seal_key", [None, b"", SEAL_KEY[:31], SEAL_KEY + b"x", SEAL_KEY_B64]
)
def test_opening_requires_a_valid_seal_key(seal_key):
    with pytest.raises(InvalidSealedSource, match="seal key is not configured"):
        decrypt_sealed_claims(make_enc(), seal_key, SLIDE_KEY)


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
    with pytest.raises(InvalidSealedSource, match="seal key is not configured") as exc_info:
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
        (VECTOR_ENC, VECTOR_KEY, "f" * 12),
        (VECTOR_ENC, VECTOR_KEY, "0123456789abcdef0123456789abcdef"),
        (VECTOR_ENC, VECTOR_KEY, VECTOR_SLIDE_KEY.upper()),
        # Sealed with another key.
        (VECTOR_ENC, SEAL_KEY, VECTOR_SLIDE_KEY),
        (VECTOR_ENC, bytes(32), VECTOR_SLIDE_KEY),
        (VECTOR_ENC, b"s" * 32, VECTOR_SLIDE_KEY),
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
    with pytest.raises(InvalidSealedSource) as exc_info:
        decrypt_sealed_claims(enc, seal_key, slide_key)
    assert "testdata" not in str(exc_info.value)
    assert VECTOR_SLIDE_KEY not in str(exc_info.value)


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
    with pytest.raises(InvalidSealedSource, match="source binding"):
        decrypt_sealed_claims(seal_claims(SEAL_KEY, SLIDE_KEY, sealed), SEAL_KEY, SLIDE_KEY)
