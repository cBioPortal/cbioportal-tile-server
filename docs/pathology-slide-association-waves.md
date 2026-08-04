# Normalized WSI release flow

The upstream pathology association dataset remains the source for pathology
structure and slide facts. The tile server loader resolves its stable study,
patient, and sample identifiers against cBioPortal, validates all references,
and writes one append-only release to the normalized ClickHouse tables:

- `wsi_release`
- `wsi_release_patient`
- `wsi_part`
- `wsi_block`
- `wsi_slide`
- `wsi_slide_placement`

The release row is the visibility boundary. A retry receives a new release
ID, and it is inserted only after all normalized rows have been inserted. The
trusted version-2 resource index is generated from the same portal references
and slide IDs. Patient, sample, and slide IDs are scoped to the study;
duplicate slide IDs carry a study-qualified source path. A failed release
restores the prior index. Unknown or mismatched study/patient/sample references, duplicate
placements, and orphan slide associations are rejected before release.

cBioPortal assembles `GET /api/wsi/v2/hierarchy/{studyId}/{patientId}` from the
active normalized release. The response contains only the nested WSI
structure and slide placement facts. Unmatched pathology is represented by a
null `sampleId` and an explicit sample group; there is no `UNMATCHED` sample
record and no flat `slide_associations` payload.

Portal-owned clinical attributes, WSI counts, and pathology timeline events
remain in the normal cBioPortal clinical APIs. The frontend fetches those APIs
and merges labels and metadata into its in-memory viewer state. The tile server
does not assemble or cache patient hierarchies; it serves slide metadata and
tiles and enforces the trusted study-scoped index.

Run the loader with a validated upstream snapshot:

```bash
python3 tools/load_clickhouse_hierarchy.py hierarchy.jsonl \
  --version 20260723030000 \
  --resource-index /var/lib/wsi/wsi-resource-index.json
```

Focused loader and API checks are available with `uv run pytest`; the complete
tile-server suite should pass before publishing a new resource index.
