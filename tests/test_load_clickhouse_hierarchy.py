import json

import pytest

from tools.load_clickhouse_hierarchy import load
from tools.wsi_study_format import write_wsi_study_files


def slide(*, sample_id="S-1", image_id="SLIDE-1", unmatched=False):
    return {
        "image_id": image_id,
        "sample_id": None if unmatched else sample_id,
        "part_key": "part:1",
        "part_number": "1",
        "part_description": "specimen",
        "block_key": "block:1",
        "block_number": "1",
        "match_level": "UNMATCHED" if unmatched else "BLOCK",
        "specimen_key": "block::1::1",
        "can_serve_tiles": True,
    }


def wsi_study_files(tmp_path, rows):
    study_ids = {row["study_id"] for row in rows}
    if len(study_ids) != 1:
        raise ValueError("test fixture must contain exactly one study")
    canonical_rows = []
    for row in rows:
        for slide_row in row["slides"]:
            canonical = {
                **slide_row,
                "patient_id": row["patient_id"],
                "is_hne": slide_row.get("is_hne", False),
                "is_ihc": slide_row.get("is_ihc", False),
            }
            if slide_row.get("can_serve_tiles") in (True, "true", "TRUE"):
                image_id = slide_row["image_id"]
                canonical.setdefault("slide_path", f"s3://test-bucket/{image_id}.svs")
                canonical.setdefault("tile_metadata_json", '{"width":1024,"height":768}')
                canonical.setdefault("thumbnail_url", f"s3://test-bucket/{image_id}.jpg")
                canonical.setdefault("thumbnail_width", 128)
                canonical.setdefault("thumbnail_height", 96)
                canonical.setdefault("thumbnail_content_type", "image/jpeg")
            canonical_rows.append(canonical)
    meta_path, _ = write_wsi_study_files(tmp_path, study_ids.pop(), canonical_rows)
    return meta_path


class RecordingClickHouse:
    def __init__(self, *, fail_on=None, identities=None, samples=None):
        self.calls = []
        self.fail_on = fail_on
        self.identities = identities or [{"study_id": "study", "study_internal_id": 7, "patient_id": "P-1", "patient_internal_id": 70}]
        self.samples = samples or [{"study_id": "study", "patient_id": "P-1", "sample_id": "S-1", "sample_internal_id": 700}]

    def query_json(self, query):
        return self.samples if "FROM sample" in query else self.identities

    def execute(self, query, body=None):
        self.calls.append((query, body))
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("simulated ClickHouse failure")


def test_unknown_portal_reference_is_rejected_before_any_database_write(tmp_path):
    snapshot = wsi_study_files(
        tmp_path,
        [{"study_id": "unknown", "patient_id": "P-1", "slides": [slide()]}],
    )
    clickhouse = RecordingClickHouse(identities=[])

    with pytest.raises(ValueError, match="unknown cBioPortal study/patient"):
        load(snapshot, 7, clickhouse)

    assert clickhouse.calls == []


def test_flat_canonical_rows_are_normalized_without_a_hierarchy_blob(tmp_path):
    snapshot = wsi_study_files(
        tmp_path,
        [{
            "study_id": "study",
            "patient_id": "P-1",
            "slides": [{
                "image_id": "SLIDE-1",
                "sample_id": "S-1",
                "reference_sample_id": "S-1",
                "part_key": "part:1",
                "part_number": "1",
                "block_key": "block:A",
                "block_number": "A",
                "block_label": "A1",
                "match_level": "BLOCK",
                "specimen_key": "block::1::A",
                "can_serve_tiles": True,
                "slide_path": "s3://test-bucket/SLIDE-1.svs",
            }],
        }],
    )
    clickhouse = RecordingClickHouse()

    load(snapshot, 7, clickhouse)

    slide = next(body for query, body in clickhouse.calls if query.startswith("INSERT INTO wsi_slide FORMAT"))
    assert json.loads(slide.decode().splitlines()[0])["image_id"] == "SLIDE-1"
    snapshot = next(body for query, body in clickhouse.calls if query.startswith("INSERT INTO wsi_release_patient FORMAT"))
    snapshot_row = json.loads(snapshot.decode().splitlines()[0])
    assert snapshot_row["reference_sample_id"] == 700
    assert "reference_sequencing_date" not in snapshot_row
    part_body = next(body for query, body in clickhouse.calls if query.startswith("INSERT INTO wsi_part FORMAT"))
    assert "release_version" not in json.loads(part_body.decode().splitlines()[0])


