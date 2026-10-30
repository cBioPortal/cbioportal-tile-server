# cbioportal-tile-server

A tile server that lets cBioPortal display large images, such as digitized
pathology slides, in a pan-and-zoom viewer. It reads images from S3-compatible
object storage and serves deep-zoom JPEG tiles (`z/x/y`) and thumbnails to the
cBioPortal frontend.

At MSK, cBioPortal uses this service to show H&E and IHC whole-slide images
alongside the patients and samples they were taken from.

The service only handles pixels. It does not know about patients, samples,
studies, slide hierarchy, or image IDs; cBioPortal owns that metadata, and
the ingress in front of this service authorizes every request against
cBioPortal (see [Request flow](#request-flow)).

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

1. cBioPortal authenticates the user and serves the study's resource data.
   Each servable slide row carries an opaque `key` (its slide key) and its
   `sealed_source`; neither the database nor any response carries an
   `imageId` or source URL.
2. The browser requests pixels by naming the slide:
   `?studyId=<study>&key=<slide key>&enc=<sealed source>`. No `Authorization`
   header is sent.
3. The ingress authorizes each request before it reaches this service with an
   auth subrequest to cBioPortal's `GET /api/studies/{studyId}`, carrying the
   browser's session cookie. Only requests for a study the user may read are
   forwarded; a `401` or `403` always comes from the ingress, never from this
   service.
4. The service opens `enc` with `WSI_SOURCE_SEAL_KEY` (bound to `key`),
   checks the URLs against the artifact policy, and reads pixels from them in
   object storage.

The tile server never resolves an image ID, queries cBioPortal metadata,
loads portal data files, or authenticates users. Client-supplied source URLs
(`?source=` or `X-WSI-Source`) are rejected.

The service must be served same-origin with cBioPortal (for example under
`/wsi` on the portal host): the ingress check relies on the browser sending
the portal session cookie with each pixel request, and the service sets no
credentialed CORS (`CORS_ORIGINS` never allows credentials).

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
3. Each servable slide's row in the study's resource data files carries a
   `metadata.serving` object with `key` (the slide key), `sealed_source`,
   `tile_metadata_json`, `thumbnail_width`, `thumbnail_height`, and
   `thumbnail_content_type`, and never the `image_id` or URLs.
   `tile_metadata_json` must pass
   `app.metadata_contract.validate_tile_metadata`.
4. cBioPortal core imports those files and is the sole ClickHouse writer. The
   frontend passes `sealed_source` verbatim as the `enc` query parameter.

The tile server never reads the pipeline's tables, registries, manifests, or
credentials; it sees only what each request's `key` and `enc` carry.
A slide becomes servable after thumbnails are published, study files are
exported, and cBioPortal core has imported them.

### Modules shared with offline tooling

Preparation pipelines may import `app.tiles`, `app.slide_store`,
`app.identity`, `app.metadata_contract`, and `app.sealed_source` so their
thumbnails, tile metadata, and sealed-source shape match what this service
renders and opens. De-identification of study files is the data provider's
responsibility; this service does not validate study rows. Treat changes to those modules as contract changes: bump
`IDENTITY_VERSION` or `TILE_METADATA_SCHEMA_VERSION` in `app/identity.py` when
the rendered output or metadata shape changes, and call the change out in the
pull request.

The frontend is read-only: it reads the slide rows from cBioPortal and then
requests `/thumbnails` and tiles; it has no object-store upload credentials.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Public liveness probe |
| GET | `/ready` | Public readiness probe (seal key and artifact-policy configuration) |
| GET | `/metrics` | Internal Prometheus/OpenMetrics scrape endpoint |
| GET | `/tiles/zxy/{z}/{x}/{y}?studyId=&key=&enc=` | JPEG tile of the sealed source |
| GET | `/thumbnails?studyId=&key=&enc=&width=&height=` | Thumbnail resize of the sealed thumbnail |

The same routes are available under `/wsi`. Every pixel request names its
slide with query parameters, each given at most once. Names are matched
case-insensitively, as nginx `$arg_` lookups are, so `StudyID=a&studyId=b` is a
repeat:

| Parameter | Rule |
|-----------|------|
| `studyId` | Required; a cBioPortal study id (`^(?!\.+$)[A-Za-z0-9_.-]{1,128}$`, so never only dots). Not used by this service, but requiring it guarantees the ingress always has a study to authorize |
| `key` | Required; the resource row's serving key: 1-128 characters of `A-Z`, `a-z`, `0-9`, `_` and `-` (a whole-slide image's key is its 12-hex slide key) |
| `enc` | Required; the row's sealed source, verbatim |
| `width`, `height` | Thumbnails only; optional requested size, a positive integer clamped to 2048 (default 256) |

A missing, malformed, or repeated parameter returns `400`. Any other parameter
is ignored. A thumbnail is passed through when the stored thumbnail already
fits in `width`×`height` and downsized otherwise (never upscaled); the
stored thumbnail's own size decides, never the request. `enc` is base64url (no
padding) of nonce (12 bytes) || AES-256-GCM ciphertext || tag (16 bytes),
keyed with the raw 32-byte `WSI_SOURCE_SEAL_KEY`, AAD = `key`, over JSON
`{"image_id":…,"tile_source":…,"thumbnail_source":…}`. The data provider seals
it when it publishes the slide; cBioPortal stores and serves it verbatim and
cannot open it, so neither cBioPortal nor the browser sees the `image_id` or
source URLs. An `enc` that does not open under `key` (for example another
slide's) returns `404` (`slide not found`). The tile server then applies the
source/thumbnail URI policy (scheme, approved prefix, extension, no traversal)
before any read; a source outside it returns `404` (`source slide not found`)
and is logged without its URL. Requests that supply a source themselves
(`?source=` or `X-WSI-Source`) are rejected with `400`.

| Status | Returned by | Meaning |
|--------|-------------|---------|
| `401`, `403` | Ingress only | No session, or the user cannot read `studyId` |
| `400` | Service | Missing, malformed, or repeated parameter; client-supplied source |
| `404` | Service | `enc` does not open under `key`; sealed source outside the URI policy; source or thumbnail object missing; tile outside the slide |
| `422` | Service | Overview tile needs preprocessing (`overview_requires_preprocessing`) |
| `429` | Service | Cache-miss rate limit (`Retry-After`) |
| `500` | Service | Unexpected slide read failure |
| `502` | Service | Stored thumbnail unreadable or unavailable |
| `503` | Service | Busy, source temporarily unavailable, or seal key not configured (`Retry-After`) |

See `../docs/wsi-deid-slide-key-contract.md` (`wsi-serving-v6`).

This service performs no user authentication: deploy it only behind an ingress
that authorizes `studyId` against cBioPortal with the session cookie, on the
portal's own origin, and do not expose it directly. `/health` and `/ready`
intentionally remain public for orchestration probes.

## Runtime configuration

The service needs only pixel storage, the source seal key, and an optional
Redis cache:

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ENDPOINT_URL` | — | S3-compatible object-store endpoint |
| `AWS_ACCESS_KEY_ID` | — | Object-store access key |
| `AWS_SECRET_ACCESS_KEY` | — | Object-store secret key |
| `WSI_SOURCE_SEAL_KEY` | — | Required. Standard base64 of exactly 32 random bytes; opens each request's `enc`. Shared with the data provider that seals sources, never with cBioPortal. Startup fails without it |
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
| `CACHE_MISS_RATE_LIMIT_PER_MINUTE` | `120` | Shared Redis limit for cache-miss leaders per client address (the last `X-Forwarded-For` entry, which the ingress sets, else the peer) and slide `key`; `0` disables it |
| `CACHE_MISS_LOCK_TTL_SECONDS` | `120` | Renewable cross-worker extraction lock lease |
| `CACHE_MISS_WAIT_TIMEOUT_SECONDS` | `60` | Maximum follower wait for another worker's extraction |
| `GUNICORN_TIMEOUT` | `180` | Worker request timeout for long cold-slide reads |
| `BLOCKCACHE_PATH` | — | Optional local range-read cache, namespaced by each source's ETag so a slide rewritten in place never reuses old blocks |
| `CORS_ORIGINS` | internal cBioPortal origins | Origins allowed uncredentialed cross-origin reads; pixel requests are same-origin, so this is not an access path |

Thumbnail artifacts are generated offline (see
[Offline preparation](#offline-preparation)); their dimensions, content
type, tile metadata, and sealed source (which carries the URL) are loaded into
the cBioPortal WSI slide table. A slide is
published with `can_serve_tiles=false` until all of those fields are present.
The online service does not generate thumbnails, consult a manifest, or write
the registry. Production deployments must run an offline thumbnail batch; the
on-demand worker is not a substitute.

Tile responses are `private, max-age=3600` and thumbnails
`private, max-age=300`; both vary on `Cookie`, the credential the ingress
authorizes on. Redis is an optimization only; a cache outage does not change
authorization.
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
pixel endpoint. Production ingress should expose only the tile,
thumbnail, health, and readiness paths; scrape `/metrics` through the internal
Kubernetes service. Important metrics include image-operation queue time,
thumbnail fetch/resize latency and fetch-slot queue time, slide-open latency,
Redis errors/latency, cache hit/miss counts, distributed
miss-lock outcomes, and cache-miss rate-limit decisions.

## Quick start

```bash
python3 tools/write_dev_env.py
printf 'WSI_SOURCE_SEAL_KEY=%s\nREDIS_PASSWORD=%s\n' "$(openssl rand -base64 32)" "$(openssl rand -hex 24)" >> .env
docker compose up --build
```

The compose file is a local rehearsal with no ingress in front of the
service, so it performs no study authorization; do not expose it. Use the
same `WSI_SOURCE_SEAL_KEY` here and wherever the study's sealed sources were
produced.

## Local slide tests

For CI-safe local slide tests, set `WSI_ALLOWED_SOURCE_SCHEMES=s3,file`,
include `file:///app/testdata/` in both prefix allowlists, and request with
an `enc` that seals the mounted file URIs with `WSI_SOURCE_SEAL_KEY` under the
request's `key` (see `seal_claims` in `tests/test_auth.py`). The normal
production default accepts only `s3` URLs.

## PHI-safe operation

Do not log source URLs, patient IDs, slide IDs, or `enc` values. Request
logs may retain operation type, dimensions, status, timing, and exception
class. The ingress must not log query strings for `/wsi`: `enc` is a bearer
value for its slide, and `key` travels there too. Keep the backend resource data behind normal cBioPortal
authentication and study authorization.

## Tests

```bash
uv run pytest
uv export --all-groups --no-emit-project --format requirements-txt \
  | uv run pip-audit -r /dev/stdin --strict
```

## Container publication

The `Build and publish container` workflow builds every pull request and runs
the same health, readiness, request-validation, CORS, and Gunicorn-startup smoke
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
