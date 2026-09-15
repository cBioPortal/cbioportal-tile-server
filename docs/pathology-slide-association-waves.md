# Normalized WSI snapshot flow

The upstream pathology association dataset remains the source for pathology
structure and slide facts. The Databricks/export pipeline emits the canonical
study files; cBioPortal core resolves stable study, patient, and sample
identifiers, validates all references, and writes one complete snapshot to
the normalized ClickHouse tables:

- `wsi_patient`
- `wsi_part`
- `wsi_block`
- `wsi_slide`
- `wsi_slide_placement`

Blue/green database promotion is the visibility boundary. Build the inactive
database from a fresh schema and import each snapshot once; discard and rebuild
after a failed or repeated import. Patient, sample, and slide IDs are scoped to
the study; duplicate slide IDs carry a study-qualified source path. Unknown or
mismatched study/patient/sample references, duplicate placements, and orphan
slide associations are rejected before import.

cBioPortal assembles `GET /api/wsi/v2/hierarchy/{studyId}/{patientId}` from the
normalized snapshot. The response contains only the nested WSI
structure and slide placement facts. Unmatched pathology is represented by a
null `sampleId` and an explicit sample group; there is no `UNMATCHED` sample
record and no flat `slide_associations` payload.

Portal-owned clinical attributes, WSI counts, and pathology timeline events
remain in the normal cBioPortal clinical APIs. The frontend fetches those APIs
and merges labels and metadata into its in-memory viewer state. The tile server
does not assemble or cache patient hierarchies; it serves source-bound tiles and
thumbnail artifacts. cBioPortal returns intrinsic tile metadata and exact
source URLs in the per-slide access bundle.

## Thumbnail artifact prerequisite

The thumbnail artifact is prepared before the canonical association refresh by
a separate scheduled batch process. The batch reads the slide inventory and
source slides, writes master JPEGs to the S3/Dell ECS-compatible store, and
populates `cdsi_prod.pathology_data_mining.slide_thumbnail_registry` with the
artifact URI, `tile_metadata_json`, dimensions, content type, and a
fingerprint embedded in the metadata JSON. The production canonical SQL is
owned by the [`pdm_databricks_pipelines` WSI bundle](https://github.com/pathology-data-mining/pdm_databricks_pipelines/tree/main/pathology_data_mining/wsi_summary)
and consumes the manifest's serving pointer only when the source URI and
fingerprint match the current preferred inventory. A rewritten
object with the same ECS URL is consequently uncertified until it has been
re-audited and its thumbnail regenerated.

The frontend is read-only and does not upload thumbnails. The tile-server
on-demand worker is limited to development/rehearsal or controlled remediation;
it does not publish registry rows and is not the production path.

Import the validated cBioPortal WSI study files through cBioPortal core:

```bash
metaImport.py -s /path/to/study
```

The tile server does not write ClickHouse. Focused API checks are available
with `uv run pytest`; the complete tile-server suite should pass after the
core import has loaded the snapshot.
