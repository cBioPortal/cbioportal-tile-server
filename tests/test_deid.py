import pytest

from app.deid import DeidViolation, validate_artifact_uri, validate_timeline_public_row, validate_wsi_public_row


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


def test_public_wsi_row_rejects_mrn_and_absolute_date_but_keeps_pseudonyms():
    row = {
        "PATIENT_ID": "P-1",
        "REFERENCE_SAMPLE_ID": "S-REF",
        "SAMPLE_ID": "S-1",
        "IMAGE_ID": "slide-1",
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
        validate_wsi_public_row({"IMAGE_ID": "slide-1", "PATH_DX_TITLE": "20240131"})
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
                "IMAGE_ID": "slide-1",
                "TILE_METADATA_JSON": '{"dimensions": {}, "patient_name": "Alice"}',
            }
        )


def test_public_wsi_row_allows_large_numeric_geometry_and_file_size_values():
    validate_wsi_public_row(
        {
            "IMAGE_ID": "slide-1",
            "FILE_SIZE_BYTES": "2012345678",
            "TILE_METADATA_JSON": '{"dimensions":{"width":20240101,"height":256}}',
        }
    )


def test_public_wsi_row_requires_thumbnail_mime_to_match_extension():
    row = {
        "IMAGE_ID": "slide-1",
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
            "IMAGE_ID": "slide-1",
            "TILE_METADATA_JSON": '{"dimensions": {}, "identity_version": "v2"}',
        }
    )


def test_public_rows_reject_identifier_field_names_even_without_labelled_values():
    with pytest.raises(DeidViolation):
        validate_wsi_public_row({"IMAGE_ID": "slide-1", "MRN_ID": "123456"})
    with pytest.raises(DeidViolation):
        validate_timeline_public_row({"PATIENT_MRN": "123456", "START_DATE": "-1"})
