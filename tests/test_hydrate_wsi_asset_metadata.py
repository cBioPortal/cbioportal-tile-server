from tools.hydrate_wsi_asset_metadata import hydrate_rows


METADATA = (
    '{"dimensions":{"width":1024,"height":768},"levels":1,'
    '"level_dimensions":[{"width":1024,"height":768}],'
    '"max_zoom":0,"tile_size":256}'
)


def _row(image_id: str, source: str = ""):
    return {
        "patient_id": "P-1",
        "image_id": image_id,
        "slide_path": source or None,
        "can_serve_tiles": False,
        "tile_metadata_json": None,
        "thumbnail_url": None,
        "thumbnail_width": None,
        "thumbnail_height": None,
        "thumbnail_content_type": None,
    }


def _record(image_id: str, source: str = "s3://slides/1.svs"):
    return {
        "image_id": image_id,
        "source_path": source,
        "artifact_uri": f"s3://thumbs/{image_id}.jpg",
        "width": 1024,
        "height": 768,
        "content_type": "image/jpeg",
        "tile_metadata_json": METADATA,
        "status": "success",
    }


def test_hydrates_complete_registry_rows_and_keeps_source_identity():
    rows, stats = hydrate_rows([_row("1")], {"1": _record("1")})

    assert stats == {
        "rows": 1,
        "hydrated": 1,
        "unchanged": 0,
        "incomplete": 0,
        "source_mismatch": 0,
    }
    assert rows[0]["can_serve_tiles"] is True
    assert rows[0]["slide_path"] == "s3://slides/1.svs"
    assert rows[0]["thumbnail_url"] == "s3://thumbs/1.jpg"


def test_does_not_mark_failed_registry_row_servable():
    record = _record("1")
    record["status"] = "failed"
    rows, stats = hydrate_rows([_row("1")], {"1": record})

    assert stats["incomplete"] == 1
    assert rows[0]["can_serve_tiles"] is False
    assert rows[0]["slide_path"] is None


def test_does_not_mark_invalid_metadata_servable():
    record = _record("1")
    record["tile_metadata_json"] = '{"dimensions":{"width":0,"height":0}}'
    rows, stats = hydrate_rows([_row("1")], {"1": record})

    assert stats["incomplete"] == 1
    assert rows[0]["can_serve_tiles"] is False


def test_does_not_replace_an_existing_source_with_a_different_registry_source():
    rows, stats = hydrate_rows(
        [_row("1", "s3://other-slides/1.svs")],
        {"1": _record("1")},
    )

    assert stats["source_mismatch"] == 1
    assert rows[0]["can_serve_tiles"] is False
    assert rows[0]["slide_path"] == "s3://other-slides/1.svs"
