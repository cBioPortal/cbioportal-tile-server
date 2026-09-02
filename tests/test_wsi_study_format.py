import json

import pytest

from tools.wsi_study_format import (
    COLUMNS,
    read_wsi_study,
    write_wsi_study_files,
)


def canonical_row(**overrides):
    row = {
        "patient_id": "P-1",
        "reference_sample_id": "S-REF",
        "sample_id": "S-1",
        "image_id": "SLIDE-1",
        "part_key": "part:1",
        "block_key": "block:A",
        "match_level": "BLOCK",
        "specimen_key": "block::part:1::block:A",
        "is_hne": True,
        "is_ihc": False,
        "can_serve_tiles": True,
        "slide_path": "s3://slides/SLIDE-1.svs",
        "tile_metadata": {"width": 2048, "height": 1024},
        "thumbnail_url": "s3://slides/SLIDE-1.jpg",
        "thumbnail_width": 256,
        "thumbnail_height": 128,
        "thumbnail_content_type": "image/jpeg",
    }
    row.update(overrides)
    return row


def test_writer_uses_cbioportal_attribute_rows_and_versioned_meta_file(tmp_path):
    meta_path, data_path = write_wsi_study_files(
        tmp_path,
        "study_tcga",
        [canonical_row()],
    )

    assert meta_path.read_text().splitlines() == [
        "cancer_study_identifier: study_tcga",
        "genetic_alteration_type: PATHOLOGY_SLIDES",
        "datatype: WSI",
        "data_filename: data_wsi.txt",
        "format_version: 2",
    ]
    lines = data_path.read_text().splitlines()
    assert all(cell.startswith("#") for line in lines[:4] for cell in line.split("\t"))
    assert lines[4].split("\t") == [column.name for column in COLUMNS]
    assert len(lines[5].split("\t")) == len(COLUMNS)


def test_wsi_study_files_round_trip_typed_values_and_unmatched_slides(tmp_path):
    write_wsi_study_files(
        tmp_path,
        "study",
        [
            canonical_row(),
            canonical_row(
                patient_id="P-2",
                reference_sample_id=None,
                sample_id=None,
                image_id="SLIDE-2",
                match_level="UNMATCHED",
                specimen_key="unmatched::SLIDE-2",
                is_hne=False,
                can_serve_tiles=False,
                slide_path=None,
                tile_metadata=None,
                thumbnail_url=None,
                thumbnail_width=None,
                thumbnail_height=None,
                thumbnail_content_type=None,
            ),
        ],
    )

    study_id, rows = read_wsi_study(tmp_path / "meta_wsi.txt")

    assert study_id == "study"
    assert rows[0]["is_hne"] is True
    assert rows[0]["is_ihc"] is False
    assert rows[0]["thumbnail_width"] == 256
    assert json.loads(rows[0]["tile_metadata_json"]) == {
        "width": 2048,
        "height": 1024,
    }
    assert rows[1]["sample_id"] is None
    assert rows[1]["can_serve_tiles"] is False


def test_reader_rejects_a_header_that_does_not_match_the_format_version(tmp_path):
    _, data_path = write_wsi_study_files(tmp_path, "study", [canonical_row()])
    contents = data_path.read_text().replace("PATIENT_ID", "PATIENT", 1)
    data_path.write_text(contents)

    with pytest.raises(ValueError, match="columns do not match format_version 2"):
        read_wsi_study(tmp_path / "meta_wsi.txt")


def test_reader_rejects_legacy_format_version(tmp_path):
    _, data_path = write_wsi_study_files(tmp_path, "study", [canonical_row()])
    meta_path = tmp_path / "meta_wsi.txt"
    meta_path.write_text(meta_path.read_text().replace("format_version: 2", "format_version: 1"))

    with pytest.raises(ValueError, match="unsupported WSI format_version"):
        read_wsi_study(meta_path)


def test_reader_rejects_incomplete_servable_artifacts(tmp_path):
    write_wsi_study_files(
        tmp_path,
        "study",
        [canonical_row(thumbnail_url=None)],
    )

    with pytest.raises(ValueError, match="servable WSI data line .*thumbnail_url"):
        read_wsi_study(tmp_path / "meta_wsi.txt")


def test_writer_rejects_non_absolute_source_urls(tmp_path):
    with pytest.raises(ValueError, match="de-identification contract: unsafe source URI"):
        write_wsi_study_files(
            tmp_path,
            "study",
            [canonical_row(slide_path="slides/SLIDE-1.svs")],
        )


def test_writer_rejects_tabs_and_newlines_in_values(tmp_path):
    with pytest.raises(ValueError, match="cannot contain tabs or newlines"):
        write_wsi_study_files(
            tmp_path,
            "study",
            [canonical_row(part_description="invalid\tdescription")],
        )


def test_writer_rejects_thumbnail_mime_extension_mismatch(tmp_path):
    with pytest.raises(ValueError, match="thumbnail content type does not match URI"):
        write_wsi_study_files(
            tmp_path,
            "study",
            [canonical_row(thumbnail_content_type="image/png")],
        )
