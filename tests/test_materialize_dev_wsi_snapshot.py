import pytest

from tools import materialize_dev_wsi_snapshot as module
from tools.generate_slide_thumbnails import RegistryRow
from tools.materialize_dev_wsi_snapshot import (
    _complete_registry_rows,
    _expected_manifest_uri,
    _literal,
    _load_registry,
    _namespace,
    _source_rows,
    _validate_registry_artifacts,
    _validate_manifest_uri,
)


def test_dev_namespace_rejects_production_namespace():
    with pytest.raises(ValueError):
        _namespace("cdsi_prod.pathology_data_mining")


def test_namespace_requires_two_simple_identifiers():
    assert _namespace("cdsi_dev.wsi_test") == ("cdsi_dev", "wsi_test")
    with pytest.raises(ValueError):
        _namespace("cdsi_dev.wsi_test.extra")


def test_source_rows_use_validated_slide_path_as_source_url():
    rows = list(
        _source_rows(
            [
                {
                    "patient_id": "P-1",
                    "image_id": "1",
                    "slide_path": "s3://bucket/1.svs",
                    "is_hne": True,
                }
            ]
        )
    )
    assert rows[0]["source_url"] == "s3://bucket/1.svs"
    assert rows[0]["is_hne"] is True


def test_sql_literals_escape_text_and_preserve_booleans():
    assert _literal("O'Brien") == "'O''Brien'"
    assert _literal(True, "bool") == "TRUE"
    assert _literal("FALSE", "bool") == "FALSE"
    assert _literal(None) == "NULL"


def test_failed_registry_rows_with_null_dimensions_are_loadable(tmp_path, monkeypatch):
    registry_path = tmp_path / "results.jsonl"
    registry_path.write_text(
        '{"image_id":"1","source_path":"s3://slides/1.svs",'
        '"artifact_uri":"s3://thumbs-dev/1.jpg","width":null,"height":null,'
        '"status":"failed","rendered_at":"2026-08-17 00:00:00"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(module.meta_store, "run_statement", lambda *args: None)

    rows = _load_registry(
        "warehouse",
        "cdsi_dev.wsi_test.slide_thumbnail_registry",
        registry_path,
        100,
    )

    assert rows[0].status == "failed"
    assert rows[0].width == 0
    assert rows[0].height == 0


def test_dev_artifact_root_rejects_production_registry_rows():
    with pytest.raises(ValueError, match="outside the dev artifact root"):
        _validate_registry_artifacts(
            [
                {
                    "image_id": "1",
                    "artifact_uri": "s3://production-thumbnails/1.jpg",
                    "status": "success",
                }
            ],
            "s3://dev-thumbnails/masters",
        )

    _validate_registry_artifacts(
        [
            {
                "image_id": "1",
                "artifact_uri": "s3://dev-thumbnails/masters/1.jpg",
                "status": "success",
            }
        ],
        "s3://dev-thumbnails/masters",
    )


def test_dev_manifest_must_live_next_to_the_artifact_root():
    root = "s3://dev-thumbnails/wsi/masters"
    assert _expected_manifest_uri(root) == "s3://dev-thumbnails/wsi/manifest.json"
    _validate_manifest_uri("s3://dev-thumbnails/wsi/manifest.json", root)
    with pytest.raises(ValueError, match="sibling manifest.json"):
        _validate_manifest_uri("s3://production/manifest.json", root)


def test_manifest_rows_require_current_complete_successes():
    source_rows = [
        {"image_id": "1", "slide_path": "s3://slides/1.svs"},
        {"image_id": "2", "slide_path": "s3://slides/2.svs"},
        {"image_id": "3", "slide_path": "s3://slides/3.svs"},
    ]
    registry_rows = [
        RegistryRow(
            image_id="1",
            source_path="s3://slides/1.svs",
            artifact_uri="s3://dev-thumbnails/masters/1.jpg",
            width=100,
            height=80,
            content_type="image/jpeg",
            status="success",
            rendered_at="2026-08-17T00:00:00+00:00",
            error_message="",
            manifest_version="v1",
            tile_metadata_json='{"width":100}',
        ),
        RegistryRow(
            image_id="2",
            source_path="s3://slides/2.svs",
            artifact_uri="s3://dev-thumbnails/masters/2.jpg",
            width=0,
            height=0,
            content_type="image/jpeg",
            status="failed",
            rendered_at="2026-08-17T00:00:00+00:00",
            error_message="render failed",
            manifest_version="v1",
        ),
        RegistryRow(
            image_id="3",
            source_path="s3://old-slides/3.svs",
            artifact_uri="s3://dev-thumbnails/masters/3.jpg",
            width=100,
            height=80,
            content_type="image/jpeg",
            status="success",
            rendered_at="2026-08-17T00:00:00+00:00",
            error_message="",
            manifest_version="v1",
            tile_metadata_json='{"width":100}',
        ),
    ]

    complete = _complete_registry_rows(source_rows, registry_rows)

    assert [row.image_id for row in complete] == ["1"]
