from pathlib import Path

from app import meta_store
from app.constants import (
    CANONICAL_ASSOCIATION_TABLE,
    SERVING_MANIFEST_TABLE,
    SUMMARY_TABLE,
)


ROOT = Path(__file__).resolve().parent.parent


def test_runtime_reads_the_pdm_owned_table_contract():
    assert meta_store._CANONICAL_ASSOCIATIONS == CANONICAL_ASSOCIATION_TABLE
    assert meta_store._SUMMARY == SUMMARY_TABLE
    assert SERVING_MANIFEST_TABLE.count(".") == 2


def test_summary_reader_uses_only_deidentified_summary_fields():
    source = (ROOT / "app" / "meta.py").read_text(encoding="utf-8")
    summary_query = source.split("FROM {meta_store._SUMMARY}", 1)[0]
    for column in (
        "sample_id",
        "patient_id",
        "servable_slide_count",
        "non_servable_hne_slide_count",
        "non_servable_ihc_slide_count",
        "has_hne",
        "has_ihc",
        "stain_types",
    ):
        assert column in summary_query
    assert "mrn" not in summary_query.lower()
    assert "procedure_date" not in summary_query.lower()
    assert "release_id" not in summary_query.lower()


def test_tile_server_has_no_production_databricks_bundle():
    assert not (ROOT / "databricks.yml").exists()
    for filename in (
        "run_wsi_pipelines.py",
        "wsi_canonical_associations_pipeline.sql",
        "wsi_summary_pipeline.sql",
        "stain_classification_schema.sql",
        "stain_metadata_audit.sql",
    ):
        assert not (ROOT / "tools" / filename).exists()
