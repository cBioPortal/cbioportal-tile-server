import csv
from pathlib import Path

from tools.generate_pathology_timeline_files import (
    build_pathology_timeline_rows,
    main,
    write_pathology_timeline_files,
)


def test_build_pathology_timeline_rows_groups_counts_from_canonical_associations():
    rows = build_pathology_timeline_rows(
        [
            {
                "patient_id": "P-1",
                "sample_id": "S-1",
                "match_level": "BLOCK",
                "image_id": "img-1",
                "block_id": "block/1-A1",
                "block_label": "A1",
                "part_description": "Colon",
                "stain_name": "H&E",
                "stain_group": "H&E (Initial)",
                "slide_path": "s3://bucket/slide-1.svs",
                "slide_timepoint_days": -5,
                "slide_timepoint_source": "Procedure date relative to tumor sequencing",
            },
            {
                "patient_id": "P-1",
                "sample_id": "S-1",
                "match_level": "BLOCK",
                "image_id": "img-2",
                "block_id": "block/1-A1",
                "block_label": "A1",
                "part_description": "Colon",
                "stain_name": "H&E",
                "stain_group": "H&E (Initial)",
                "slide_path": "",
                "slide_timepoint_days": -5,
                "slide_timepoint_source": "Procedure date relative to tumor sequencing",
            },
            {
                "patient_id": "P-1",
                "sample_id": None,
                "match_level": "UNMATCHED",
                "image_id": "img-3",
                "block_id": None,
                "block_label": None,
                "part_description": "Outside consult",
                "stain_name": "PD-L1",
                "stain_group": "IHC",
                "slide_path": "s3://bucket/slide-3.svs",
                "slide_timepoint_days": -3,
                "slide_timepoint_source": "Procedure date relative to tumor sequencing",
            },
        ],
        "study_1",
    )

    assert rows == [
        [
            "P-1",
            "-5",
            "",
            "PATHOLOGY SLIDES",
            "S-1",
            "H&E",
            "BLOCK",
            "Part 1 / Block A1",
            "1",
            "0",
            "1",
            "Procedure date relative to tumor sequencing",
            "/patient/wsiHESlides?studyId=study_1&caseId=P-1&stainFilter=hne&matchLevel=BLOCK&specimenKey=block%3A%3A1%3A%3AA1&sampleId=S-1",
        ],
        [
            "P-1",
            "-5",
            "",
            "PATHOLOGY SLIDES",
            "S-1",
            "H&E",
            "BLOCK",
            "Part 1 / Block A1",
            "0",
            "1",
            "1",
            "Procedure date relative to tumor sequencing",
            "",
        ],
        [
            "P-1",
            "-3",
            "",
            "PATHOLOGY SLIDES",
            "Unmatched",
            "IHC",
            "Unmatched",
            "Outside consult",
            "1",
            "0",
            "1",
            "Procedure date relative to tumor sequencing",
            "/patient/wsiHESlides?studyId=study_1&caseId=P-1&stainFilter=ihc&matchLevel=Unmatched&specimenKey=unmatched%3A%3A%3F%3A%3A%3F",
        ],
    ]


def test_resolved_flags_override_stale_stain_text_for_timeline_subtype():
    rows = build_pathology_timeline_rows(
        [
            {
                "patient_id": "P-2",
                "sample_id": "S-2",
                "match_level": "PART",
                "image_id": "img-4",
                "part_number": "1",
                "part_description": "Lung",
                "stain_name": "H&E, Initial",
                "stain_group": "H&E (Initial)",
                "is_hne": False,
                "is_ihc": True,
                "slide_path": "s3://bucket/slide-4.svs",
                "slide_timepoint_days": 0,
                "slide_timepoint_source": "resolved",
            }
        ],
        "study_2",
    )
    assert rows[0][5] == "IHC"


