"""
ZXY tile coordinate math and tile extraction.

Coordinate convention: z=0 is lowest resolution (whole slide in ~1 tile),
increasing z = increasing detail. x=0, y=0 is top-left. This matches the
convention used by OpenLayers, Leaflet, and the IIIF Image API.

Tile size is always TILE_SIZE × TILE_SIZE pixels. Edge tiles are padded with
white so callers never have to handle partial tiles.
"""

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageDraw
from tiffslide import TiffSlide

from .config import settings
from .metrics import DECODE_SOURCE_PIXELS

TILE_SIZE = settings.tile_size


class OverviewTooLarge(RuntimeError):
    """Raised when an overview decode would exceed the configured pixel budget."""

    def __init__(
        self,
        *,
        z: int,
        x: int,
        y: int,
        best_level: int,
        level_downsample: float,
        read_width: int,
        read_height: int,
        requested_pixels: int,
    ) -> None:
        self.z = z
        self.x = x
        self.y = y
        self.best_level = best_level
        self.level_downsample = level_downsample
        self.read_width = read_width
        self.read_height = read_height
        self.requested_pixels = requested_pixels
        self.max_decode_pixels = settings.max_decode_pixels
        super().__init__(
            "overview decode requires preprocessing "
            f"(z={z} x={x} y={y} level={best_level} "
            f"read={read_width}x{read_height} pixels={requested_pixels} "
            f"limit={self.max_decode_pixels})"
        )


class NoSafeThumbnailOverview(RuntimeError):
    """Raised when no thumbnail overview level fits the configured budget."""

    def __init__(
        self,
        *,
        level: int,
        level_width: int,
        level_height: int,
        requested_pixels: int,
    ) -> None:
        self.level = level
        self.level_width = level_width
        self.level_height = level_height
        self.requested_pixels = requested_pixels
        self.max_decode_pixels = settings.thumbnail_max_decode_pixels
        super().__init__(
            "thumbnail overview requires preprocessing "
            f"(level={level} read={level_width}x{level_height} "
            f"pixels={requested_pixels} limit={self.max_decode_pixels})"
        )


@dataclass(frozen=True)
class DecodePlan:
    z: int
    x: int
    y: int
    x0: int
    y0: int
    out_width: int
    out_height: int
    best_level: int
    level_downsample: float
    read_width: int
    read_height: int
    requested_pixels: int


@dataclass(frozen=True)
class ThumbnailDecodePlan:
    level: int
    read_width: int
    read_height: int
    requested_pixels: int
    target_width: int
    target_height: int


def max_zoom(slide: TiffSlide) -> int:
    """
    The highest zoom level for this slide.

    At max_zoom, one tile pixel ≈ one level-0 slide pixel (subject to
    rounding to the nearest power-of-two pyramid level).
    """
    w, h = slide.dimensions
    return math.ceil(math.log2(max(w, h) / TILE_SIZE))


def _slide_properties_metadata(slide: TiffSlide) -> tuple[float, float, str, int | None]:
    try:
        props = slide.properties
        # tiffslide uses its own namespace; fall back to openslide for compat
        mpp_x = float(props.get("tiffslide.mpp-x") or props.get("openslide.mpp-x", 0) or 0)
        mpp_y = float(props.get("tiffslide.mpp-y") or props.get("openslide.mpp-y", 0) or 0)
        vendor = props.get("tiffslide.vendor") or props.get("openslide.vendor", "") or ""
        obj_power = props.get("tiffslide.objective-power") or props.get("openslide.objective-power")
        objective_power = int(obj_power) if obj_power is not None else None
        return mpp_x, mpp_y, vendor, objective_power
    except Exception:
        return 0.0, 0.0, "", None


def slide_metadata(slide: TiffSlide) -> dict:
    w, h = slide.dimensions
    mz = max_zoom(slide)
    mpp_x, mpp_y, vendor, objective_power = _slide_properties_metadata(slide)

    return {
        "dimensions": {"width": w, "height": h},
        "levels": slide.level_count,
        "level_dimensions": [
            {"width": lw, "height": lh}
            for lw, lh in slide.level_dimensions
        ],
        "level_downsamples": list(slide.level_downsamples),
        "max_zoom": mz,
        "tile_size": TILE_SIZE,
        "mpp": {"x": mpp_x, "y": mpp_y},
        "objective_power": objective_power,
        "vendor": vendor,
    }


