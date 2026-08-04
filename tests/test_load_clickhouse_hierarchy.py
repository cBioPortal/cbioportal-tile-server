import json

import pytest

from tools.load_clickhouse_hierarchy import load


def hierarchy(*, sample_id="S-1", image_id="SLIDE-1", unmatched=False):
    actual_sample = None if unmatched else sample_id
    return {
        "samples": [
            {
                "sample_id": "UNMATCHED" if unmatched else sample_id,
                "parts": [
                    {
                        "part_number": "1",
                        "part_description": "specimen",
                        "blocks": [
                            {
                                "block_number": "1",
                                "slides": [{"image_id": image_id, "can_serve_tiles": True}],
                            }
                        ],
                    }
                ],
            }
        ],
        "slide_associations": [
            {
                "image_id": image_id,
                "sample_id": actual_sample,
                "match_level": "UNMATCHED" if unmatched else "BLOCK",
                "specimen_key": "block::1::1",
            }
        ],
    }


def snapshot_file(tmp_path, rows):
    path = tmp_path / "hierarchy.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


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
    snapshot = snapshot_file(tmp_path, [{"study_id": "unknown", "patient_id": "P-1", "hierarchy": hierarchy()}])
    clickhouse = RecordingClickHouse(identities=[])

    with pytest.raises(ValueError, match="unknown cBioPortal study/patient"):
        load(snapshot, 7, clickhouse)

    assert clickhouse.calls == []


def test_flat_canonical_rows_are_normalized_without_a_hierarchy_blob(tmp_path):
    snapshot = snapshot_file(
        tmp_path,
        [{
            "study_id": "study",
            "patient_id": "P-1",
            "slides": [{
                "image_id": "SLIDE-1",
                "sample_id": "S-1",
                "reference_sample_id": "S-1",
                "part_number": "1",
                "block_number": "A",
                "block_label": "A1",
                "match_level": "BLOCK",
                "specimen_key": "block::1::A",
                "can_serve_tiles": True,
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


def test_reference_sample_without_a_pathology_slide_is_resolved(tmp_path):
    snapshot = snapshot_file(
        tmp_path,
        [{
            "study_id": "study",
            "patient_id": "P-1",
            "hierarchy": {
                "reference_sample_id": "S-1",
                "samples": [],
                "slide_associations": [],
            },
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


def test_duplicate_patient_is_rejected_before_any_database_write(tmp_path):
    snapshot = snapshot_file(
        tmp_path,
        [
            {"study_id": "study", "patient_id": "P-1", "hierarchy": hierarchy()},
            {"study_id": "study", "patient_id": "P-1", "hierarchy": hierarchy(image_id="SLIDE-2")},
        ],
    )
    clickhouse = RecordingClickHouse()

    with pytest.raises(ValueError, match="duplicate study_id/patient_id"):
        load(snapshot, 7, clickhouse)
    assert clickhouse.calls == []


def test_unmatched_slide_is_persisted_with_null_sample_and_no_sentinel(tmp_path):
    snapshot = snapshot_file(tmp_path, [{"study_id": "study", "patient_id": "P-1", "hierarchy": hierarchy(unmatched=True)}])
    clickhouse = RecordingClickHouse()

    load(snapshot, 7, clickhouse)

    placement = next(body for query, body in clickhouse.calls if query.startswith("INSERT INTO wsi_slide_placement"))
    row = json.loads(placement.decode().splitlines()[0])
    assert row["sample_id"] is None
    assert not any('"sample_id": "UNMATCHED"' in (body or b"").decode() for _, body in clickhouse.calls if body)


def test_duplicate_slide_association_is_rejected_before_writes(tmp_path):
    value = hierarchy()
    value["slide_associations"].append(value["slide_associations"][0].copy())
    snapshot = snapshot_file(tmp_path, [{"study_id": "study", "patient_id": "P-1", "hierarchy": value}])
    clickhouse = RecordingClickHouse()

    with pytest.raises(ValueError, match="duplicate slide association"):
        load(snapshot, 7, clickhouse)
    assert clickhouse.calls == []


def test_retry_uses_a_new_release_and_normalized_tables(tmp_path):
    snapshot = snapshot_file(tmp_path, [{"study_id": "study", "patient_id": "P-1", "hierarchy": hierarchy()}])
    clickhouse = RecordingClickHouse()
    resource_index = tmp_path / "resource-index.json"

    count, studies = load(snapshot, 7, clickhouse, resource_index)
    assert count == 1
    assert studies == {"study"}
    first_manifest = [body for query, body in clickhouse.calls if query.startswith("INSERT INTO wsi_release")][-1]
    first_release = json.loads(first_manifest.decode().splitlines()[0])["release_id"]
    assert any(query.startswith("INSERT INTO wsi_slide FORMAT") for query, _ in clickhouse.calls)
    assert not any("hierarchy_json" in (body or b"").decode() for _, body in clickhouse.calls)

    load(snapshot, 7, clickhouse, resource_index)
    manifests = [body for query, body in clickhouse.calls if query.startswith("INSERT INTO wsi_release")]
    second_release = json.loads(manifests[-1].decode().splitlines()[0])["release_id"]
    assert second_release != first_release
    assert json.loads(resource_index.read_text())["studies"]["study"]["samples"] == ["S-1"]


def test_failed_release_restores_previous_resource_index(tmp_path):
    snapshot = snapshot_file(tmp_path, [{"study_id": "study", "patient_id": "P-1", "hierarchy": hierarchy()}])
    resource_index = tmp_path / "resource-index.json"
    load(snapshot, 7, RecordingClickHouse(), resource_index)
    previous_index = resource_index.read_text()

    failing = RecordingClickHouse(fail_on="INSERT INTO wsi_release")
    with pytest.raises(RuntimeError, match="simulated"):
        load(snapshot, 8, failing, resource_index)
    assert resource_index.read_text() == previous_index
