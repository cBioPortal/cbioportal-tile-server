# Normalized WSI release flow

The upstream pathology association dataset remains the source for pathology
structure and slide facts. The Databricks/export pipeline emits the canonical
study files; cBioPortal core resolves stable study, patient, and sample
identifiers, validates all references, and writes one append-only release to
the normalized ClickHouse tables:

- `wsi_release`
- `wsi_release_patient`
- `wsi_part`
- `wsi_block`
- `wsi_slide`
- `wsi_slide_placement`

The release row is the visibility boundary. A retry receives a new release
ID, and it is inserted only after all normalized rows have been inserted. Patient,
sample, and slide IDs are scoped to the study; duplicate slide IDs carry a
study-qualified source path. Unknown or mismatched study/patient/sample references, duplicate
placements, and orphan slide associations are rejected before release.

cBioPortal assembles `GET /api/wsi/v2/hierarchy/{studyId}/{patientId}` from the
active normalized release. The response contains only the nested WSI
structure and slide placement facts. Unmatched pathology is represented by a
null `sampleId` and an explicit sample group; there is no `UNMATCHED` sample
record and no flat `slide_associations` payload.

Portal-owned clinical attributes, WSI counts, and pathology timeline events
remain in the normal cBioPortal clinical APIs. The frontend fetches those APIs
and merges labels and metadata into its in-memory viewer state. The tile server
does not assemble or cache patient hierarchies; it serves source-bound tiles and
thumbnail artifacts. cBioPortal returns intrinsic tile metadata and exact
source URLs in the per-slide access bundle.

Import the validated cBioPortal WSI study files through cBioPortal core:

```bash
metaImport.py -s /path/to/study
```

The tile server does not write ClickHouse. Focused API checks are available
with `uv run pytest`; the complete tile-server suite should pass after the
core import has published a release.