def _tile_geometry(slide: TiffSlide, z: int, x: int, y: int) -> tuple[int, int, int, int, int, int, int, int]:
    mz = max_zoom(slide)
    if z < 0 or z > mz:
        raise ValueError(f"zoom {z} out of range [0, {mz}]")

    target_ds = 2 ** (mz - z)
    slide_w, slide_h = slide.dimensions
    x0 = x * TILE_SIZE * target_ds
    y0 = y * TILE_SIZE * target_ds

    if x0 >= slide_w or y0 >= slide_h:
        raise ValueError(f"tile ({x}, {y}, {z}) is outside slide bounds")

    src_w = min(TILE_SIZE * target_ds, slide_w - x0)
    src_h = min(TILE_SIZE * target_ds, slide_h - y0)
    out_w = math.ceil(src_w / target_ds)
    out_h = math.ceil(src_h / target_ds)
    return mz, target_ds, x0, y0, src_w, src_h, out_w, out_h


def _resize_and_pad(region: Image.Image, out_w: int, out_h: int) -> Image.Image:
    if region.size != (out_w, out_h):
        region = region.resize((out_w, out_h), Image.LANCZOS)

    if (out_w, out_h) != (TILE_SIZE, TILE_SIZE):
        canvas = Image.new("RGB", (TILE_SIZE, TILE_SIZE), (255, 255, 255))
        canvas.paste(region, (0, 0))
        return canvas
    return region


def _plan_decode(slide: TiffSlide, z: int, x: int, y: int) -> DecodePlan:
    _, target_ds, x0, y0, src_w, src_h, out_w, out_h = _tile_geometry(slide, z, x, y)

    best_level = slide.get_best_level_for_downsample(target_ds)
    level_ds = slide.level_downsamples[best_level]

    read_w = math.ceil(src_w / level_ds)
    read_h = math.ceil(src_h / level_ds)

    level_w, level_h = slide.level_dimensions[best_level]
    read_w = min(read_w, level_w - math.floor(x0 / level_ds))
    read_h = min(read_h, level_h - math.floor(y0 / level_ds))

    if read_w <= 0 or read_h <= 0:
        return DecodePlan(
            z=z,
            x=x,
            y=y,
            x0=x0,
            y0=y0,
            out_width=out_w,
            out_height=out_h,
            best_level=best_level,
            level_downsample=level_ds,
            read_width=0,
            read_height=0,
            requested_pixels=0,
        )

    requested_pixels = read_w * read_h
    if requested_pixels > settings.max_decode_pixels:
        raise OverviewTooLarge(
            z=z,
            x=x,
            y=y,
            best_level=best_level,
            level_downsample=level_ds,
            read_width=read_w,
            read_height=read_h,
            requested_pixels=requested_pixels,
        )

    return DecodePlan(
        z=z,
        x=x,
        y=y,
        x0=x0,
        y0=y0,
        out_width=out_w,
        out_height=out_h,
        best_level=best_level,
        level_downsample=level_ds,
        read_width=read_w,
        read_height=read_h,
        requested_pixels=requested_pixels,
    )


def render_tile_image(slide: TiffSlide, z: int, x: int, y: int) -> tuple[Image.Image, DecodePlan]:
    plan = _plan_decode(slide, z, x, y)
    DECODE_SOURCE_PIXELS.observe(plan.requested_pixels)
    if plan.read_width <= 0 or plan.read_height <= 0:
        return Image.new("RGB", (TILE_SIZE, TILE_SIZE), (255, 255, 255)), plan

    region = slide.read_region((plan.x0, plan.y0), plan.best_level, (plan.read_width, plan.read_height))
    region = region.convert("RGB")
    return _resize_and_pad(region, plan.out_width, plan.out_height), plan


