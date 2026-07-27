from pathlib import Path

from app.constants import CANONICAL_ASSOCIATION_TABLE


def test_canonical_association_sql_asset_exists():
    sql_path = (
        Path(__file__).resolve().parent.parent
        / "tools"
        / "wsi_canonical_associations_pipeline.sql"
    )
    assert sql_path.exists()
    sql = sql_path.read_text(encoding="utf-8")
    assert f"CREATE OR REPLACE TABLE {CANONICAL_ASSOCIATION_TABLE}" in sql


def test_databricks_bundle_references_canonical_association_task():
    bundle_path = Path(__file__).resolve().parent.parent / "databricks.yml"
    contents = bundle_path.read_text(encoding="utf-8")

    assert "task_key: compute-canonical-associations" in contents
    assert "path: tools/wsi_canonical_associations_pipeline.sql" in contents
    assert "depends_on:" in contents
    assert "- task_key: compute-canonical-associations" in contents


def test_canonical_pipeline_prefers_reef_inventory_paths():
    reef_pattern = "s3://mskmind-bkt/reef-slides/"
    sql_path = (
        Path(__file__).resolve().parent.parent
        / "tools"
        / "wsi_canonical_associations_pipeline.sql"
    )
    sql = sql_path.read_text(encoding="utf-8")
    assert reef_pattern in sql
