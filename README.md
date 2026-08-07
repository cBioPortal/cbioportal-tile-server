# cbioportal-tile-server

FastAPI tile server for cBioPortal whole-slide imaging (WSI). It serves slide
tiles, thumbnails, and slide metadata to OpenSeadragon and related cBioPortal
clients.

This service is not the cBioPortal frontend and it is not the Spring backend.
It is a separate runtime with its own storage, authorization, and deployment
concerns.

## Architecture at a glance

For operators standing this up outside the MSK environment, the most important
deployment boundary is:

- `cBioPortal` authenticates the user, checks study access, and issues a
  short-lived WSI Bearer token.
- `cbioportal-tile-server` validates that token, enforces the trusted
  study-to-resource index, and serves slide data.
- patient hierarchy is served by the authenticated cBioPortal backend, not by
  a tile-server fallback route.
- an upstream data-preparation pipeline publishes the slide/resource index and
  loads the corresponding WSI hierarchy release used by cBioPortal.

Databricks is not a universal platform requirement for WSI. In this repository
today:

- tile and thumbnail authorization is driven by `WSI_RESOURCE_INDEX_FILE`
- authenticated slide path resolution can come from the trusted index
- unauthenticated search and fallback slide-path lookup use Databricks metadata
- `/slides/{image_id}/dbmeta` returns a restricted diagnostic metadata subset

If your institution does not use Databricks, you can replace the upstream ETL
platform, but you must still provide equivalent published inputs and either:

- avoid the Databricks-dependent endpoints and flows, or
- adapt this service to resolve metadata/search from your own source of truth

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe; returns `503` when auth is enabled but the trusted resource index is unavailable |
| GET | `/slides/{image_id}/dbmeta` | Restricted diagnostic metadata for a slide |
| GET | `/search?q=` | Autocomplete suggestions |
| GET | `/tiles/{slide_id}/metadata` | Slide dimensions, zoom levels, MPP |
| GET | `/thumbnails/{slide_id}` | JPEG thumbnail from a pre-rendered artifact |
| GET | `/tiles/{slide_id}/zxy/{z}/{x}/{y}` | ZXY tile (JPEG) |
| GET | `/tiles/{slide_id}/warmup` | Prime overview reads for a slide |

The same endpoints are also available under the explicit `/wsi` namespace,
for example `/wsi/tiles/{slide_id}/...`.

All endpoints except `/health` and `/ready` require a cBioPortal-issued short-lived WSI
capability in the header:

```text
Authorization: Bearer <token>
```

The token must be an HMAC-SHA256 JWT with the configured audience,
`scope=wsi:read`, a non-empty subject, `study_id`, `wsi_auth_version=1`, and
valid `iat`/`exp` claims whose lifetime does not exceed `WSI_AUTH_MAX_TTL`.
The `/wsi/health` and `/wsi/ready` ingress aliases are rewritten to the probe
routes. Do not disable these checks in production.

## What this service needs

At runtime, a production deployment needs:

1. slide/image storage reachable by this service
2. a trusted resource index published to `WSI_RESOURCE_INDEX_FILE`
3. matching WSI token settings shared with cBioPortal
4. any metadata backend required by the specific endpoints you expose

Setting a tile-server URL in cBioPortal is not enough by itself. You must also
publish the resource index, load the matching WSI hierarchy into cBioPortal's
database, and configure slide storage access for this service.

## Quick start

```bash
python3 tools/write_dev_env.py          # securely populate .env from local credentials
printf 'WSI_AUTH_SECRET=%s\nREDIS_PASSWORD=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 24)" >> .env
docker compose up --build
```

The checked-in Docker Compose setup reflects the current MSK-oriented
development environment. Treat it as a local rehearsal, not as a complete
portable production recipe.

## Configuration

