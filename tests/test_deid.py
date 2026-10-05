import json

import pytest

from app.deid import DeidViolation, validate_artifact_uri, validate_timeline_public_row, validate_wsi_public_row

SLIDE_KEY = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "value",
    ["2021-03-14", "03/14/2021", "14/03/2021", "March 14, 2021", "14 March 2021"],
)
def test_public_wsi_row_rejects_common_absolute_date_formats(value):
    with pytest.raises(DeidViolation):
        validate_wsi_public_row({"IMAGE_ID": "slide-1", "SLIDE_KEY": SLIDE_KEY, "PATH_DX_TITLE": value})


def test_artifact_uri_requires_an_approved_prefix_and_rejects_traversal():
    with pytest.raises(DeidViolation):
        validate_artifact_uri(
            "s3://slides/../patient-123.svs",
            image_id="slide-1",
            kind="source",
            prefixes=("s3://slides/",),
        )
    with pytest.raises(DeidViolation):
        validate_artifact_uri(
            "s3://other/slide-1.svs",
            image_id="slide-1",
            kind="source",
            prefixes=("s3://slides/",),
        )


def test_artifact_uri_allows_pipeline_date_under_approved_prefix():
    validate_artifact_uri(
        "s3://pathology/2026-08-19/slide-1.svs",
        image_id="slide-1",
        kind="source",
        prefixes=("s3://pathology/",),
    )


@pytest.mark.parametrize("field", ["IMAGE_IDS", "IMAGE_ID"])
def test_timeline_rows_reject_real_image_id_columns(field):
    validate_timeline_public_row(
        {
            "PATIENT_ID": "P-1",
            "IMAGE_COUNT": "1",
            "NON_SERVABLE_IMAGE_COUNT": "1",
            "TOTAL_IMAGE_COUNT": "2",
            "START_DATE": "-14",
        }
    )
    with pytest.raises(DeidViolation, match="forbidden timeline field"):
        validate_timeline_public_row(
            {"PATIENT_ID": "P-1", field: '["slide-1"]', "START_DATE": "-14"}
        )


def test_public_wsi_row_rejects_mrn_and_absolute_date_but_keeps_pseudonyms():
    row = {
        "PATIENT_ID": "P-1",
        "REFERENCE_SAMPLE_ID": "S-REF",
        "SAMPLE_ID": "S-1",
        "IMAGE_ID": "slide-1", "SLIDE_KEY": SLIDE_KEY,
        "BARCODE": "S-1",
        "PATH_DX_TITLE": "MRN: 123456",
        "SOURCE_URL": "s3://slides/slide-1.svs",
        "THUMBNAIL_URL": "s3://thumbs/slide-1.jpg",
    }
    with pytest.raises(DeidViolation):
        validate_wsi_public_row(
            row,
            source_prefixes=("s3://slides/",),
            thumbnail_prefixes=("s3://thumbs/",),
        )
    row["PATH_DX_TITLE"] = "Diagnosis"
    row["BARCODE"] = "MRN: 123456"
    with pytest.raises(DeidViolation):
        validate_wsi_public_row(
            row,
            source_prefixes=("s3://slides/",),
            thumbnail_prefixes=("s3://thumbs/",),
        )
    row["BARCODE"] = "S-1"
    validate_wsi_public_row(
        row,
        source_prefixes=("s3://slides/",),
        thumbnail_prefixes=("s3://thumbs/",),
    )


def test_timeline_rows_only_allow_relative_start_dates():
    with pytest.raises(DeidViolation):
        validate_timeline_public_row({"PATIENT_ID": "P-1", "START_DATE": "2024-01-01"})
    with pytest.raises(DeidViolation):
        validate_timeline_public_row({"PATIENT_ID": "P-1", "START_DATE": "20240101"})
    validate_timeline_public_row({"PATIENT_ID": "P-1", "START_DATE": "-14"})


def test_public_wsi_row_rejects_compact_absolute_dates_and_encoded_identifiers():
    with pytest.raises(DeidViolation):
        validate_wsi_public_row({"IMAGE_ID": "slide-1", "SLIDE_KEY": SLIDE_KEY, "PATH_DX_TITLE": "20240131"})
    with pytest.raises(DeidViolation):
        validate_artifact_uri(
            "s3://slides/P-1%2FMRN%3A123456.svs",
            image_id="slide-1",
            kind="source",
            prefixes=("s3://slides/",),
            related_identifiers=("P-1",),
        )


def test_public_wsi_row_rejects_unknown_tile_metadata_fields():
    with pytest.raises(DeidViolation):
        validate_wsi_public_row(
            {
                "IMAGE_ID": "slide-1", "SLIDE_KEY": SLIDE_KEY,
                "TILE_METADATA_JSON": '{"dimensions": {}, "patient_name": "Alice"}',
            }
        )


