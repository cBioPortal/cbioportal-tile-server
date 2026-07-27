import json

import pytest

from tools.load_clickhouse_hierarchy import _row


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