All settings are environment variables (see `app/config.py`):

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ENDPOINT_URL` | — | S3-compatible object-store endpoint; Dell ECS in the current production deployment |
| `AWS_ACCESS_KEY_ID` | — | Object-store access key |
| `AWS_SECRET_ACCESS_KEY` | — | Object-store secret key |
| `DATABRICKS_HOST` | — | Databricks workspace URL; required only for Databricks-backed metadata flows |
| `DATABRICKS_TOKEN` | — | Databricks PAT; required only for Databricks-backed metadata flows |
| `DATABRICKS_WAREHOUSE_ID` | `0b49b7d78734ad5c` | Databricks SQL warehouse for metadata/search queries |
| `WSI_AUTH_SECRET` | — | At least 32 bytes; shared with the cBioPortal capability issuer |
| `WSI_AUTH_AUDIENCE` | `cbioportal-wsi` | Capability-token audience |
| `WSI_AUTH_REQUIRED` | `true` | Require Bearer capabilities for non-health routes |
| `WSI_AUTH_MAX_TTL` | `300` | Maximum WSI token lifetime in seconds |
| `WSI_RESOURCE_INDEX_FILE` | — | Loader-published version-2 study/resource binding; required when auth is enabled |
| `TILE_SIZE` | `256` | Tile edge length in pixels |
| `JPEG_QUALITY` | `85` | JPEG encoding quality |
| `MAX_DECODE_PIXELS` | `4194304` | Maximum source pixels a single on-demand decode may read before the request is rejected |
| `THUMBNAIL_MAX_DECODE_PIXELS` | `16000000` | Maximum source pixels for a bounded thumbnail overview decode |
| `THUMBNAIL_TIMEOUT_SEC` | `8` | Deadline for process-isolated on-demand thumbnail generation |
| `THUMBNAIL_PLACEHOLDER_CACHE_TTL` | `60` | Redis/browser cache lifetime for placeholder thumbnails in seconds |
| `THUMBNAIL_MANIFEST_URI` | — | JSON manifest listing available pre-rendered thumbnails |
| `THUMBNAIL_MASTER_SIZE` | `1024` | Maximum edge length of generated thumbnail masters |
| `THUMBNAIL_MANIFEST_REFRESH_SEC` | `300` | In-process manifest refresh interval in seconds |
| `THUMBNAIL_GENERATED_RECORD_CACHE_CAPACITY` | `4096` | Maximum transient on-demand thumbnail records retained per worker |
| `THUMBNAIL_BATCH_TIMEOUT_SEC` | `600` | Maximum time for one isolated offline slide render |
| `REDIS_URL` | `redis://redis:6379` | Redis connection; use a password-protected URL in production |
| `TILE_CACHE_TTL` | `86400` | Tile cache TTL in seconds; `0` means no expiry |
| `THUMBNAIL_CACHE_TTL` | `86400` | Thumbnail cache TTL in seconds |
| `METADATA_CACHE_TTL` | `86400` | Metadata cache TTL in seconds |
| `MAX_OPEN_SLIDES` | `64` | LRU slide cache capacity; benchmark-backed default |
| `N_WORKERS` | `4` | Gunicorn worker count |
| `MAX_IMAGE_OPERATIONS` | `2` | Per-worker cap for concurrent slide opens and decodes |
| `PATH_CACHE_CAPACITY` | `4096` | In-process LRU size for slide ID to path lookups |
| `BLOCKCACHE_PATH` | `/cache/slide-blocks` (Docker Compose) | Local block cache directory for SVS range reads; set empty to disable |
| `BLOCKCACHE_BLOCK_SIZE` | `8388608` | Block-cache block size in bytes |
| `CORS_ORIGINS` | internal MSK cBioPortal origins | Comma-separated allowed origins |
| `WSI_TEST_SLIDE_MAP_FILE` | — | Test-only JSON file mapping slide IDs to local mounted `.svs` files |
| `RATE_LIMIT_PER_MINUTE` | `120` | Per-client limit for expensive requests; `0` disables the in-process limiter |

Tile and thumbnail responses are private-cacheable (`max-age=3600` and
`max-age=300` respectively). Slide metadata and search responses
are `private, no-store` because they may contain PHI. Redis is an optimization
only; requests must continue to work if the cache is unavailable.

Thumbnail requests normally resolve a pre-rendered JPEG master from
`THUMBNAIL_MANIFEST_URI` and downsize it when the request is smaller than the
stored master. On a cache miss, the server starts one short-lived,
concurrency-capped worker process to generate and persist the master. The
worker may use the heavier fallback needed for slides without a safe overview;
that fallback never runs inside the API process. Timeouts and failures return a
placeholder JPEG with `X-Thumbnail-Status` and `X-Thumbnail-Reason` headers.

If a slide lacks a sufficiently downsampled overview pyramid, overview-tile
requests still return HTTP `422` with
`{"error":"overview_requires_preprocessing"}`. Thumbnail misses are handled
by the isolated worker described above.

## Dependency matrix

The service does not use every backend for every route. The current behavior is:

