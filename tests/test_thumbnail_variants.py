from io import BytesIO

from PIL import Image

from tools.generate_thumbnail_variants import _fit_thumbnail
from tools.generate_thumbnail_variants import _join_uri
from tools.generate_thumbnail_variants import _publish
from tools.generate_thumbnail_variants import _registry_batch_query
from tools.generate_thumbnail_variants import _registry_query
from tools.generate_thumbnail_variants import _render
from tools.generate_thumbnail_variants import variant_root_for_master


def test_fit_thumbnail_preserves_aspect_ratio_inside_navigation_box():
    source = Image.new("RGB", (1024, 512), (120, 140, 160))
    payload = BytesIO()
    source.save(payload, format="JPEG")

    result, width, height = _fit_thumbnail(payload.getvalue())

    assert (width, height) == (128, 64)
    with Image.open(BytesIO(result)) as image:
        assert image.size == (128, 64)


def test_fit_thumbnail_handles_portrait_images_without_upscaling():
    source = Image.new("RGB", (256, 512), (120, 140, 160))
    payload = BytesIO()
    source.save(payload, format="JPEG")

    result, width, height = _fit_thumbnail(payload.getvalue())

    assert (width, height) == (48, 96)
    with Image.open(BytesIO(result)) as image:
        assert image.size == (48, 96)


def test_variant_keys_are_bound_to_the_master_manifest_version():
    assert variant_root_for_master(
        "s3://mskmind-bkt/wsi-thumbnails/masters"
    ) == "s3://mskmind-bkt/wsi-thumbnails/variants/nav-128x96"
    assert _join_uri(
        "s3://mskmind-bkt/wsi-thumbnails/variants/nav-128x96",
        "1000035",
        "20260824235900000000",
    ) == (
        "s3://mskmind-bkt/wsi-thumbnails/variants/nav-128x96/"
        "20260824235900000000/1000035.jpg"
    )


def test_variant_render_is_idempotent_and_reuses_existing_derivative(tmp_path):
    source = Image.new("RGB", (1024, 512), (120, 140, 160))
    master = tmp_path / "masters" / "1000035.jpg"
    master.parent.mkdir()
    source.save(master, format="JPEG")
    row = {
        "image_id": "1000035",
        "source_path": "s3://slides/1000035.svs",
        "artifact_uri": str(master),
        "manifest_version": "20260824235900000000",
    }

    created = _render(row, str(tmp_path / "variants"), force=False)
    reused = _render(row, str(tmp_path / "variants"), force=False)

    assert created["skipped"] is False
    assert reused["skipped"] is True
    assert reused["serving_artifact_uri"] == created["serving_artifact_uri"]
    assert (reused["serving_width"], reused["serving_height"]) == (128, 64)


def test_registry_batches_are_keyset_paginated():
    query = _registry_batch_query("1000035", 5000)

    assert "CAST(image_id AS STRING) > '1000035'" in query
    assert (
        "ORDER BY CAST(image_id AS STRING), COALESCE(source_path, '') LIMIT 5000"
        in query
    )
    assert "current_inventory" in query


def test_registry_batches_continue_after_same_image_source_path():
    query = _registry_batch_query("1000035", 5000, "s3://slides/1000035.svs")

    assert "CAST(image_id AS STRING) = '1000035'" in query
    assert "COALESCE(source_path, '') > 's3://slides/1000035.svs'" in query


def test_registry_query_uses_configured_inventory_path_column_everywhere():
    query = _registry_query(
        registry_table="cdsi_dev.wsi_test.slide_thumbnail_registry",
        inventory_table="cdsi_dev.wsi_test.wsi_source_snapshot",
        inventory_path_column="source_url",
    )

    assert "inventory.source_url AS path" in query
    assert "inventory.source_url IS NOT NULL" in query
    assert "inventory.source_url LIKE 's3://%'" in query
    assert "AND path IS NOT NULL" not in query


def test_variant_publish_is_bound_to_manifest_version(monkeypatch):
    statements = []
    monkeypatch.setattr(
        "tools.generate_thumbnail_variants.run_statement",
        lambda statement, warehouse_id: statements.append((statement, warehouse_id)),
    )

    _publish(
        "warehouse",
        [
            {
                "image_id": "1000035",
                "source_path": "s3://slides/1000035.svs",
                "manifest_version": "v2",
                "serving_artifact_uri": "s3://variants/v2/1000035.jpg",
                "serving_width": 128,
                "serving_height": 96,
            }
        ],
        "cdsi_dev.wsi_test.slide_thumbnail_registry",
    )

    sql, warehouse_id = statements[0]
    assert warehouse_id == "warehouse"
    assert "manifest_version" in sql
    assert (
        "COALESCE(target.manifest_version, '') = "
        "COALESCE(source.manifest_version, '')"
        in sql
    )
    assert "cdsi_dev.wsi_test.slide_thumbnail_registry" in sql
