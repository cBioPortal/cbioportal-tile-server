# cbioportal-tile-server

The cBioPortal WSI pixel service. This process serves only JPEG tiles and
pre-rendered thumbnail artifacts; it does not know about patients, samples,
studies, slide hierarchy, or image IDs.

## Request flow

1. cBioPortal authenticates the user and checks study permissions.
2. `GET /api/wsi/v2/slides/{studyId}/{imageId}/access` reads the materialized
   slide row from the cBioPortal database and returns the exact tile source URL,
   thumbnail artifact URL, tile metadata, and a short-lived Bearer capability.
3. The browser sends that URL and capability to this service.
4. The service verifies the capability's SHA-256 binding to the exact URL and
   reads pixels from object storage.

The tile server never resolves an image ID, queries cBioPortal metadata, or
loads portal data files. A URL without a matching capability is
rejected, even when the URL is otherwise reachable.

## Production WSI artifact dataflow

Thumbnail generation, registry publication, and study-file export are offline
prerequisites to the API, owned by
[`pdm_databricks_pipelines`](https://github.com/pathology-data-mining/pdm_databricks_pipelines/tree/main/pathology_data_mining):
the [`wsi_tools`](https://github.com/pathology-data-mining/pdm_databricks_pipelines/tree/main/pathology_data_mining/wsi_tools) project renders and publishes thumbnails and
exports the cBioPortal WSI and timeline study files, and the
[`wsi_summary`](https://github.com/pathology-data-mining/pdm_databricks_pipelines/tree/main/pathology_data_mining/wsi_summary) bundle builds the serving manifest,
canonical associations, and summary tables. cBioPortal core imports the study
files and is the sole ClickHouse writer. The tile server holds no Databricks
code or credentials; `wsi_tools` imports this package's slide reader,
renderer, tile metadata, and de-identification checks (pinned by commit) so
published artifacts match what this service serves.

The frontend is read-only: it requests the backend access bundle and then
requests `/thumbnails`; it has no ECS/S3 upload credentials. `app/thumbnail_worker.py`
is a controlled on-demand CLI that can write a generated JPEG to the
configured S3/ECS-compatible location, but it does not update the thumbnail
registry and is not a production publication mechanism. Keep it limited to
development, rehearsal, or explicit remediation.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Public liveness probe |
| GET | `/ready` | Public readiness probe (auth and artifact-policy configuration) |
| GET | `/metrics` | Internal Prometheus/OpenMetrics scrape endpoint |
| GET | `/tiles/zxy/{z}/{x}/{y}?source=...` | Source-bound JPEG tile |
| GET | `/thumbnails?source=...&width=...&height=...` | Source-bound thumbnail resize |

The same routes are available under `/wsi`. Every pixel request requires:

```text
Authorization: Bearer <cBioPortal slide capability>
```

Capabilities are HMAC-SHA256 JWTs with `scope=wsi:read`,
`wsi_auth_version=2`, `study_id`, `image_id`, exact source URL digests, bounded
thumbnail dimensions, and an expiry no longer than `WSI_AUTH_MAX_TTL`.
`/health` and `/ready` intentionally remain public for orchestration probes.

## Runtime configuration

The service needs only pixel storage, the shared capability secret, and an
optional Redis cache:

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ENDPOINT_URL` | — | S3-compatible object-store endpoint |
| `AWS_ACCESS_KEY_ID` | — | Object-store access key |
| `AWS_SECRET_ACCESS_KEY` | — | Object-store secret key |
| `WSI_AUTH_SECRET` | — | At least 32 bytes; shared with cBioPortal |
| `WSI_AUTH_PREVIOUS_SECRET` | — | Optional prior secret during a bounded signing-key rotation |
| `WSI_AUTH_AUDIENCE` | `cbioportal-wsi` | Capability audience |
| `WSI_AUTH_MAX_TTL` | `300` | Maximum capability lifetime in seconds |
| `WSI_ALLOWED_SOURCE_SCHEMES` | `s3` | Comma-separated schemes accepted in source URLs |
| `WSI_ALLOWED_SOURCE_PREFIXES` | empty | Comma-separated approved source URI prefixes; required for publication |
| `WSI_ALLOWED_THUMBNAIL_PREFIXES` | empty | Comma-separated approved thumbnail URI prefixes; required for publication |
| `REDIS_URL` | `redis://redis:6379` | Optional tile/thumbnail cache |
| `THUMBNAIL_FETCH_CONCURRENCY` | `8` | Per-worker concurrent thumbnail object fetches |
| `THUMBNAIL_FETCH_MAX_ATTEMPTS` | `2` | Total object-store read attempts per thumbnail request |
| `THUMBNAIL_FETCH_RETRY_DELAY_SEC` | `0.1` | Delay before the bounded thumbnail read retry |
| `THUMBNAIL_S3_MAX_CONNECTIONS` | `32` | Per-worker pooled S3 connections |
| `THUMBNAIL_S3_CONNECT_TIMEOUT_SEC` | `1` | S3 connection timeout |
| `THUMBNAIL_S3_READ_TIMEOUT_SEC` | `5` | S3 object-read timeout |
| `THUMBNAIL_S3_MAX_ATTEMPTS` | `2` | S3 client retry limit |
| `THUMBNAIL_PREWARM_URI` | — | Optional stable thumbnail object used to prewarm each worker |
| `THUMBNAIL_PREWARM_REQUIRED` | `false` | Refuse startup when the prewarm object cannot be read |
| `TILE_SIZE` | `256` | Tile edge length |
| `JPEG_QUALITY` | `85` | JPEG encoding quality |
| `MAX_DECODE_PIXELS` | `16777216` | Maximum on-demand tile decode |
| `THUMBNAIL_MAX_DECODE_PIXELS` | `16777216` | Maximum thumbnail decode |
| `MAX_OPEN_SLIDES` | `64` | Open-slide LRU capacity |
| `MAX_IMAGE_OPERATIONS` | `2` | Concurrent pixel operations per worker |
| `N_WORKERS` | `4` | Gunicorn worker count |
| `CACHE_MISS_RATE_LIMIT_PER_MINUTE` | `120` | Shared Redis limit for cache-miss leaders per capability subject and source; `0` disables it |
| `CACHE_MISS_LOCK_TTL_SECONDS` | `120` | Renewable cross-worker extraction lock lease |
| `CACHE_MISS_WAIT_TIMEOUT_SECONDS` | `60` | Maximum follower wait for another worker's extraction |
| `GUNICORN_TIMEOUT` | `180` | Worker request timeout for long cold-slide reads |
| `BLOCKCACHE_PATH` | — | Optional local range-read cache |
| `CORS_ORIGINS` | internal cBioPortal origins | Allowed browser origins |

Thumbnail artifacts are generated offline by the `pdm_databricks_pipelines`
`wsi_tools` thumbnail batch and their URL, dimensions, content type,
and tile metadata are loaded into the cBioPortal WSI slide table. A slide is
published with `can_serve_tiles=false` until all of those fields are present.
The online service does not generate thumbnails, consult a manifest, or write
the registry. Production deployments must schedule the offline batch described
above; an on-demand worker is not a substitute for registry publication.

Tile and thumbnail responses are private-cacheable and vary on `Authorization`.
Redis is an optimization only; a cache outage does not change authorization.
If a requested overview cannot be decoded within the configured pixel bound,
the tile endpoint returns HTTP 422 with
`{"error":"overview_requires_preprocessing"}`.

Cache misses are coordinated in two layers. The in-process single-flight
coalesces requests within a Gunicorn worker, while Redis locks coalesce the
same key across workers and replicas. Only the extraction owner consumes the
cache-miss rate limit; cache hits are not application-rate-limited. If Redis
is unavailable, the service falls back to local single-flight and the image
operation semaphore, and records the outage in metrics.

The `/metrics` endpoint is intended for internal monitoring and is not a WSI
capability endpoint. Production ingress should expose only the tile,
thumbnail, health, and readiness paths; scrape `/metrics` through the internal
Kubernetes service. Important metrics include image-operation queue time,
thumbnail fetch/resize latency and fetch-slot queue time, slide-open latency,
Redis errors/latency, cache hit/miss counts, distributed
miss-lock outcomes, and cache-miss rate-limit decisions.

## Quick start

```bash
python3 tools/write_dev_env.py
printf 'WSI_AUTH_SECRET=%s\nREDIS_PASSWORD=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 24)" >> .env
docker compose up --build
```

The compose file is a local rehearsal. Configure the same secret, audience,
and compatible TTL in cBioPortal and this service.

## Offline preparation

The offline tools, their runbooks, and the dev snapshot workflow are
documented in the [`wsi_tools` README](https://github.com/pathology-data-mining/pdm_databricks_pipelines/tree/main/pathology_data_mining/wsi_tools). Changes here to
`app/tiles.py`, `app/slide_store.py`, `app/identity.py`,
`app/metadata_contract.py`, or `app/deid.py` change what those tools publish;
bump the `cbioportal-tile-server` pin in `wsi_tools/pyproject.toml` when they
do.

For CI-safe local slide tests, set `WSI_ALLOWED_SOURCE_SCHEMES=s3,file` and
issue a v2 capability whose source URL is the mounted file URI. The normal
production default accepts only `s3` URLs.

## PHI-safe operation

Do not log source URLs, patient IDs, slide IDs, or token contents. Request
logs may retain operation type, dimensions, status, timing, and exception
class. Keep the backend hierarchy and access endpoint behind normal
cBioPortal authentication and study authorization.

## Tests

```bash
uv run pytest
uv export --all-groups --no-emit-project --format requirements-txt \
  | uv run pip-audit -r /dev/stdin --strict
```

## Container publication

The `Build and publish container` workflow builds every pull request and runs
the same health, readiness, authentication, CORS, and Gunicorn-startup smoke
checks used for the triage canary.  A push to `main` publishes only an
immutable full-commit tag to Docker Hub:

```text
cbioportal/cbioportal-tile-server:<commit-sha>
```

The repository must provide the `DOCKER_HUB_USERNAME` and
`DOCKER_HUB_TOKEN` Actions secrets.  Kubernetes deployments should use the
digest printed by the workflow (`@sha256:...`), not a mutable tag.  The
workflow does not publish from pull requests or expose registry credentials to
fork builds.
