import json

from app.resource_index import ResourceIndex


def test_suggestions_are_prefix_ordered_and_revision_changes(tmp_path):
    path = tmp_path / "resource-index.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "studies": {
                    "study": {
                        "patients": ["P-002", "P-001", "P-010"],
                        "samples": [],
                        "slides": {},
                    }
                },
            }
        )
    )
    index = ResourceIndex(str(path))
    first_revision = index.revision()

    assert [item["id"] for item in index.suggestions("study", "P-0")] == [
        "P-001",
        "P-002",
        "P-010",
    ]

    path.write_text(
        json.dumps(
            {
                "version": 2,
                "studies": {
                    "study": {
                        "patients": ["P-001", "P-003"],
                        "samples": [],
                        "slides": {},
                    }
                },
            }
        )
    )

    assert index.revision() != first_revision
    assert [item["id"] for item in index.suggestions("study", "P-")] == [
        "P-001",
        "P-003",
    ]
