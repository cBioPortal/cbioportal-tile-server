import pytest

from tools.generate_wsi_sample_count_clinical_file import _load_counts, main
from tools.wsi_study_format import write_wsi_study_files


def row(image_id, sample_id, match_level):
    return {
        "patient_id": "P-1",
        "sample_id": sample_id,
        "image_id": image_id,
        "part_key": "part:1",
        "block_key": f"block:{image_id}",
        "match_level": match_level,
        "specimen_key": f"{match_level.lower()}::{image_id}",
        "is_hne": False,
        "is_ihc": False,
        "can_serve_tiles": False,
    }


def test_counts_are_derived_from_wsi_study_files(tmp_path):
    meta_path, _ = write_wsi_study_files(
        tmp_path,
        "study",
        [
            row("SLIDE-1", "S-1", "BLOCK"),
            row("SLIDE-2", "S-1", "PART"),
            row("SLIDE-3", None, "UNMATCHED"),
        ],
    )

    counts = _load_counts(meta_path, "study")

    assert counts == {
        "S-1": {
            "WSI_SAMPLE_SLIDE_COUNT": 2,
            "WSI_SAMPLE_PART_MATCHED_SLIDE_COUNT": 1,
            "WSI_SAMPLE_BLOCK_MATCHED_SLIDE_COUNT": 1,
        }
    }


def test_counts_reject_a_mismatched_study_identifier(tmp_path):
    meta_path, _ = write_wsi_study_files(
        tmp_path,
        "study-a",
        [row("SLIDE-1", "S-1", "BLOCK")],
    )

    with pytest.raises(ValueError, match="does not match"):
        _load_counts(meta_path, "study-b")


def test_generation_keeps_wsi_metadata_and_writes_count_metadata(tmp_path, monkeypatch):
    meta_path, _ = write_wsi_study_files(
        tmp_path,
        "study",
        [row("SLIDE-1", "S-1", "BLOCK")],
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_wsi_sample_count_clinical_file.py",
            "--study-id",
            "study",
            "--wsi-meta",
            str(meta_path),
            "--study-dir",
            str(tmp_path),
        ],
    )

    assert main() == 0
    assert "datatype: WSI" in meta_path.read_text(encoding="utf-8")
    assert (tmp_path / "meta_clinical_sample_wsi_counts.txt").is_file()
