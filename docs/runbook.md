# cBioPortal WSI tile-server runbook

This service is a source-bound pixel reader. cBioPortal owns authentication,
study authorization, hierarchy, and the slide access bundle. The tile server
does not resolve image IDs, query Databricks, read a resource index, search,
or expose clinical metadata.

## Source of truth

- `../cbioportal` serves `/api/wsi/v2/hierarchy/{studyId}/{patientId}` and
  `/api/wsi/v2/slides/{studyId}/{imageId}/access` from ClickHouse.
- `../cbioportal-frontend` requests that access bundle and sends its exact
  URLs plus the returned Bearer capability to this service.
- The deployment repository owns ingress, probes, secrets, and rollout
  resources.

The backend access response is the only online input needed beyond the shared
secret. It contains `sourceUrl`, `thumbnail.sourceUrl`, dimensions, intrinsic
tile metadata, and a v2 token. The token binds both URLs by SHA-256.

## Production topology

The existing `/wsi` ingress may route to this service. Keep `/health` and
`/ready` public for orchestration; all other routes require a Bearer token.
Ingress and deployment configuration are authoritative for timeouts, network
policy, worker count, and block-cache volumes.

## Required environment

```text
AWS_ENDPOINT_URL=<S3-compatible endpoint>
AWS_ACCESS_KEY_ID=<object-store key>
AWS_SECRET_ACCESS_KEY=<object-store secret>
WSI_AUTH_SECRET=<same at-least-32-byte secret as cBioPortal>
WSI_AUTH_AUDIENCE=cbioportal-wsi
WSI_AUTH_MAX_TTL=300
WSI_ALLOWED_SOURCE_SCHEMES=s3
WSI_ALLOWED_SOURCE_PREFIXES=<approved source URI prefixes>
WSI_ALLOWED_THUMBNAIL_PREFIXES=<approved thumbnail URI prefixes>
REDIS_URL=<password-protected Redis URL>
```

The backend should use `wsi.access-token-ttl-seconds=300`. Do not set a tile
server TTL lower than the backend TTL. `WSI_AUTH_REQUIRED` is retained as a
legacy configuration key but authentication is mandatory for pixel routes.
The URI prefix allowlists are a de-identification boundary; leave them empty
only for an isolated non-publishing unit test.

## Endpoints and smoke checks

```bash
curl -fsS https://cbioportal.example.org/wsi/health
curl -fsS https://cbioportal.example.org/wsi/ready
curl -i https://cbioportal.example.org/wsi/tiles/zxy/0/0/0?source=s3%3A%2F%2Fbucket%2Fslide.svs
```

The final command must return `401` without `Authorization`. With a fresh
bundle from cBioPortal, use the returned source URL and token:

```bash
curl -fsS \
  -H "Authorization: Bearer ${WSI_TOKEN}" \
  "https://cbioportal.example.org/wsi/tiles/zxy/0/0/0?source=$(python -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$WSI_SOURCE")"
```

Also verify that changing one character of the source URL returns `403`, that
an expired token returns `401`, and that the thumbnail endpoint accepts only
the artifact URL bound in the same token.

## Data preparation

The offline pipeline loads each WSI slide row with:

```text
source_url, tile_metadata_json, thumbnail_url,
thumbnail_width, thumbnail_height, thumbnail_content_type
```

The cBioPortal core importer publishes `can_serve_tiles=true` only when all
fields are complete; otherwise the hierarchy reports the slide as unavailable.
No registry or manifest is mounted into the online tile-server pod.

Thumbnail generation and publication, study snapshot export and refresh, and
the Slurm batch operations (status, resume, repair) are owned by
[`pdm_databricks_pipelines/pathology_data_mining/wsi_tools`](https://github.com/pathology-data-mining/pdm_databricks_pipelines/tree/main/pathology_data_mining/wsi_tools);
the canonical-association and summary refresh is the
[`wsi_summary`](https://github.com/pathology-data-mining/pdm_databricks_pipelines/tree/main/pathology_data_mining/wsi_summary) bundle. A slide becomes servable only after
the thumbnail batch, the `wsi_summary` refresh, study-file export, and the
cBioPortal core study import have all completed.

The frontend only requests `/thumbnails` and never uploads artifacts. The
tile-server `app/thumbnail_worker.py` CLI can be used for development,
rehearsal, or controlled remediation, but it writes only an object-store
artifact and does not populate the registry. It must not be used as the
production source of truth.

## Response and cache policy

- Tiles: `private, max-age=3600`, vary on `Authorization`.
- Thumbnails: `private, max-age=300`, vary on `Authorization`.
- Redis is an optimization, never an authorization boundary.
- Overview decodes that exceed `MAX_DECODE_PIXELS` return HTTP 422 with
  `overview_requires_preprocessing`.

Application logs must not include tokens, source URLs, patient IDs, or slide
IDs. Keep operation type, dimensions, status, timing, and exception class.

## Local integration

The local cBioPortal compose rehearsal should pass the same
`WSI_AUTH_SECRET`/audience to the backend and tile server. For mounted local
slides, explicitly set `WSI_ALLOWED_SOURCE_SCHEMES=s3,file` and include
`file:///app/testdata/` in both URI prefix allowlists; production should remain
`s3` only. Generate a v2 access bundle through the backend before
testing a pixel request.
