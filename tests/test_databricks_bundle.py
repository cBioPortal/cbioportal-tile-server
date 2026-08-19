from pathlib import Path

from app.constants import CANONICAL_ASSOCIATION_TABLE, STAIN_CLASSIFICATION_TABLE
import tools.run_wsi_pipelines as wsi_pipelines


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


def test_canonical_pipeline_emits_the_normalized_loader_contract():
    sql_path = (
        Path(__file__).resolve().parent.parent
        / "tools"
        / "wsi_canonical_associations_pipeline.sql"
    )
    sql = sql_path.read_text(encoding="utf-8")

    for column in (
        "part_key",
        "block_key",
        "reference_sample_id",
        "is_hne",
        "is_ihc",
        "can_serve_tiles",
        "specimen_key",
        "procedure_date_days",
        "timepoint_source",
        "slide_path",
        "metadata_is_hne",
        "metadata_is_ihc",
        "stain_name_canonical",
        "stain_group_canonical",
        "stain_name_raw",
        "stain_group_raw",
        "resolved_is_hne",
        "resolved_is_ihc",
        "stain_classification_source",
    ):
        assert column in sql
    assert "reference_sequencing_date" not in sql
    assert "REGEXP_REPLACE" in sql
    assert "association.stain_name_canonical AS stain_name" in sql
    assert "association.stain_group_canonical AS stain_group" in sql


def test_canonical_pipeline_normalizes_common_stain_aliases():
    sql = (
        Path(__file__).resolve().parent.parent
        / "tools"
        / "wsi_canonical_associations_pipeline.sql"
    ).read_text(encoding="utf-8")

    for source, canonical in (
        ("impacttumor", "IMPACT - Tumor"),
        ("recutmolecularhe", "RECUT MOLECULAR H&E"),
        ("androgenreceptorquant", "ANDROGEN RECEPTOR"),
        ("immunorecut%", "IMMUNO RECUT"),
    ):
        assert source in sql
        assert canonical in sql


def test_canonical_pipeline_applies_conservative_stain_policy():
    root = Path(__file__).resolve().parent.parent
    sql = (root / "tools" / "wsi_canonical_associations_pipeline.sql").read_text(
        encoding="utf-8"
    )
    audit_sql = (root / "tools" / "stain_metadata_audit.sql").read_text(
        encoding="utf-8"
    )

    assert "REGEXP_REPLACE(LOWER(COALESCE(normalized.stain_group_clean" in sql
    assert "REGEXP_REPLACE(LOWER(COALESCE(normalized.stain_name_clean" in sql
    assert "stain_name_key = 'sslhe'" in sql
    assert "stain_name_key LIKE '%fish%'" in sql
    assert "stain_group_clean IS NULL" in sql
    assert "metadata_is_fish" not in sql
    assert "nonbinary_queue" in audit_sql
    assert "manual_override" in audit_sql
    assert "curated_ssl_he" in audit_sql
    assert "fish_exclusion" in audit_sql


def test_canonical_pipeline_ranks_manual_adjudications_before_rescoring():
    sql = (
        Path(__file__).resolve().parent.parent
        / "tools"
        / "wsi_canonical_associations_pipeline.sql"
    ).read_text(encoding="utf-8")

    approved_predictions = sql.split("approved_stain_predictions AS (")[1].split(
        "resolved_associations AS ("
    )[0]
    assert "LOWER(TRIM(COALESCE(manual_label, ''))) IN ('he', 'ihc')" in approved_predictions
    assert approved_predictions.index("LOWER(TRIM(COALESCE(manual_label, '')))") < approved_predictions.index(
        "scored_at DESC"
    )


def test_summary_pipeline_uses_resolved_flags():
    sql = (
        Path(__file__).resolve().parent.parent
        / "tools"
        / "wsi_summary_pipeline.sql"
    ).read_text(encoding="utf-8")
    assert "AND (is_hne OR is_ihc)" in sql
    assert "LIKE '%h&e%'" not in sql
    assert "LIKE '%ihc%'" not in sql


def test_pipeline_renderer_rewrites_all_output_names(monkeypatch):
    monkeypatch.setattr(
        wsi_pipelines,
        "CANONICAL_ASSOCIATION_TABLE",
        "cdsi_dev.wsi_test.canonical_slide_associations",
    )
    monkeypatch.setattr(
        wsi_pipelines, "SUMMARY_TABLE", "cdsi_dev.wsi_test.sample_wsi_summary"
    )
    monkeypatch.setattr(
        wsi_pipelines,
        "THUMBNAIL_REGISTRY_TABLE",
        "cdsi_dev.wsi_test.slide_thumbnail_registry",
    )
    monkeypatch.setattr(
        wsi_pipelines,
        "STAIN_CLASSIFICATION_TABLE",
        "cdsi_dev.wsi_test.slide_stain_classification",
    )
    rendered = wsi_pipelines._render(
        "FROM cdsi_prod.pathology_data_mining.slide_thumbnail_registry "
        "JOIN cdsi_prod.pathology_data_mining.canonical_slide_associations c "
        "JOIN cdsi_prod.pathology_data_mining.sample_wsi_summary s "
        "JOIN cdsi_prod.pathology_data_mining.slide_stain_classification p"
    )
    assert "cdsi_prod.pathology_data_mining" not in rendered


def test_pipeline_renderer_rewrites_stain_classification_table(monkeypatch):
    monkeypatch.setattr(
        wsi_pipelines,
        "STAIN_CLASSIFICATION_TABLE",
        "cdsi_dev.wsi_test.slide_stain_classification",
    )
    rendered = wsi_pipelines._render(
        "FROM cdsi_prod.pathology_data_mining.slide_stain_classification"
    )
    assert STAIN_CLASSIFICATION_TABLE not in rendered
    assert "cdsi_dev.wsi_test.slide_stain_classification" in rendered


def test_canonical_pipeline_scopes_unmatched_parts_to_source_accessions():
    sql_path = (
        Path(__file__).resolve().parent.parent
        / "tools"
        / "wsi_canonical_associations_pipeline.sql"
    )
    sql = sql_path.read_text(encoding="utf-8")

    assert "part:unmatched:" in sql
    assert "^(.+/[0-9]+)-[^/]+$" in sql
