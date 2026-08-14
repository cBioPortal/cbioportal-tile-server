import pytest

from tools.export_materialized_hierarchy_snapshot import _read_patient_ids


def test_patient_ids_are_read_by_column_name(tmp_path):
    (tmp_path / "data_clinical_patient.txt").write_text(
        "#Sample count\t#Patient Identifier\n"
        "#Description\t#Description\n"
        "#NUMBER\t#STRING\n"
        "#1\t#1\n"
        "WSI_SAMPLE_COUNT\tPATIENT_ID\n"
        "2\tP-2\n"
        "1\tP-1\n"
        "3\tP-2\n"
    )

    assert _read_patient_ids(tmp_path) == ["P-1", "P-2"]


def test_patient_file_requires_patient_id_column(tmp_path):
    (tmp_path / "data_clinical_patient.txt").write_text(
        "#Name\n#Description\n#STRING\n#1\nOTHER_ID\nvalue\n"
    )

    with pytest.raises(ValueError, match="PATIENT_ID column not found"):
        _read_patient_ids(tmp_path)
