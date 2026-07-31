import json

import pytest

from tools.load_clickhouse_hierarchy import _row, load


def test_row_compacts_and_validates_hierarchy():
    study, encoded = _row(
        json.dumps({
            "study_id": "study",
            "patient_id": "P-1",
            "hierarchy": {"patient_id": "P-1", "samples": []},
        }),
        7,
        "2026-07-23 03:00:00",
    )
    assert study == "study"
    row = json.loads(encoded)
    assert row["snapshot_version"] == 7
    assert row["publication_id"] == ""
    assert row["hierarchy_json"] == '{"patient_id":"P-1","samples":[]}'


def test_row_rejects_patient_mismatch():
    with pytest.raises(ValueError, match="does not match"):
        _row(
            json.dumps({
                "study_id": "study",
                "patient_id": "P-1",
                "hierarchy": {"patient_id": "P-2", "samples": []},
            }),
            7,
            "2026-07-23 03:00:00",
        )


@pytest.mark.parametrize(
    "row",
    [
        {"study_id": 7, "patient_id": "P-1", "hierarchy": {"patient_id": "P-1"}},
        {"study_id": "study", "patient_id": 7, "hierarchy": {"patient_id": "7"}},
        {"study_id": " ", "patient_id": "P-1", "hierarchy": {"patient_id": "P-1"}},
    ],
)
def test_row_rejects_non_string_or_blank_identifiers(row):
    with pytest.raises(ValueError, match="study_id"):
        _row(json.dumps(row), 7, "2026-07-23 03:00:00")


class RecordingClickHouse:
    def __init__(self, fail_on: str | None = None):
        self.calls: list[tuple[str, bytes | None]] = []
        self.fail_on = fail_on

    def execute(self, query: str, body: bytes | None = None) -> None:
        self.calls.append((query, body))
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("simulated ClickHouse failure")


def snapshot_file(tmp_path, rows):
    path = tmp_path / "hierarchy.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def test_duplicate_patient_is_rejected_before_any_database_write(tmp_path):
    snapshot = snapshot_file(
        tmp_path,
        [
            {"study_id": "study", "patient_id": "P-1", "hierarchy": {"patient_id": "P-1"}},
            {"study_id": "study", "patient_id": "P-1", "hierarchy": {"patient_id": "P-1"}},
        ],
    )
    clickhouse = RecordingClickHouse()

    with pytest.raises(ValueError, match="duplicate study_id/patient_id"):
        load(snapshot, 7, clickhouse)

    assert clickhouse.calls == []


def test_failed_row_insert_does_not_publish_a_manifest(tmp_path):
    snapshot = snapshot_file(
        tmp_path,
        [{"study_id": "study", "patient_id": "P-1", "hierarchy": {"patient_id": "P-1"}}],
    )
    clickhouse = RecordingClickHouse(fail_on="INSERT INTO wsi_patient_hierarchy FORMAT JSONEachRow")

    with pytest.raises(RuntimeError, match="simulated"):
        load(snapshot, 7, clickhouse)

    assert not any(
        query == "INSERT INTO wsi_patient_hierarchy_manifest FORMAT JSONEachRow"
        for query, _ in clickhouse.calls
    )


def test_retry_uses_a_new_publication_and_publishes_one_manifest_batch(tmp_path):
    snapshot = snapshot_file(
        tmp_path,
        [
            {
                "study_id": "study",
                "patient_id": "P-1",
                "hierarchy": {"patient_id": "P-1", "samples": [{"sample_id": "corrected"}]},
            },
            {"study_id": "other-study", "patient_id": "P-2", "hierarchy": {"patient_id": "P-2"}},
        ],
    )
    clickhouse = RecordingClickHouse()
    resource_index = tmp_path / "resource-index.json"

    count, studies = load(snapshot, 7, clickhouse, resource_index)
    assert count == 2
    assert studies == {"study", "other-study"}

    row_bodies = [
        body
        for query, body in clickhouse.calls
        if query == "INSERT INTO wsi_patient_hierarchy FORMAT JSONEachRow"
    ]
    manifest_bodies = [
        body
        for query, body in clickhouse.calls
        if query == "INSERT INTO wsi_patient_hierarchy_manifest FORMAT JSONEachRow"
    ]
    assert len(row_bodies) == 1
    assert len(manifest_bodies) == 1
    row_publications = {json.loads(line)["publication_id"] for line in row_bodies[0].decode().splitlines()}
    manifest_publications = {json.loads(line)["publication_id"] for line in manifest_bodies[0].decode().splitlines()}
    assert len(row_publications) == 1
    assert row_publications == manifest_publications

    original_publication = next(iter(row_publications))
    count, _ = load(snapshot, 7, clickhouse, resource_index)
    assert count == 2
    all_row_bodies = [
        body
        for query, body in clickhouse.calls
        if query == "INSERT INTO wsi_patient_hierarchy FORMAT JSONEachRow"
    ]
    retry_publication = json.loads(all_row_bodies[-1].decode().splitlines()[0])["publication_id"]
    assert retry_publication != original_publication

    index = json.loads(resource_index.read_text())
    assert index["version"] == 1
    assert index["studies"]["study"]["patients"] == ["P-1"]
    assert index["studies"]["study"]["samples"] == ["corrected"]


def test_failed_manifest_publication_restores_previous_resource_index(tmp_path):
    snapshot = snapshot_file(
        tmp_path,
        [{"study_id": "study", "patient_id": "P-1", "hierarchy": {"patient_id": "P-1"}}],
    )
    resource_index = tmp_path / "resource-index.json"
    load(snapshot, 7, RecordingClickHouse(), resource_index)
    previous_index = resource_index.read_text()

    failing = RecordingClickHouse(
        fail_on="INSERT INTO wsi_patient_hierarchy_manifest FORMAT JSONEachRow"
    )
    with pytest.raises(RuntimeError, match="simulated"):
        load(snapshot, 8, failing, resource_index)

    assert resource_index.read_text() == previous_index


def test_cross_study_resource_collision_is_rejected_before_database_write(tmp_path):
    snapshot = snapshot_file(
        tmp_path,
        [
            {
                "study_id": "study-a",
                "patient_id": "P-1",
                "hierarchy": {
                    "patient_id": "P-1",
                    "samples": [{"sample_id": "S-1", "parts": []}],
                    "slide_associations": [{"image_id": "SLIDE-1"}],
                },
            },
            {
                "study_id": "study-b",
                "patient_id": "P-2",
                "hierarchy": {
                    "patient_id": "P-2",
                    "samples": [{"sample_id": "S-2", "parts": []}],
                    "slide_associations": [{"image_id": "SLIDE-1"}],
                },
            },
        ],
    )
    clickhouse = RecordingClickHouse()

    with pytest.raises(ValueError, match="ambiguous across studies"):
        load(snapshot, 7, clickhouse, tmp_path / "resource-index.json")

    assert clickhouse.calls == []
