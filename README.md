# cbioportal-tile-server

FastAPI tile server that streams SVS whole-slide images from Dell ECS (S3-compatible) to
OpenSeadragon via ZXY tile requests.  Used as the backend for the cBioPortal H&E slide viewer.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/slides/{image_id}/dbmeta` | Raw Databricks row for a slide |
| GET | `/search?q=` | Autocomplete suggestions |
| GET | `/tiles/{slide_id}/metadata` | Slide dimensions, zoom levels, MPP |
| GET | `/tiles/{slide_id}/thumbnail` | JPEG thumbnail |
| GET | `/tiles/{slide_id}/zxy/{z}/{x}/{y}` | ZXY tile (JPEG) |

The same endpoints are also available under the explicit `/wsi` namespace,
for example `/wsi/tiles/{slide_id}/...`.

All endpoints except `/health` require a cBioPortal-issued short-lived WSI
capability in the header:

```text
Authorization: Bearer <token>
```

The token must be an HMAC-SHA256 JWT with the configured audience,
`scope=wsi:read`, a non-empty subject, `study_id`, `wsi_auth_version=1`, and
valid `iat`/`exp` claims whose lifetime does not exceed `WSI_AUTH_MAX_TTL`.
The `/wsi/health` alias is also unauthenticated for probes. Do not disable
this check in production.

## Quick start

```bash
python3 tools/write_dev_env.py          # securely populate .env from local credentials
printf 'WSI_AUTH_SECRET=%s\nREDIS_PASSWORD=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 24)" >> .env
docker compose up --build
```

## Configuration

All settings are environment variables (see `app/config.py`):

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ENDPOINT_URL` | — | Dell ECS endpoint |
| `AWS_ACCESS_KEY_ID` | — | ECS access key |
| `AWS_SECRET_ACCESS_KEY` | — | ECS secret key |
| `DATABRICKS_HOST` | — | Databricks workspace URL |
| `DATABRICKS_TOKEN` | — | Databricks PAT |
| `DATABRICKS_WAREHOUSE_ID` | `0b49b7d78734ad5c` | SQL warehouse |
| `WSI_AUTH_SECRET` | — | At least 32 bytes; shared with the cBioPortal capability issuer |
| `WSI_AUTH_AUDIENCE` | `cbioportal-wsi` | Capability-token audience |
| `WSI_AUTH_REQUIRED` | `true` | Require Bearer capabilities for non-health routes |
| `WSI_AUTH_MAX_TTL` | `300` | Maximum WSI token lifetime in seconds |
| `WSI_RESOURCE_INDEX_FILE` | — | Loader-published version-1 study/resource binding; required when auth is enabled |
| `TILE_SIZE` | `256` | Tile edge length in pixels |
| `JPEG_QUALITY` | `85` | JPEG encoding quality |
| `MAX_DECODE_PIXELS` | `4194304` | Maximum source pixels a single on-demand decode may read before the request is rejected |
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

If a slide lacks a sufficiently downsampled overview pyramid, thumbnail and
overview-tile requests now return HTTP `422` with
`{"error":"overview_requires_preprocessing"}` instead of attempting a
memory-unsafe full-slide decode.

## Study isolation contract

Authenticated production mode (`WSI_AUTH_REQUIRED=true`) authorizes every
protected resource against
the `WSI_RESOURCE_INDEX_FILE` produced from the materialized hierarchy
snapshot. The token's `study_id` is authoritative. A `studyId` query parameter
may be supplied by the frontend only as a consistency check and is never
trusted on its own.

The mapping is required for slide metadata, thumbnails, tiles, warmup, raw
`/slides/{id}/dbmeta`, and `/search`. A token
for study A must return `403` for a patient or slide bound only to study B;
missing or invalid capabilities return `401`. A missing or invalid trusted
index fails closed with `503`. Search results are filtered to the token study.
Resource identifiers must be unambiguous across studies in the published
index; ambiguous patient, sample, or slide identifiers fail closed rather than
being served through an ID-only metadata query.

The cBioPortal backend and this service must use the same secret bytes,
audience (`cbioportal-wsi`), and compatible TTL (`300` seconds from the
backend, no more than `WSI_AUTH_MAX_TTL`). Protected responses use private
cache headers; Redis and an HTTP cache are never authorization boundaries.

Unauthenticated local/development mode (`WSI_AUTH_REQUIRED=false`) is retained
for public fixtures only. It must not be used for private-study deployment.

## Local file-backed test slides

For CI-safe or laptop-local tile tests, the server can bypass Databricks/ECS for
specific slide IDs by using `WSI_TEST_SLIDE_MAP_FILE`.

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
directly. Other slide IDs continue to resolve through Databricks and ECS.

## Running tests

```bash
uv run pytest
uv export --all-groups --no-emit-project --format requirements-txt \
  | uv run pip-audit -r /dev/stdin --strict
```
