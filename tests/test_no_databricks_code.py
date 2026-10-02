from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_tile_server_has_no_databricks_code():
    # Offline preparation (warehouse queries, thumbnail batches, study export)
    # is maintained outside this repository; the service must not depend on it.
    assert not (ROOT / "databricks.yml").exists()
    for module in ("constants.py", "meta.py", "meta_store.py", "associations.py"):
        assert not (ROOT / "app" / module).exists()
    for filename in (
        "run_wsi_pipelines.py",
        "wsi_canonical_associations_pipeline.sql",
        "wsi_summary_pipeline.sql",
        "stain_classification_schema.sql",
        "stain_metadata_audit.sql",
        "generate_slide_thumbnails.py",
        "export_materialized_hierarchy_snapshot.py",
        "materialize_dev_wsi_snapshot.py",
    ):
        assert not (ROOT / "tools" / filename).exists()
    for path in (ROOT / "app").glob("*.py"):
        assert "databricks" not in path.read_text(encoding="utf-8").lower(), path.name