def test_build_pathology_timeline_rows_uses_canonical_procedure_offset():
    rows = build_pathology_timeline_rows(
        [
            {
                "patient_id": "P-1",
                "sample_id": "S-1",
                "match_level": "PART",
                "image_id": "img-1",
                "part_number": "1",
                "part_description": "Colon",
                "stain_name": "H&E",
                "stain_group": "H&E",
                "slide_path": "s3://bucket/slide-1.svs",
                "procedure_date_days": -17,
                "timepoint_source": "Procedure date",
            }
        ],
        "study_1",
    )

    assert rows[0][1] == "-17"
    assert rows[0][11] == "Procedure date"


def test_write_pathology_timeline_files_returns_written_pair(tmp_path: Path):
    meta_path, data_path, row_count = write_pathology_timeline_files(
        tmp_path,
        "study_1",
        [
            {
                "patient_id": "P-1",
                "sample_id": "S-1",
                "match_level": "PART",
                "image_id": "img-1",
                "part_number": "1",
                "part_description": "Colon",
                "stain_name": "H&E",
                "stain_group": "H&E",
                "slide_path": "s3://bucket/slide-1.svs",
                "procedure_date_days": 0,
            }
        ],
    )

    assert row_count == 1
    assert meta_path.is_file()
    assert data_path.is_file()


def test_build_pathology_timeline_rows_deduplicates_same_image_across_match_buckets():
    rows = build_pathology_timeline_rows(
        [
            {
                "patient_id": "P-1",
                "sample_id": "S-1",
                "match_level": "PART",
                "image_id": "img-1",
                "block_id": "block/1-A1",
                "block_label": "A1",
                "part_description": "Colon",
                "stain_name": "H&E",
                "stain_group": "H&E (Initial)",
                "slide_path": "s3://bucket/slide-1.svs",
                "slide_timepoint_days": -5,
                "slide_timepoint_source": "Procedure date relative to tumor sequencing",
            },
            {
                "patient_id": "P-1",
                "sample_id": "S-1",
                "match_level": "BLOCK",
                "image_id": "img-1",
                "block_id": "block/1-A1",
                "block_label": "A1",
                "part_description": "Colon",
                "stain_name": "H&E",
                "stain_group": "H&E (Initial)",
                "slide_path": "s3://bucket/slide-1.svs",
                "slide_timepoint_days": -5,
                "slide_timepoint_source": "Procedure date relative to tumor sequencing",
            },
        ],
        "study_1",
    )

    assert rows == [
        [
            "P-1",
            "-5",
            "",
            "PATHOLOGY SLIDES",
            "S-1",
            "H&E",
            "BLOCK",
            "Part 1 / Block A1",
            "1",
            "0",
            "1",
            "Procedure date relative to tumor sequencing",
            "/patient/wsiHESlides?studyId=study_1&caseId=P-1&stainFilter=hne&matchLevel=BLOCK&specimenKey=block%3A%3A1%3A%3AA1&sampleId=S-1",
        ]
    ]


def test_build_pathology_timeline_rows_collapses_non_servable_duplicate_specimens():
    rows = build_pathology_timeline_rows(
        [
            {
                "patient_id": "P-1",
                "sample_id": None,
                "match_level": "UNMATCHED",
                "image_id": "img-1",
                "block_id": "part/10-A1",
                "block_label": "A1",
                "part_description": "Colon",
                "stain_name": "H&E",
                "stain_group": "H&E (Initial)",
                "slide_path": "",
                "slide_timepoint_days": 12,
                "slide_timepoint_source": "Procedure date relative to tumor sequencing",
            },
            {
                "patient_id": "P-1",
                "sample_id": None,
                "match_level": "UNMATCHED",
                "image_id": "img-2",
                "block_id": "part/10-B1",
                "block_label": "B1",
                "part_description": "Colon",
                "stain_name": "H&E",
                "stain_group": "H&E (Initial)",
                "slide_path": "",
                "slide_timepoint_days": 12,
                "slide_timepoint_source": "Procedure date relative to tumor sequencing",
            },
        ],
        "study_1",
    )

    assert rows == [
        [
            "P-1",
            "12",
            "",
            "PATHOLOGY SLIDES",
            "Unmatched",
            "H&E",
            "Unmatched",
            "Part 10",
            "0",
            "2",
            "2",
            "Procedure date relative to tumor sequencing",
            "",
        ]
    ]