def render_overview_image(slide: TiffSlide) -> tuple[Image.Image, DecodePlan]:
    plan = _plan_decode(slide, 0, 0, 0)
    DECODE_SOURCE_PIXELS.observe(plan.requested_pixels)
    if plan.read_width <= 0 or plan.read_height <= 0:
        return Image.new("RGB", (1, 1), (255, 255, 255)), plan

    region = slide.read_region((plan.x0, plan.y0), plan.best_level, (plan.read_width, plan.read_height))
    region = region.convert("RGB")
    if region.size != (plan.out_width, plan.out_height):
        region = region.resize((plan.out_width, plan.out_height), Image.LANCZOS)
    return region, plan


def get_tile_bytes(slide: TiffSlide, z: int, x: int, y: int) -> bytes:
    """
    Extract tile (x, y) at zoom level z and return JPEG bytes.

    Raises ValueError for out-of-range coordinates.
    """
    image, _ = render_tile_image(slide, z, x, y)
    return _encode_jpeg(image)


def get_thumbnail_bytes(slide: TiffSlide, width: int, height: int) -> bytes:
    image, _ = render_overview_image(slide)
    image = image.copy()
    image.thumbnail((width, height), Image.Resampling.LANCZOS)
    return _encode_jpeg(image)


def _plan_thumbnail_decode(
    slide: TiffSlide, width: int, height: int
) -> ThumbnailDecodePlan:
    slide_width, slide_height = slide.dimensions
    scale = min(width / slide_width, height / slide_height, 1.0)
    target_width = max(1, min(slide_width, round(slide_width * scale)))
    target_height = max(1, min(slide_height, round(slide_height * scale)))
    safe_levels: list[tuple[int, int, int, int]] = []
    for level in range(slide.level_count):
        level_width, level_height = slide.level_dimensions[level]
        requested_pixels = level_width * level_height
        if requested_pixels <= settings.thumbnail_max_decode_pixels:
            safe_levels.append((level, level_width, level_height, requested_pixels))

    if not safe_levels:
        fallback_level = slide.level_count - 1
        fallback_width, fallback_height = slide.level_dimensions[fallback_level]
        raise NoSafeThumbnailOverview(
            level=fallback_level,
            level_width=fallback_width,
            level_height=fallback_height,
            requested_pixels=fallback_width * fallback_height,
        )

    for level, level_width, level_height, requested_pixels in reversed(safe_levels):
        if level_width >= target_width and level_height >= target_height:
            return ThumbnailDecodePlan(
                level=level,
                read_width=level_width,
                read_height=level_height,
                requested_pixels=requested_pixels,
                target_width=target_width,
                target_height=target_height,
            )

    level, level_width, level_height, requested_pixels = safe_levels[0]
    return ThumbnailDecodePlan(
        level=level,
        read_width=level_width,
        read_height=level_height,
        requested_pixels=requested_pixels,
        target_width=target_width,
        target_height=target_height,
    )


def render_thumbnail_image(
    slide: TiffSlide, width: int, height: int
) -> tuple[Image.Image, ThumbnailDecodePlan]:
    plan = _plan_thumbnail_decode(slide, width, height)
    DECODE_SOURCE_PIXELS.observe(plan.requested_pixels)
    region = slide.read_region((0, 0), plan.level, (plan.read_width, plan.read_height))
    region = region.convert("RGB")
    region.thumbnail((width, height), Image.Resampling.LANCZOS)
    return region, plan


def get_thumbnail_bytes_with_plan(
    slide: TiffSlide, width: int, height: int
) -> tuple[bytes, ThumbnailDecodePlan]:
    image, plan = render_thumbnail_image(slide, width, height)
    return _encode_jpeg(image), plan


def get_placeholder_thumbnail_bytes(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), (236, 236, 236))
    draw = ImageDraw.Draw(image)
    border = max(1, min(width, height) // 32)
    draw.rectangle((0, 0, width - 1, height - 1), outline=(190, 190, 190), width=border)
    draw.line((0, 0, width - 1, height - 1), fill=(210, 210, 210), width=border)
    draw.line((0, height - 1, width - 1, 0), fill=(210, 210, 210), width=border)
    return _encode_jpeg(image)


def _blank_tile() -> bytes:
    img = Image.new("RGB", (TILE_SIZE, TILE_SIZE), (255, 255, 255))
    return _encode_jpeg(img)


def _encode_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=settings.jpeg_quality, optimize=True)
    return buf.getvalue()
