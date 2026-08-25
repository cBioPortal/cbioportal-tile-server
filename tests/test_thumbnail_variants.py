from io import BytesIO

from PIL import Image

from tools.generate_thumbnail_variants import _fit_thumbnail
from tools.generate_thumbnail_variants import _join_uri
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