def test_flat_canonical_rows_coerce_databricks_boolean_strings(tmp_path):
    snapshot = wsi_study_files(
        tmp_path,
        [{
            "study_id": "study",
            "patient_id": "P-1",
            "slides": [{
                "image_id": "SLIDE-1",
                "sample_id": "S-1",
                "reference_sample_id": "S-1",
                "part_key": "part:1",
                "part_number": "1",
                "block_key": "block:A",
                "block_number": "A",
                "match_level": "BLOCK",
                "specimen_key": "block::1::A",
                "is_hne": "true",
                "is_ihc": "false",
                "can_serve_tiles": "true",
                "slide_path": "s3://test-bucket/SLIDE-1.svs",
            }],
        }],
    )
    clickhouse = RecordingClickHouse()

    load(snapshot, 7, clickhouse)

    slide = next(
        json.loads(body.decode().splitlines()[0])
        for query, body in clickhouse.calls
        if query.startswith("INSERT INTO wsi_slide FORMAT")
    )
    assert slide["is_hne"] is True
    assert slide["is_ihc"] is False
    assert slide["can_serve_tiles"] is True


def test_flat_canonical_rows_keep_slides_when_part_metadata_differs(tmp_path):
    snapshot = wsi_study_files(
        tmp_path,
        [{
            "study_id": "study",
            "patient_id": "P-1",
            "slides": [
                {
                    "image_id": "SLIDE-1",
                    "sample_id": "S-1",
                    "part_key": "part:1",
                    "part_number": "1",
                    "part_type": "LIVER",
                    "part_description": "Primary specimen",
                    "block_key": "block:A",
                    "block_number": "A",
                    "match_level": "BLOCK",
                    "specimen_key": "block::1::A",
                    "can_serve_tiles": False,
                },
                {
                    "image_id": "SLIDE-2",
                    "sample_id": "S-1",
                    "part_key": "part:1",
                    "part_number": "1",
                    "part_type": "SUBMITTED SLIDES",
                    "part_description": "Primary specimen ",
                    "block_key": "block:B",
                    "block_number": "B",
                    "match_level": "PART",
                    "specimen_key": "part::1",
                    "can_serve_tiles": False,
                },
            ],
        }],
    )
    clickhouse = RecordingClickHouse()

    load(snapshot, 7, clickhouse)

    parts = [
        json.loads(line)
        for query, body in clickhouse.calls
        if query.startswith("INSERT INTO wsi_part FORMAT")
        for line in body.decode().splitlines()
    ]
    slides = [
        json.loads(line)
        for query, body in clickhouse.calls
        if query.startswith("INSERT INTO wsi_slide FORMAT")
        for line in body.decode().splitlines()
    ]
    assert len(parts) == 1
    assert {slide["image_id"] for slide in slides} == {"SLIDE-1", "SLIDE-2"}


def test_canonical_rows_preserve_multiple_parts_blocks_and_slide_bindings(tmp_path):
    snapshot = wsi_study_files(
        tmp_path,
        [{
            "study_id": "study",
            "patient_id": "P-1",
            "slides": [
                {
                    "image_id": "SLIDE-1",
                    "sample_id": "S-1",
                    "reference_sample_id": "S-1",
                    "part_key": "part:1",
                    "part_number": "1",
                    "block_key": "block:A",
                    "block_number": "A",
                    "match_level": "BLOCK",
                    "specimen_key": "block::part:1::block:A",
                    "procedure_date_days": 4,
                    "timepoint_source": "Procedure date relative to tumor sequencing",
                    "is_hne": True,
                    "is_ihc": False,
                    "can_serve_tiles": True,
                    "slide_path": "s3://test-bucket/SLIDE-1.svs",
                },
                {
                    "image_id": "SLIDE-2",
                    "sample_id": "S-1",
                    "reference_sample_id": "S-1",
                    "part_key": "part:2",
                    "part_number": "2",
                    "block_key": "block:B",
                    "block_number": "B",
                    "match_level": "PART",
                    "specimen_key": "part::part:2::block:B",
                    "procedure_date_days": 9,
                    "timepoint_source": "Procedure date relative to tumor sequencing",
                    "is_hne": False,
                    "is_ihc": True,
                    "can_serve_tiles": True,
                    "slide_path": "s3://test-bucket/SLIDE-2.svs",
                },
            ],
        }],
    )
    clickhouse = RecordingClickHouse()
    load(snapshot, 7, clickhouse)

    parts = [
        json.loads(line)
        for query, body in clickhouse.calls
        if query.startswith("INSERT INTO wsi_part FORMAT")
        for line in body.decode().splitlines()
    ]
    blocks = [
        json.loads(line)
        for query, body in clickhouse.calls
        if query.startswith("INSERT INTO wsi_block FORMAT")
        for line in body.decode().splitlines()
    ]
    placements = [
        json.loads(line)
        for query, body in clickhouse.calls
        if query.startswith("INSERT INTO wsi_slide_placement FORMAT")
        for line in body.decode().splitlines()
    ]
    assert {row["part_key"] for row in parts} == {"part:1", "part:2"}
    assert {(row["part_key"], row["block_key"]) for row in blocks} == {
        ("part:1", "block:A"),
        ("part:2", "block:B"),
    }
    assert {row["procedure_date_days"] for row in placements} == {4, 9}