def test_public_wsi_row_allows_large_numeric_geometry_and_file_size_values():
    validate_wsi_public_row(
        {
            "IMAGE_ID": "slide-1", "SLIDE_KEY": SLIDE_KEY,
            "FILE_SIZE_BYTES": "2012345678",
            "TILE_METADATA_JSON": '{"dimensions":{"width":20240101,"height":256}}',
        }
    )


def test_public_wsi_row_requires_thumbnail_mime_to_match_extension():
    row = {
        "IMAGE_ID": "slide-1", "SLIDE_KEY": SLIDE_KEY,
        "THUMBNAIL_URL": "s3://thumbs/slide-1.jpg",
        "THUMBNAIL_CONTENT_TYPE": "image/png",
    }
    with pytest.raises(DeidViolation):
        validate_wsi_public_row(row, thumbnail_prefixes=("s3://thumbs/",))
    row["THUMBNAIL_CONTENT_TYPE"] = "image/jpeg"
    validate_wsi_public_row(row, thumbnail_prefixes=("s3://thumbs/",))


def test_public_wsi_row_allows_identity_metadata_from_thumbnail_registry():
    validate_wsi_public_row(
        {
            "IMAGE_ID": "slide-1", "SLIDE_KEY": SLIDE_KEY,
            "TILE_METADATA_JSON": '{"dimensions": {}, "identity_version": "v2"}',
        }
    )


def test_public_wsi_row_allows_date_like_hex_source_fingerprint():
    # Eight-digit runs such as 20190412 occur naturally in SHA-256 hex.
    fingerprint = "ab20190412" + "c" * 54
    validate_wsi_public_row(
        {
            "IMAGE_ID": "slide-1", "SLIDE_KEY": SLIDE_KEY,
            "TILE_METADATA_JSON": json.dumps({"dimensions": {}, "source_fingerprint": fingerprint}),
        }
    )
    for bad in ("2019-04-12", "not-a-digest", 123):
        with pytest.raises(DeidViolation):
            validate_wsi_public_row(
                {
                    "IMAGE_ID": "slide-1", "SLIDE_KEY": SLIDE_KEY,
                    "TILE_METADATA_JSON": json.dumps({"dimensions": {}, "source_fingerprint": bad}),
                }
            )


def test_public_rows_reject_identifier_field_names_even_without_labelled_values():
    with pytest.raises(DeidViolation):
        validate_wsi_public_row({"IMAGE_ID": "slide-1", "SLIDE_KEY": SLIDE_KEY, "MRN_ID": "123456"})
    with pytest.raises(DeidViolation):
        validate_timeline_public_row({"PATIENT_MRN": "123456", "START_DATE": "-1"})


@pytest.mark.parametrize(
    "slide_key",
    ["", "0123456789ABCDEF0123456789ABCDEF", "0123456789abcdef", "g" * 32, SLIDE_KEY + "0"],
)
def test_public_wsi_row_requires_opaque_slide_key(slide_key):
    with pytest.raises(DeidViolation, match="SLIDE_KEY"):
        validate_wsi_public_row({"IMAGE_ID": "slide-1", "SLIDE_KEY": slide_key})


def test_slide_key_hex_is_not_mistaken_for_a_compact_date():
    validate_wsi_public_row(
        {"IMAGE_ID": "slide-1", "SLIDE_KEY": "abc20210314def0123456789abcdef01"}
    )


def test_image_id_is_no_longer_an_unscanned_identifier():
    with pytest.raises(DeidViolation, match="absolute date"):
        validate_wsi_public_row({"IMAGE_ID": "2021-03-14", "SLIDE_KEY": SLIDE_KEY})


def test_public_wsi_row_allows_date_like_hex_in_opaque_keys_and_server_only_fields():
    key = "20190412" + "a" * 24
    validate_wsi_public_row(
        {
            "IMAGE_ID": "20190412", "SLIDE_KEY": SLIDE_KEY,
            "PART_KEY": f"part:{key}", "BLOCK_KEY": f"block:{key}",
            "SPECIMEN_KEY": f"block::part:{key}::block:{key}",
        }
    )
    # Non-canonical keys are still scanned as text.
    with pytest.raises(DeidViolation):
        validate_wsi_public_row({"IMAGE_ID": "slide-1", "SLIDE_KEY": SLIDE_KEY, "PART_KEY": "part:20190412"})
    # Server-only fields still reject labelled MRNs and delimited dates.
    for bad in ("s3://bucket/mrn 1234567.svs", "s3://bucket/2021-03-14.svs"):
        with pytest.raises(DeidViolation):
            validate_wsi_public_row({"IMAGE_ID": "slide-1", "SLIDE_KEY": SLIDE_KEY, "SOURCE_URL": bad})