def test_build_pathology_timeline_rows_skips_unknown_stain_types():
    rows = build_pathology_timeline_rows(
        [
            {
                "patient_id": "P-1",
                "sample_id": "S-1",
                "match_level": "PART",
                "image_id": "img-1",
                "block_id": "part/1-A1",
                "block_label": "A1",
                "part_description": "Colon",
                "stain_name": "SLIDES SUBMITTED",
                "stain_group": "Surgical Submitted",
                "slide_path": "s3://bucket/slide-1.svs",
                "slide_timepoint_days": 8,
                "slide_timepoint_source": "Procedure date relative to tumor sequencing",
            },
            {
                "patient_id": "P-1",
                "sample_id": "S-1",
                "match_level": "PART",
                "image_id": "img-2",
                "block_id": "part/1-A1",
                "block_label": "A1",
                "part_description": "Colon",
                "stain_name": "FROZEN SECTION",
                "stain_group": "Frozen",
                "slide_path": "s3://bucket/slide-2.svs",
                "slide_timepoint_days": 8,
                "slide_timepoint_source": "Procedure date relative to tumor sequencing",
            },
        ],
        "study_1",
    )

    assert rows == []


def test_build_pathology_timeline_rows_keeps_explicitly_classified_other_slides():
    rows = build_pathology_timeline_rows(
        [
            {
                "patient_id": "P-1",
                "sample_id": "S-1",
                "match_level": "BLOCK",
                "image_id": "img-1",
                "block_id": "part/1-A1",
                "block_label": "A1",
                "part_description": "Colon",
                "stain_name": "Unstained recut",
                "stain_group": "Other",
                "is_hne": False,
                "is_ihc": False,
                "slide_path": "s3://bucket/slide-1.svs",
                "slide_timepoint_days": 8,
                "slide_timepoint_source": "Procedure date relative to tumor sequencing",
            },
        ],
        "study_1",
    )

    assert rows == [
        [
            "P-1",
            "8",
            "",
            "PATHOLOGY SLIDES",
            "S-1",
            "Other",
            "BLOCK",
            "Part 1 / Block A1",
            "1",
            "0",
            "1",
            "Procedure date relative to tumor sequencing",
            "/patient/wsiHESlides?studyId=study_1&caseId=P-1&stainFilter=all&matchLevel=BLOCK&specimenKey=block%3A%3A1%3A%3AA1&sampleId=S-1",
        ]
    ]


def test_build_pathology_timeline_rows_sanitizes_multiline_specimen_labels():
    rows = build_pathology_timeline_rows(
        [
            {
                "patient_id": "P-1",
                "sample_id": None,
                "match_level": "UNMATCHED",
                "image_id": "img-1",
                "block_id": None,
                "block_label": None,
                "part_description": "Liver, wedge biopsy\n(20-S-17-000271, B)",
                "stain_name": "H&E",
                "stain_group": "H&E (Initial)",
                "slide_path": "",
                "slide_timepoint_days": -20,
                "slide_timepoint_source": "Procedure date relative\nto tumor sequencing",
            },
        ],
        "study_1",
    )

    assert rows == [
        [
            "P-1",
            "-20",
            "",
            "PATHOLOGY SLIDES",
            "Unmatched",
            "H&E",
            "Unmatched",
            "Liver, wedge biopsy (20-S-17-000271, B)",
            "0",
            "1",
            "1",
            "Procedure date relative to tumor sequencing",
            "",
        ]
    ]