| Capability | Trusted resource index | Slide object storage | Databricks |
|-----------|-------------------------|----------------------|------------|
| authz for protected slide/sample resources and patient search suggestions | required | no | no |
| authenticated slide path resolution | required | yes | no |
| tile and thumbnail serving | indirect | required | no |
| `/tiles/{slide_id}/metadata` | indirect | required | no |
| `/tiles/{slide_id}/warmup` | indirect | required | no |
| authenticated `/search` | required | no | no |
| unauthenticated `/search` | no | no | required |
| `/slides/{image_id}/dbmeta` | authz guard only | no | required |
| unauthenticated fallback slide-path lookup | no | no | required |

This means:

- a private-study production rollout must have the trusted resource index
- tile serving itself does not require a live Databricks query path
- the cBioPortal backend owns patient hierarchy authorization and responses
- the current implementation still uses Databricks for restricted diagnostic metadata and
  unauthenticated development fallback behavior

If you need a deployment with no Databricks dependency at all, plan to replace
or remove the Databricks-backed endpoints and lookup paths.

## Study isolation contract

Authenticated production mode (`WSI_AUTH_REQUIRED=true`) authorizes every
protected resource against
the `WSI_RESOURCE_INDEX_FILE` produced from the materialized hierarchy
snapshot. The token's `study_id` is authoritative. A `studyId` query parameter
may be supplied by the frontend only as a consistency check and is never
trusted on its own.

The mapping is required for slide metadata, thumbnails, tiles, warmup, restricted
`/slides/{id}/dbmeta`, and `/search`. A token
for study A must return `403` for a patient or slide bound only to study B;
missing or invalid capabilities return `401`. A missing or invalid trusted
index fails closed with `503`. Search results are generated from the token
study's index entries. Resource identifiers are scoped to the token's study in
the published index, so the same stable patient, sample, or slide ID may
safely occur in another study. Every authorized slide resolves through its
study-qualified source-path binding.

The cBioPortal backend and this service must use the same secret bytes,
audience (`cbioportal-wsi`), and compatible TTL (`300` seconds from the
backend, no more than `WSI_AUTH_MAX_TTL`). Protected responses use private
cache headers; Redis and an HTTP cache are never authorization boundaries.

Unauthenticated local/development mode (`WSI_AUTH_REQUIRED=false`) is retained
for public fixtures only. It must not be used for private-study deployment.

## PHI-safe logging

Application logs must not contain patient IDs, slide IDs, search terms, S3 paths,
or exception text that may contain those values. Request outcome logs may retain
operation type, dimensions, status/reason, timing, and exception class. Review
ingress and proxy access-log policies separately because they are owned by the
deployment repository.

## Bring-up checklist

Before debugging viewer behavior, verify all of the following:

1. cBioPortal can issue a WSI token for the target study.
2. `WSI_AUTH_SECRET` and `WSI_AUTH_AUDIENCE` match the cBioPortal backend.
3. `WSI_AUTH_MAX_TTL` is greater than or equal to the backend-issued token TTL.
4. `WSI_RESOURCE_INDEX_FILE` exists and matches the WSI release loaded into
   cBioPortal.
5. this service can open the referenced slide files from object storage.
6. any enabled metadata/search endpoints have their required backend configured.

For private-study deployments, do not treat a missing resource index, missing
slide path, or authorization mismatch as “no slides”. Those are deployment
failures.

## Local file-backed test slides

For CI-safe or laptop-local tile tests, the server can bypass Databricks and
object storage for specific slide IDs by using `WSI_TEST_SLIDE_MAP_FILE`.

Example:

```json
{
  "openslide-small": "/app/testdata/CMU-1-Small-Region.svs",
  "mussel-small": "/app/testdata/948176.svs"
}
```

The repository includes:

- `tests/testdata/local-slide-map.json`
- `tools/download_public_test_slides.py`

Prepare the assets with:

```bash
python3 tools/download_public_test_slides.py
WSI_TEST_SLIDE_MAP_FILE=/app/testdata/local-slide-map.json docker compose up --build
```

When a requested slide ID exists in the map, the tile server opens the local file
directly. Other slide IDs continue to resolve through the normal configured
backends.

## Notes for non-MSK deployments

This repository contains some MSK-specific assumptions in naming, examples, and
local tooling. In particular, examples may reference Dell ECS, Databricks, and
MSK deployment conventions.

For a portable deployment, document and own these institution-specific pieces:

1. where slide inventory and patient/sample/slide associations originate
2. how the trusted resource index is generated and published
3. how slide object storage is addressed and authenticated
4. which metadata/search backend, if any, replaces the current Databricks flows
5. how unreadable or unsafe slides are detected before publication

## Running tests

```bash
uv run pytest
uv export --all-groups --no-emit-project --format requirements-txt \
  | uv run pip-audit -r /dev/stdin --strict
```
