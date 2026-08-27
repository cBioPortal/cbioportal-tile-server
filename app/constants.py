"""
Single source of truth for Databricks table names and warehouse defaults.

Imported by both app/meta.py and tools/generate_resource_patient.py so that
changes to table names or the warehouse ID only need to be made here.
"""

import os
import re


_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2}$")


def _table_name(environment_name: str, default: str) -> str:
    """Return a configured three-part Unity Catalog table name.

    The dev stack uses this to point at isolated ``*_dev`` output tables.  Do
    not accept arbitrary SQL here: these values are interpolated into
    operational statements and must remain identifiers.
    """
    value = os.environ.get(environment_name, default).strip()
    if not _TABLE_NAME.fullmatch(value):
        raise ValueError(f"{environment_name} must be a three-part table name")
    return value

#: De-identified slide ↔ clinical join table (PHI-restricted via Unity Catalog)
DEID_TABLE = "cdsi_eng_phi.pdm_base_tables_dev.impact_block_matched_slides_v1"

#: Legacy part-level sample ↔ slide relation with broader coverage than block matching
PART_MATCH_TABLE = "cdsi_eng_phi.pdm_base_tables.impact_matched_slides"

#: Cleaned slide-level universe used to scope diagnostic pathology coverage
CLEANED_SLIDE_TABLE = "cdsi_eng_phi.pdm_base_tables_dev.case_breakdown_cleaned_v2"

#: Slide file inventory — contains s3:// paths for each image_id. The isolated
#: dev snapshot overrides this with its source table.
INVENTORY_TABLE = _table_name(
    "WSI_INVENTORY_TABLE", "cdsi_eng_phi.pdm_base_tables.slide_inventory"
)

#: Default Databricks SQL warehouse (can be overridden via DATABRICKS_WAREHOUSE_ID)
DEFAULT_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "0b49b7d78734ad5c")

#: Pre-computed slide availability summary table (written nightly by the Asset Bundle job)
SUMMARY_TABLE = _table_name(
    "WSI_SUMMARY_TABLE", "cdsi_prod.pathology_data_mining.sample_wsi_summary"
)

# Approved image-assisted stain classifications. Missing rows intentionally
# fall back to the source metadata classification.
STAIN_CLASSIFICATION_TABLE = _table_name(
    "WSI_STAIN_CLASSIFICATION_TABLE",
    "cdsi_prod.pathology_data_mining.slide_stain_classification",
)

#: Canonical patient/sample/slide association table (written nightly by the Asset Bundle job)
CANONICAL_ASSOCIATION_TABLE = _table_name(
    "WSI_CANONICAL_ASSOCIATION_TABLE",
    "cdsi_prod.pathology_data_mining.canonical_slide_associations",
)

# Incremental thumbnail publication registry used by the on-prem renderer.
THUMBNAIL_REGISTRY_TABLE = _table_name(
    "WSI_THUMBNAIL_REGISTRY_TABLE",
    "cdsi_prod.pathology_data_mining.slide_thumbnail_registry",
)

# Effective, fingerprint-bound serving pointers produced by the PDM control
# plane. Thumbnail generation follows this table so immutable promotions are
# rendered at their promoted URI instead of regenerating the original object.
SERVING_MANIFEST_TABLE = _table_name(
    "WSI_SERVING_MANIFEST_TABLE",
    "cdsi_prod.pathology_data_mining.wsi_serving_manifest",
)