def test_build_pathology_timeline_rows_uses_diagnosis_relative_canonical_fields():
    rows = build_pathology_timeline_rows(
        [
            {
                "patient_id": "P-1",
                "sample_id": "S-1",
                "match_level": "BLOCK",
                "image_id": "img-1",
                "part_number": "1",
                "block_number": "2",
                "part_description": "Colon",
                "stain_name": "H&E",
                "stain_group": "H&E",
                "is_hne": True,
                "is_ihc": False,
                "timeline_start_days": 0,
                "timeline_date_status": "AVAILABLE",
                "can_serve_tiles": True,
            },
            {
                "patient_id": "P-1",
                "sample_id": "S-1",
                "match_level": "BLOCK",
                "image_id": "img-2",
                "part_number": "1",
                "block_number": "2",
                "part_description": "Colon",
                "stain_name": "H&E",
                "stain_group": "H&E",
                "is_hne": True,
                "is_ihc": False,
                "timeline_start_days": None,
                "timeline_date_status": "MISSING_DIAGNOSIS_DATE",
                "can_serve_tiles": True,
            },
        ],
        "study_1",
    )

    assert len(rows) == 1
    assert rows[0][1] == "0"
    assert rows[0][11] == "Procedure date relative to first ICD-O diagnosis"


def test_main_writes_pathology_timeline_files(monkeypatch, tmp_path: Path):
    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "meta_study.txt").write_text(
        "cancer_study_identifier: test_study\n", encoding="utf-8"
    )
    (study_dir / "data_clinical_sample.txt").write_text(
        "\n".join(
            [
                "#Sample Identifier\tPatient Identifier",
                "#Sample identifier\tPatient identifier",
                "#STRING\tSTRING",
                "#1\t1",
                "SAMPLE_ID\tPATIENT_ID",
                "S-1\tP-1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "tools.generate_pathology_timeline_files._fetch_canonical_associations",
        lambda patient_ids, warehouse_id: [
            {
                "patient_id": "P-1",
                "sample_id": "S-1",
                "match_level": "PART",
                "image_id": "img-1",
                "block_id": "part/2-B1",
                "block_label": "B1",
                "part_description": "Liver",
                "stain_name": "H&E",
                "stain_group": "H&E (Other)",
                "slide_path": "s3://bucket/slide-1.svs",
                "slide_timepoint_days": -8,
                "slide_timepoint_source": "Procedure date relative to tumor sequencing",
            }
        ],
    )

    exit_code = main(["--study-dir", str(study_dir)])

    assert exit_code == 0
    meta_contents = (study_dir / "meta_clinical_timeline_pathology_slides.txt").read_text(
        encoding="utf-8"
    )
    assert "datatype: TIMELINE" in meta_contents
    assert "data_filename: data_clinical_timeline_pathology_slides.txt" in meta_contents

    with (study_dir / "data_clinical_timeline_pathology_slides.txt").open(
        encoding="utf-8", newline=""
    ) as handle:
        reader = list(csv.reader(handle, delimiter="\t"))

    assert reader[0] == [
        "PATIENT_ID",
        "START_DATE",
        "STOP_DATE",
        "EVENT_TYPE",
        "SAMPLE_ID",
        "SUBTYPE",
        "MATCH_LEVEL",
        "SPECIMEN",
        "IMAGE_COUNT",
        "NON_SERVABLE_IMAGE_COUNT",
        "TOTAL_IMAGE_COUNT",
        "TIMEPOINT_SOURCE",
        "LINKOUT",
    ]
    assert reader[1] == [
        "P-1",
        "-8",
        "",
        "PATHOLOGY SLIDES",
        "S-1",
        "H&E",
        "PART",
        "Part 2",
        "1",
        "0",
        "1",
        "Procedure date relative to tumor sequencing",
        "/patient/wsiHESlides?studyId=test_study&caseId=P-1&stainFilter=hne&matchLevel=PART&specimenKey=part%3A%3A2&sampleId=S-1",
    ]
