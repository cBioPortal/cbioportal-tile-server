import re
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
    # The assistant may call a Databricks model-serving endpoint through the
    # OpenAI client (app/agent.py, configured in app/config.py), and the
    # annotation pool may fetch a Lakebase database credential from the
    # workspace REST API (app/lakebase_credentials.py); no module may use a
    # Databricks SDK, connector or REST job/SQL API.
    forbidden = re.compile(
        r"^\s*(?:from|import)\s+(?:databricks|pyspark|dbutils)\b|/api/2\.\d/(?:jobs|sql)",
        re.MULTILINE,
    )
    provider_modules = {"agent.py", "config.py", "lakebase_credentials.py"}
    for path in (ROOT / "app").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not forbidden.search(text), path.name
        if path.name not in provider_modules:
            assert "databricks" not in text.lower(), path.name
