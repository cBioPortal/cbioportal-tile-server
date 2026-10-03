# cBioPortal WSI tile-server runbook

This service is a capability-bound pixel reader. cBioPortal owns authentication,
study authorization, hierarchy, and the slide access bundle. The tile server
does not resolve image IDs, query Databricks, read a resource index, search,
or expose clinical metadata.

## Source of truth

- `../cbioportal` serves `/api/wsi/v2/hierarchy/{studyId}/{patientId}` and
  `/api/wsi/v2/slides/{studyId}/{slideKey}/access` from ClickHouse.
- `../cbioportal-frontend` requests that access bundle and sends only the
  returned Bearer capability to this service; it never sees or sends a
  source URL.
- The deployment repository owns ingress, probes, secrets, and rollout
  resources.

The backend access response is the only online input needed beyond the shared
secret. It contains `slideKey`, thumbnail dimensions, intrinsic tile metadata,
and a `wsi_auth_version=3` token. The token's `enc` claim carries the real
`image_id` and both source URLs encrypted with AES-256-GCM (HKDF-SHA256 of the
shared secret, info `wsi-claim-enc-v3`, AAD = `slide_key`); the tile server
decrypts it and reads only those URLs. v2 tokens are rejected. Contract:
`wsi-serving-v5` (`../docs/wsi-deid-slide-key-contract.md`).

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
curl -i https://cbioportal.example.org/wsi/tiles/zxy/0/0/0
```

The final command must return `401` without `Authorization`. With a fresh
bundle from cBioPortal, use only the returned token:

```bash
curl -fsS \
  -H "Authorization: Bearer ${WSI_TOKEN}" \
  "https://cbioportal.example.org/wsi/tiles/zxy/0/0/0"
```

Also verify that adding `?source=...` or an `X-WSI-Source` header returns
`400`, that changing one character of the token returns `401`, that an
expired token returns `401`, and that a v2 token returns `401`.

## Data preparation

The offline pipeline loads each WSI slide row with:

```text
source_url, tile_metadata_json, thumbnail_url,
thumbnail_width, thumbnail_height, thumbnail_content_type
```

The cBioPortal core importer publishes `can_serve_tiles=true` only when all
fields are complete; otherwise the hierarchy reports the slide as unavailable.
No registry or manifest is mounted into the online tile-server pod.

Thumbnail generation, study-file export, and any batch scheduling are owned by
the deployment's offline preparation pipeline, not this repository. A slide
becomes servable only after its thumbnail is published, the study files are
exported, and cBioPortal core has imported them. See the README's
"Offline preparation" section for the full contract.

The frontend only requests `/thumbnails` and never uploads artifacts. The
tile-server `app/thumbnail_worker.py` CLI can be used for development,
rehearsal, or controlled remediation, but it writes only an object-store
artifact. It must not be used as the production source of truth.

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