def test_malformed_canonical_row_is_rejected_before_database_writes(tmp_path):
    snapshot = wsi_study_files(
        tmp_path,
        [{
            "study_id": "study",
            "patient_id": "P-1",
            "slides": [{
                "image_id": "SLIDE-1",
                "sample_id": "S-1",
                "block_key": "block:A",
                "match_level": "BLOCK",
                "specimen_key": "block::part:1::block:A",
                "can_serve_tiles": False,
            }],
        }],
    )
    clickhouse = RecordingClickHouse()

    with pytest.raises(ValueError, match="PART_KEY"):
        load(snapshot, 7, clickhouse)

    assert clickhouse.calls == []


def test_reference_sample_without_a_pathology_slide_is_resolved(tmp_path):
    snapshot = wsi_study_files(
        tmp_path,
        [{
            "study_id": "study",
            "patient_id": "P-1",
            "slides": [{
                "image_id": "SLIDE-1",
                "sample_id": "S-1",
                "reference_sample_id": "S-1",
                "part_key": "part:1",
                "block_key": "block:1",
                "match_level": "BLOCK",
                "specimen_key": "block::1::1",
                "can_serve_tiles": False,
            }],
        }],
    )
    clickhouse = RecordingClickHouse()

    load(snapshot, 7, clickhouse)

    snapshot_body = next(
        body
        for query, body in clickhouse.calls
        if query.startswith("INSERT INTO wsi_release_patient FORMAT")
    )
    assert json.loads(snapshot_body.decode().splitlines()[0])["reference_sample_id"] == 700


def test_sample_resolution_ignores_same_stable_id_in_another_study(tmp_path):
    snapshot = wsi_study_files(
        tmp_path,
        [{"study_id": "study-a", "patient_id": "P-1", "slides": [slide()]}],
    )
    clickhouse = RecordingClickHouse(
        identities=[
            {"study_id": "study-a", "study_internal_id": 7, "patient_id": "P-1", "patient_internal_id": 70},
        ],
        samples=[
            {"study_id": "study-a", "patient_id": "P-1", "sample_id": "S-1", "sample_internal_id": 700},
            {"study_id": "study-b", "patient_id": "P-9", "sample_id": "S-1", "sample_internal_id": 900},
        ]
    )

    load(snapshot, 7, clickhouse)

    release_patient = next(
        body
        for query, body in clickhouse.calls
        if query.startswith("INSERT INTO wsi_release_patient FORMAT")
    )
    assert json.loads(release_patient.decode().splitlines()[0])["reference_sample_id"] is None
    placement = next(
        body
        for query, body in clickhouse.calls
        if query.startswith("INSERT INTO wsi_slide_placement FORMAT")
    )
    assert json.loads(placement.decode().splitlines()[0])["sample_id"] == 700


def test_duplicate_image_is_rejected_before_any_database_write(tmp_path):
    snapshot = wsi_study_files(
        tmp_path,
        [
            {"study_id": "study", "patient_id": "P-1", "slides": [slide()]},
            {"study_id": "study", "patient_id": "P-2", "slides": [slide()]},
        ],
    )
    clickhouse = RecordingClickHouse()

    with pytest.raises(ValueError, match="duplicate IMAGE_ID"):
        load(snapshot, 7, clickhouse)
    assert clickhouse.calls == []


def test_unmatched_slide_is_persisted_with_null_sample_and_no_sentinel(tmp_path):
    snapshot = wsi_study_files(
        tmp_path,
        [{"study_id": "study", "patient_id": "P-1", "slides": [slide(unmatched=True)]}],
    )
    clickhouse = RecordingClickHouse()

    load(snapshot, 7, clickhouse)

    placement = next(body for query, body in clickhouse.calls if query.startswith("INSERT INTO wsi_slide_placement"))
    row = json.loads(placement.decode().splitlines()[0])
    assert row["sample_id"] is None
    assert not any('"sample_id": "UNMATCHED"' in (body or b"").decode() for _, body in clickhouse.calls if body)


def test_retry_uses_a_new_release_and_normalized_tables(tmp_path):
    snapshot = wsi_study_files(
        tmp_path,
        [{"study_id": "study", "patient_id": "P-1", "slides": [slide()]}],
    )
    clickhouse = RecordingClickHouse()
    count, studies = load(snapshot, 7, clickhouse)
    assert count == 1
    assert studies == {"study"}
    first_manifest = [body for query, body in clickhouse.calls if query.startswith("INSERT INTO wsi_release")][-1]
    first_release = json.loads(first_manifest.decode().splitlines()[0])["release_id"]
    assert any(query.startswith("INSERT INTO wsi_slide FORMAT") for query, _ in clickhouse.calls)
    assert not any("hierarchy_json" in (body or b"").decode() for _, body in clickhouse.calls)

    load(snapshot, 7, clickhouse)
    manifests = [body for query, body in clickhouse.calls if query.startswith("INSERT INTO wsi_release")]
    second_release = json.loads(manifests[-1].decode().splitlines()[0])["release_id"]
    assert second_release != first_release
