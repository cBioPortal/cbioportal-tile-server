import base64
import json
import os
import stat

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tools import build_keep_manifest as builder

SEAL_KEY = bytes(range(32))
IMAGE_ID = "1234567"


def _seal(slide_key: str, tile_source: str, image_id: str = IMAGE_ID) -> str:
    claims = {"image_id": image_id, "tile_source": tile_source, "thumbnail_source": tile_source}
    nonce = os.urandom(12)
    sealed = nonce + AESGCM(SEAL_KEY).encrypt(nonce, json.dumps(claims).encode(), slide_key.encode())
    return base64.urlsafe_b64encode(sealed).decode().rstrip("=")


def _row(slide_key: str, source: str, **extra) -> dict:
    return {
        "study_id": "mskimpact",
        "slide_key": slide_key,
        "sealed_source": _seal(slide_key, source, extra.pop("image_id", IMAGE_ID)),
        "slide_width": "59759",
        "slide_height": 42622,
        **extra,
    }


def test_image_id_is_the_numeric_file_name():
    assert builder.image_id_from_source(f"s3://mskmind-bkt/reef-slides/{IMAGE_ID}.svs") == IMAGE_ID
    assert builder.image_id_from_source(f"s3://pathology/study/folder/{IMAGE_ID}.svs") == IMAGE_ID
    assert builder.image_id_from_source("s3://pathology/study/S12-345.svs") is None
    assert builder.image_id_from_source(f"https://host/{IMAGE_ID}.svs") is None


def test_manifest_rows_carry_image_id_and_counts(tmp_path, monkeypatch, capsys):
    rows = [
        _row("aaaaaaaaaaaa", f"s3://mskmind-bkt/reef-slides/{IMAGE_ID}.svs"),
        _row("bbbbbbbbbbbb", "s3://pathology/study/S12-345.svs"),
        _row("cccccccccccc", "s3://mskmind-bkt/reef-slides/7654321.svs", image_id="7654321"),
        _row("dddddddddddd", f"s3://mskmind-bkt/reef-slides/{IMAGE_ID}.svs", image_id="1111111"),
        {**_row("eeeeeeeeeeee", f"s3://x/{IMAGE_ID}.svs"), "sealed_source": "not-sealed"},
        _row("aaaaaaaaaaaa", f"s3://mskmind-bkt/reef-slides/{IMAGE_ID}.svs"),
    ]
    rows_path = tmp_path / "rows.jsonl"
    rows_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    index_path = tmp_path / "wds_index.json"
    index_path.write_text(json.dumps({IMAGE_ID: {"shard_file": "X/000001.tar"}}), encoding="utf-8")
    env_path = tmp_path / "stack.env"
    env_path.write_text(
        "OTHER=1\nWSI_SOURCE_SEAL_KEY=" + base64.b64encode(SEAL_KEY).decode() + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "research" / "manifest.json"

    assert builder.main([
        "--rows", str(rows_path),
        "--seal-key-env-file", str(env_path),
        "--wds-index", str(index_path),
        "--out", str(out),
    ]) == 0

    printed = capsys.readouterr()
    assert IMAGE_ID not in printed.out + printed.err
    assert rows[0]["sealed_source"] not in printed.out + printed.err
    counts = dict(line.split(": ") for line in printed.out.strip().splitlines())
    assert counts == {
        "considered": "6",
        "image_id_derived": "2",
        "in_wds_index": "1",
        "written": "1",
        "skipped_duplicate": "1",
        "skipped_unsealable": "1",
        "skipped_not_derivable": "1",
        "skipped_image_id_mismatch": "1",
        "skipped_no_features": "1",
    }
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest == {
        "version": 1,
        "slides": [
            {
                "study_id": "mskimpact",
                "slide_id": "aaaaaaaaaaaa",
                "slide_width": 59759,
                "slide_height": 42622,
                "models": [
                    {
                        "id": "reef_v2_hoptimus1_keep",
                        "provider": "keep_adapter",
                        "image_id": IMAGE_ID,
                        "slide_width": 59759,
                        "slide_height": 42622,
                        "capabilities": ["text_search"],
                    }
                ],
            }
        ],
    }
    assert stat.S_IMODE(out.stat().st_mode) == 0o640
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o750


def test_invalid_seal_key_is_not_echoed(tmp_path, capsys):
    env_path = tmp_path / "stack.env"
    env_path.write_text("WSI_SOURCE_SEAL_KEY=short-secret-value\n", encoding="utf-8")
    rows_path = tmp_path / "rows.jsonl"
    rows_path.write_text("", encoding="utf-8")
    assert builder.main([
        "--rows", str(rows_path),
        "--seal-key-env-file", str(env_path),
        "--out", str(tmp_path / "m.json"),
    ]) == 2
    assert "short-secret-value" not in capsys.readouterr().err
