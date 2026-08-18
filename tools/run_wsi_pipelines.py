"""Run the WSI canonical and summary pipelines for a configured namespace.

The checked-in SQL remains production-safe by default.  Set the three
``WSI_*_TABLE`` variables before invoking this command to materialize an
isolated dev/test namespace instead of replacing production tables.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from app import meta_store
from app.constants import (
    CANONICAL_ASSOCIATION_TABLE,
    DEFAULT_WAREHOUSE_ID,
    SUMMARY_TABLE,
    STAIN_CLASSIFICATION_TABLE,
    THUMBNAIL_REGISTRY_TABLE,
)


PRODUCTION_CANONICAL = "cdsi_prod.pathology_data_mining.canonical_slide_associations"
PRODUCTION_SUMMARY = "cdsi_prod.pathology_data_mining.sample_wsi_summary"
PRODUCTION_REGISTRY = "cdsi_prod.pathology_data_mining.slide_thumbnail_registry"
PRODUCTION_STAIN_CLASSIFICATION = "cdsi_prod.pathology_data_mining.slide_stain_classification"


def _render(sql: str) -> str:
    """Render output references from the production template safely."""
    return (
        sql.replace(PRODUCTION_REGISTRY, THUMBNAIL_REGISTRY_TABLE)
        .replace(PRODUCTION_CANONICAL, CANONICAL_ASSOCIATION_TABLE)
        .replace(PRODUCTION_SUMMARY, SUMMARY_TABLE)
        .replace(PRODUCTION_STAIN_CLASSIFICATION, STAIN_CLASSIFICATION_TABLE)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--warehouse-id",
        default=os.environ.get("DATABRICKS_WAREHOUSE_ID", DEFAULT_WAREHOUSE_ID),
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    for filename in (
        "stain_classification_schema.sql",
        "wsi_canonical_associations_pipeline.sql",
        "wsi_summary_pipeline.sql",
    ):
        sql = _render((root / filename).read_text(encoding="utf-8"))
        if "stain_classification" in filename:
            target = STAIN_CLASSIFICATION_TABLE
        else:
            target = CANONICAL_ASSOCIATION_TABLE if "canonical" in filename else SUMMARY_TABLE
        print(f"running {filename} -> {target}", flush=True)
        meta_store.run_statement(sql, args.warehouse_id)
        print(f"completed {filename}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
