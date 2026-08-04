# Normalized WSI release flow

The upstream pathology association dataset remains the source for pathology
structure and slide facts. The tile server loader resolves its stable study,
patient, and sample identifiers against cBioPortal, validates all references,
and writes one append-only release to the normalized ClickHouse tables:

- `wsi_release_manifest`
- `wsi_patient_snapshot`
- `wsi_part`
- `wsi_block`
- `wsi_slide`
- `wsi_slide_placement`

The manifest is the release boundary. A retry receives a new release
ID, and the active version is advanced only after all normalized rows have been
inserted. The trusted resource index is generated from the same portal
references and slide IDs; a failed manifest release restores the prior
index. Unknown or mismatched study/patient/sample references, duplicate
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
