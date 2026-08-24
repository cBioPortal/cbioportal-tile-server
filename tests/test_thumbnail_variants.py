from io import BytesIO

from PIL import Image

from tools.generate_thumbnail_variants import _fit_thumbnail


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
