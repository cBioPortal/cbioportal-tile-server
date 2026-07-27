# Pathology Slide Association Waves

This document tracks the current implementation state for the slide-association
cleanup and performance work.

## Current state

Completed in the tile server:

- Python-side serializer dedupe in `app/meta.py` for repeated association rows.
- SQL-side canonicalization in `app/meta_store.py` inside `PATIENT_SQL`:
  repeated association rows are collapsed before the `/patient/{patient_id}`
  payload is assembled.
- A materialized upstream SQL asset now exists at
  `tools/wsi_canonical_associations_pipeline.sql`, with the Databricks bundle
  configured to build
  `cdsi_prod.pathology_data_mining.canonical_slide_associations` nightly
  before the WSI summary pipeline runs.
- The tile server association read path now uses
  `cdsi_prod.pathology_data_mining.canonical_slide_associations` exclusively.
- Regression coverage in `tests/test_meta.py` for duplicate
  `slide_associations`.
- A study-file generator now exists at
  `tools/generate_pathology_timeline_files.py` to emit canonical
  `PATHOLOGY SLIDES` timeline events from the shared Databricks snapshot.

Verified on July 19, 2026:

- Focused metadata tests pass:
  `uv run pytest tests/test_meta.py -k 'duplicate_slide_associations_are_deduplicated or single_slide_full_nesting or two_slides_same_block'`
- Live tile-server payload for `P-0002438` at `http://pllimsksparky3:8081`
  no longer returns duplicate H&E associations at `-142` days.
- The materialized canonical Databricks table now exists at
  `cdsi_prod.pathology_data_mining.canonical_slide_associations`.
- Representative canonical validation now passes for:
  - `P-0002438`
  - `P-0048660`
  - `P-0011144`
- Focused pathology timeline generator coverage exists in
  `tests/test_generate_pathology_timeline_files.py`.

## What this wave does

- Prevents duplicate `slide_associations` from inflating:
  - pathology clinical-data counts
  - summary pathology timeline counts
  - match-filter counts in the WSI viewer
- Keeps `sample_id = null` as the canonical representation for unmatched slides.
- Produces a standard cBioPortal `TIMELINE` study file so pathology slide
  events load through the existing `clinical_event` import path and become
  available from the ClickHouse-backed clinical events API after study reload.

## Runtime prerequisite

- The canonical association table must be available before the tile server is
  deployed. The Databricks bundle builds it before the WSI summary pipeline.
- It does not move patient summary, clinical-data pathology rows, or study-view
  pathology counts off runtime augmentation and onto ClickHouse query paths by
  itself; it only loads canonical pathology events into the cBioPortal
  database.
- It does not fully cut over downstream aggregation to the canonical table yet.

## Remaining waves

### Wave 1 remainder

- The canonical association logic is now promoted to a shared Databricks table
  and the nightly bundle task builds it before downstream consumers run.
- Add explicit `association_version`, `updated_at`, `sample_bucket`, and
  `sample_label` in the shared upstream dataset.
  Status on July 18, 2026: these fields are now emitted by
  `tools/wsi_canonical_associations_pipeline.sql`, and downstream study-file
  generation writes a `wsi_snapshot_manifest.json` derived from that snapshot.

### Wave 2

- Point all tile-server association reads at the shared Databricks dataset.
- Retire legacy WSI sample clinical attributes and timepoint columns from
  study files before reloads.
- Add targeted hierarchy snapshot invalidation tied to backfills.
  Status on July 17, 2026: tile-server Redis patient cache eviction is now
  available through `app.cache.delete_patient(...)` and the operational helper
  `tools/invalidate_patient_cache.py`.
- Study refresh tools now support `--invalidate-patient-cache` so a targeted
  regenerate-and-reload workflow can refresh stale hierarchy rows for the study
  cohort in the same run:
  - `tools/generate_wsi_clinical_attrs.py`
  - `tools/generate_wsi_timepoint_clinical_attrs.py`
  - `tools/generate_resource_patient.py`

### Wave 3

- Load canonical association rows into ClickHouse.
- Build aggregate ClickHouse tables for:
  - patient summary pathology timeline
  - patient clinical-data pathology rows
  - study pathology sorting and counts
  Status on July 19, 2026: canonical pathology timeline events can now be
  generated as study `TIMELINE` files and imported into `clinical_event`.
  Dedicated ClickHouse aggregates for pathology-specific grouping and study
  sorting remain pending.

### Wave 4

- Build validation harnesses comparing:
  - canonical Databricks associations
  - backend `/api/wsi/hierarchy/{study_id}/{patient_id}` payloads
  - ClickHouse aggregates
  Status on July 17, 2026: a canonical Databricks validation helper now exists
  at `tools/validate_canonical_associations.py`. Validation against tile-server
  payloads and ClickHouse aggregates is still pending.

### Wave 5

- Cut patient summary pathology reads over to ClickHouse aggregates.
- Cut patient clinical-data pathology reads over to ClickHouse aggregates.
- Cut study-view pathology counts and sort paths over to ClickHouse aggregates.

## Operational note

The frontend dev server on `pllimsksparky3:3000` proxies `/patient/P-*` to the
local tile server on `localhost:8081`. Tile-server code changes require a tile
server rebuild and restart on that host before the frontend picks them up.
