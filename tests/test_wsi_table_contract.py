from pathlib import Path

from app import meta_store
from tools.generate_pathology_timeline_files import _ASSOCIATION_QUERY
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


def test_association_transport_matches_pdm_canonical_contract():
    query = meta_store.PATIENT_ASSOCIATION_SQL.lower()
    for column in (
        "timeline_start_days",
        "timeline_date_status",
        "can_serve_tiles",
        "slide_path",
        "tile_metadata_json",
        "thumbnail_url",
        "thumbnail_width",
        "thumbnail_height",
        "thumbnail_content_type",
    ):
        assert column in query
    for retired in ("procedure_date_days", "timepoint_source"):
        assert retired not in query


def test_timeline_query_uses_only_relative_timing_fields():
    query = _ASSOCIATION_QUERY.lower()
    assert "timeline_start_days" in query
    assert "timeline_date_status" in query
    assert "procedure_date_days" not in query
    assert "timepoint_source" not in query


def test_association_preference_does_not_read_retired_procedure_offsets():
    source = (ROOT / "app" / "associations.py").read_text(encoding="utf-8")
    assert 'row.get("procedure_date_days")' not in source
