# cbioportal-tile-server

A tile server that lets cBioPortal display large images, such as digitized
pathology slides, in a pan-and-zoom viewer. It reads images from S3-compatible
object storage and serves deep-zoom JPEG tiles (`z/x/y`) and thumbnails to the
cBioPortal frontend.

At MSK, cBioPortal uses this service to show H&E and IHC whole-slide images
alongside the patients and samples they were taken from.

The service only handles pixels. It does not know about patients, samples,
studies, slide hierarchy, or image IDs; cBioPortal owns that metadata and
authorizes every request (see [Request flow](#request-flow)).

## What it serves

- **Image formats:** anything
  [`tiffslide`](https://github.com/Bayer-Group/tiffslide) can open. That
  includes whole-slide image formats such as Aperio SVS and Leica SCN, and
  generic tiled/pyramidal TIFF, so the server is not limited to pathology
  slides. Tiles are encoded as RGB JPEG, so RGB images are the best fit.
  Formats `tiffslide` cannot open (for example DICOM and MIRAX) are not
  supported.
- **Tiles are generated on the fly.** No tile pyramid is precomputed. A source
  image stores some resolution levels, but not necessarily one for every zoom
  level the viewer requests. For each tile request, the server reads the
  region from the coarsest source level that still meets the requested
  resolution, downscales it to the tile size, encodes it as JPEG, and caches
  it (in Redis, when configured) for subsequent requests.
- **The only preprocessing is a one-time offline step per image** that
  renders a thumbnail and records the image's tile metadata (dimensions and
  resolution levels). cBioPortal marks an image as servable only after both
  exist (see [Offline preparation](#offline-preparation)). The
  server fetches those thumbnails and resizes them on request; it never
  renders a thumbnail from the source image.
- **Limit:** if an image lacks low-resolution levels, the most zoomed-out
  tiles can exceed `MAX_DECODE_PIXELS`, and the server rejects them with HTTP
  422 rather than decoding the full-resolution image.

## Request flow

1. cBioPortal authenticates the user and checks study permissions.
2. `GET /api/wsi/v2/slides/{studyId}/{slideKey}/access` reads the materialized
   slide row from the cBioPortal database and returns tile metadata, thumbnail
   dimensions, and a short-lived Bearer capability. The exact tile source URL
   and thumbnail artifact URL are in the capability's `enc` claim, sealed by
   the data provider with a key cBioPortal does not hold; neither the database
   nor the response carries an `imageId` or source URL.
3. The browser sends only that capability to this service.
4. The service verifies the capability, opens `enc` with
   `WSI_SOURCE_SEAL_KEY`, checks the URLs against the artifact policy, and
   reads pixels from them in object storage.

The tile server never resolves an image ID, queries cBioPortal metadata, or
loads portal data files. Client-supplied source URLs (`?source=` or
`X-WSI-Source`) are rejected.

## Offline preparation

Thumbnail generation and study-file export happen offline, before a slide can
be served. Each deployment supplies its own preparation pipeline; this
repository contains only the online service. Whatever produces the data, the
tile server relies on this contract:

1. Thumbnail JPEGs are written to object storage under a prefix listed in
   `WSI_ALLOWED_THUMBNAIL_PREFIXES`; source slides live under
   `WSI_ALLOWED_SOURCE_PREFIXES`.
2. The pipeline seals each servable slide's `image_id`, tile source URL, and
   thumbnail URL into `sealed_source` (the `enc` format above, keyed with
   `WSI_SOURCE_SEAL_KEY`, AAD = `slide_key`).
3. Each slide row in the cBioPortal WSI study files (`meta_wsi.txt`/
   `data_wsi.txt`) carries `slide_key`, `sealed_source`,
   `tile_metadata_json`, `thumbnail_width`, `thumbnail_height`, and
   `thumbnail_content_type`, and never the `image_id` or URLs.
   `tile_metadata_json` must pass
   `app.metadata_contract.validate_tile_metadata`.
4. cBioPortal core imports those files and is the sole ClickHouse writer. It
   publishes `can_serve_tiles=true` only when every field is present, and
   forwards `sealed_source` as the capability's `enc` claim.

The tile server never reads the pipeline's tables, registries, manifests, or
credentials; it sees only what the access bundle and capability token carry.
A slide becomes servable after thumbnails are published, study files are
exported, and cBioPortal core has imported them.

### Modules shared with offline tooling

Preparation pipelines may import `app.tiles`, `app.slide_store`,
`app.identity`, `app.metadata_contract`, and `app.deid` so their thumbnails,
tile metadata, and de-identification checks match what this service renders
and enforces. Treat changes to those modules as contract changes: bump
`IDENTITY_VERSION` or `TILE_METADATA_SCHEMA_VERSION` in `app/identity.py` when
the rendered output or metadata shape changes, and call the change out in the
pull request.

The frontend is read-only: it requests the backend access bundle and then
requests `/thumbnails`; it has no object-store upload credentials.
`app/thumbnail_worker.py` is a controlled on-demand CLI that can write a
generated JPEG to the configured S3-compatible location, but it is not a
production publication mechanism. Keep it limited to development, rehearsal,
or explicit remediation.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Public liveness probe |
| GET | `/ready` | Public readiness probe (auth and artifact-policy configuration) |
| GET | `/metrics` | Internal Prometheus/OpenMetrics scrape endpoint |
| GET | `/tiles/zxy/{z}/{x}/{y}` | Capability-bound JPEG tile |
| GET | `/thumbnails?width=...&height=...` | Capability-bound thumbnail resize |

The same routes are available under `/wsi`. Every pixel request requires:

```text
Authorization: Bearer <cBioPortal slide capability>
```

Capabilities are HMAC-SHA256 JWTs (signed with `WSI_AUTH_SECRET`) with
`scope=wsi:read`, `wsi_auth_version=4`, `study_id`, an opaque `slide_key`,
bounded thumbnail dimensions, an expiry no longer than `WSI_AUTH_MAX_TTL`, and
an `enc` claim. `enc` is the slide's sealed source: base64url (no padding) of
nonce (12 bytes) || AES-256-GCM ciphertext || tag (16 bytes), keyed with the raw
32-byte `WSI_SOURCE_SEAL_KEY`, AAD = `slide_key`, over JSON
`{"image_id":…,"tile_source":…,"thumbnail_source":…}`. The data provider seals
it when it publishes the slide; cBioPortal stores and forwards it verbatim and
cannot open it, so neither cBioPortal nor the browser sees the `image_id` or
source URLs. The tile server decrypts it and applies the source/thumbnail URI
policy (scheme, approved prefix, extension, identifier checks) before any read.
Requests that supply a source themselves (`?source=` or `X-WSI-Source`) are
rejected with `400`. See `../docs/wsi-deid-slide-key-contract.md`
(`wsi-serving-v6`).
`/health` and `/ready` intentionally remain public for orchestration probes.

## Runtime configuration

The service needs only pixel storage, the shared capability secret, the source
seal key, and an optional Redis cache:

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ENDPOINT_URL` | — | S3-compatible object-store endpoint |
| `AWS_ACCESS_KEY_ID` | — | Object-store access key |
| `AWS_SECRET_ACCESS_KEY` | — | Object-store secret key |
| `WSI_AUTH_SECRET` | — | At least 32 bytes; shared with cBioPortal |
| `WSI_AUTH_PREVIOUS_SECRET` | — | Optional prior secret during a bounded signing-key rotation |
| `WSI_SOURCE_SEAL_KEY` | — | Required. Standard base64 of exactly 32 random bytes; opens the `enc` claim. Shared with the data provider that seals sources, never with cBioPortal. Startup fails without it |
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
| `ANNOTATION_DATABASE_URL` | — | Optional Postgres/Lakebase DSN for annotation storage |
| `ANNOTATION_DB_PATH` | `/data/annotations.db` | SQLite path used when `ANNOTATION_DATABASE_URL` is unset |
| `ANNOTATION_AUTH_ENABLED` | `true` | Require cBioPortal-issued annotation capabilities |

Thumbnail artifacts are generated offline (see
[Offline preparation](#offline-preparation)); their dimensions, content
type, tile metadata, and sealed source (which carries the URL) are loaded into
the cBioPortal WSI slide table. A slide is
published with `can_serve_tiles=false` until all of those fields are present.
The online service does not generate thumbnails, consult a manifest, or write
the registry. Production deployments must run an offline thumbnail batch; the
on-demand worker is not a substitute.

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
printf 'WSI_AUTH_SECRET=%s\nWSI_SOURCE_SEAL_KEY=%s\nREDIS_PASSWORD=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -base64 32)" "$(openssl rand -hex 24)" >> .env
docker compose up --build
```

The compose file is a local rehearsal. Configure the same secret, audience,
and compatible TTL in cBioPortal and this service, and the same
`WSI_SOURCE_SEAL_KEY` here and wherever the study's sealed sources were
produced.

## Local slide tests

For CI-safe local slide tests, set `WSI_ALLOWED_SOURCE_SCHEMES=s3,file`,
include `file:///app/testdata/` in both prefix allowlists, and issue a v4
capability whose `enc` seals the mounted file URIs with `WSI_SOURCE_SEAL_KEY`
(see `seal_claims` in `tests/test_auth.py`). The normal
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
