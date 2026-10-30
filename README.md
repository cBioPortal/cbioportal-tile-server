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
is ignored, including `tw` and `th`. A stored JPEG thumbnail that already
fits in `width`×`height` is passed through unchanged; one in another format
is re-encoded to JPEG, and a larger one is downsized (never upscaled). The
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
| `502` | Service | Stored thumbnail unreadable, over `THUMBNAIL_MAX_DECODE_PIXELS`, or unavailable |
| `503` | Service | Busy, source temporarily unavailable, or seal key not configured (`Retry-After`) |

The serving contract version is `wsi-serving-v6` (reported by `/ready` as
`serving_contract_version` when a release is configured).

This service performs no user authentication: deploy it only behind an ingress
that authorizes `studyId` against cBioPortal with the session cookie, on the
portal's own origin, and do not expose it directly (see
[Ingress](#ingress)). `/health` and `/ready` intentionally remain public for
orchestration probes.

## Ingress

The ingress in front of `/wsi` does four things:

1. **Authorizes every pixel request** with an auth subrequest to the portal's
   `GET /api/studies/{studyId}`, forwarding the browser's `Cookie` (and
   `Authorization`, if the portal accepts tokens) header, and forwards the
   request only on `2xx`.
2. **Caches that decision, if at all, per credential and study.** The cache
   key must include the session cookie (and every other credential the portal
   accepts) and the study. An auth cache keyed without the session cookie
   lets one user's approval authorize another user's request for the same
   study for as long as the entry lives.
3. **Sets `X-Forwarded-For` to the client address** it sees, replacing any
   client-supplied value. The cache-miss rate limit uses the last
   `X-Forwarded-For` entry as the client. If any hop appends instead of
   setting (an ingress that appends its peer behind a load balancer, or a
   proxy between the ingress and the service), the last entry is that hop's
   own address for every request, and all clients share one rate-limit
   bucket.
4. **Logs `/wsi` requests without query strings.** `enc` is a bearer value
   for its slide.

`/health` and `/ready` are routed without auth. `/metrics` is not routed.

The study id has to come from the original request line. Inside an nginx
auth subrequest `$arg_studyId` is empty, because the subrequest has its own
(empty) query string, while `$request_uri` is still the original request's.
Both examples therefore extract the study with a `map` on `$request_uri`. The
map refuses an id that is only dots, and maps a missing or malformed study to
`!`, which is not a study id, so the portal refuses it rather than receiving
`/api/studies/`:

```nginx
map $request_uri $wsi_study {
    "~[?&]studyId=(?<study>(?!\.+(&|$))[A-Za-z0-9_.-]{1,128})(&|$)" $study;
    default "!";
}
```

### ingress-nginx

Put the `map` in the controller ConfigMap as `http-snippet`, together with
the forwarding settings:

```yaml
# ingress-nginx controller ConfigMap
data:
  http-snippet: |
    map $request_uri $wsi_study {
        "~[?&]studyId=(?<study>(?!\.+(&|$))[A-Za-z0-9_.-]{1,128})(&|$)" $study;
        default "!";
    }
  # X-Forwarded-For is set to $remote_addr, never appended to.
  compute-full-forwarded-for: "false"
  # "true" only when a trusted L7 load balancer sits in front of the
  # controller; then list its addresses in proxy-real-ip-cidr so
  # $remote_addr is the client it reports. Otherwise leave it "false".
  use-forwarded-headers: "false"
```

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: wsi-pixels
  annotations:
    nginx.ingress.kubernetes.io/auth-url: "http://cbioportal.<namespace>.svc.cluster.local:8080/api/studies/$wsi_study"
    nginx.ingress.kubernetes.io/auth-method: GET
    # Session cookie, bearer token, and study: one entry per credential and study.
    nginx.ingress.kubernetes.io/auth-cache-key: "$cookie_SESSION$http_authorization$wsi_study"
    nginx.ingress.kubernetes.io/auth-cache-duration: "200 30s"
    # Request lines carry enc; do not write them to the access log.
    nginx.ingress.kubernetes.io/enable-access-log: "false"
spec:
  ingressClassName: nginx
  rules:
    - host: cbioportal.example.org
      http:
        paths:
          - path: /wsi/tiles/
            pathType: Prefix
            backend: {service: {name: cbioportal-tile-server, port: {number: 8080}}}
          - path: /wsi/thumbnails
            pathType: Exact
            backend: {service: {name: cbioportal-tile-server, port: {number: 8080}}}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: wsi-probes
spec:
  ingressClassName: nginx
  rules:
    - host: cbioportal.example.org
      http:
        paths:
          - path: /wsi/health
            pathType: Exact
            backend: {service: {name: cbioportal-tile-server, port: {number: 8080}}}
          - path: /wsi/ready
            pathType: Exact
            backend: {service: {name: cbioportal-tile-server, port: {number: 8080}}}
```

Notes:

- ingress-nginx's auth location sets `proxy_pass_request_headers on`, so the
  original request headers, including `Cookie` and `Authorization`, reach
  `auth-url` without further configuration. It also sends
  `X-Original-URL` with the full original URL, query string included; the
  portal must not log that header.
- Use the portal's session cookie name in `auth-cache-key` (`SESSION` with
  Spring Session). Keep `auth-cache-duration` short: a revoked session or
  permission stays authorized until its entry expires. Omit both annotations
  to authorize every request without caching.
- `auth_request` understands only `2xx`, `401`, and `403`. A study the portal
  answers `404` for (unknown, or `!`) becomes a `500` from the ingress, which
  is still a refusal.
- `enable-access-log: "false"` drops access logs for these paths. To keep a
  path-only log instead, define a log format in `http-snippet` (see the
  `wsi_no_query` format below) and add a `configuration-snippet` with
  `access_log /var/log/nginx/access.log wsi_no_query;`; that needs snippet
  annotations enabled on the controller. nginx error-log lines include the
  request line, so keep error logs at the default level and treat them as
  sensitive.
- The service serves the same routes under `/wsi`, so no rewrite is needed.

### Plain nginx

```nginx
map $request_uri $wsi_study {
    "~[?&]studyId=(?<study>(?!\.+(&|$))[A-Za-z0-9_.-]{1,128})(&|$)" $study;
    default "!";
}

# Log the path only: the query string carries enc.
map $request_uri $wsi_log_path {
    "~^(?<path>[^?]*)" $path;
}
log_format wsi_no_query '$remote_addr - $remote_user [$time_local] "$request_method $wsi_log_path" '
                        '$status $body_bytes_sent "$http_user_agent"';

upstream portal { server portal:8080; }
upstream tile_server { server tile-server:8080; }

server {
    listen 80;

    # Probes stay open.
    location ~ ^/wsi/(health|ready)$ {
        proxy_pass http://tile_server;
    }

    # Tiles and thumbnails: forwarded only when the portal says the session
    # may read the study the request names.
    location ~ ^/wsi/(tiles|thumbnails)(/.*)?$ {
        if ($wsi_study = "!") {
            return 400;
        }
        access_log /var/log/nginx/access.log wsi_no_query;
        auth_request /_wsi_study_access;
        proxy_pass http://tile_server;
        proxy_set_header Host $http_host;
        # The rate-limit subject; replaces anything the client sent.
        proxy_set_header X-Forwarded-For $remote_addr;
        # The tile server needs no portal session.
        proxy_set_header Cookie "";
        proxy_read_timeout 120s;
    }

    location = /_wsi_study_access {
        internal;
        proxy_pass http://portal/api/studies/$wsi_study;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        # Explicit, although subrequests forward the original headers.
        proxy_set_header Cookie $http_cookie;
        proxy_set_header Host $http_host;
        proxy_set_header Accept application/json;
        # auth_request only understands 2xx/401/403; an unknown study is a 403.
        proxy_intercept_errors on;
        error_page 404 = @wsi_study_unknown;
    }

    location @wsi_study_unknown {
        return 403;
    }

    location / {
        proxy_pass http://portal;
        proxy_set_header Host $http_host;
    }
}
```

This example caches nothing. To cache, add `proxy_cache` to the auth
location with `proxy_cache_key "$cookie_SESSION$http_authorization$wsi_study";`
and a short `proxy_cache_valid 200 30s;`, for the reason above.

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
| `THUMBNAIL_MAX_DECODE_PIXELS` | `16777216` | Maximum source-level decode when rendering a thumbnail offline, and maximum stored-thumbnail size the service decodes (a larger one returns `502`) |
| `MAX_OPEN_SLIDES` | `64` | Open-slide LRU capacity |
| `MAX_IMAGE_OPERATIONS` | `2` | Concurrent pixel operations per worker |
| `N_WORKERS` | `4` | Gunicorn worker count |
| `CACHE_MISS_RATE_LIMIT_PER_MINUTE` | `120` | Shared Redis limit for cache-miss leaders per client address and slide `key`; `0` disables it. The client address is the last `X-Forwarded-For` entry (else the peer), so the ingress must set that header to the client address (see [Ingress](#ingress)) |
| `CACHE_MISS_LOCK_TTL_SECONDS` | `120` | Renewable cross-worker extraction lock lease |
| `CACHE_MISS_WAIT_TIMEOUT_SECONDS` | `60` | Maximum follower wait for another worker's extraction |
| `GUNICORN_TIMEOUT` | `180` | Worker request timeout for long cold-slide reads |
| `BLOCKCACHE_PATH` | — | Optional local range-read cache, namespaced by each source's ETag so a slide rewritten in place never reuses old blocks |
| `CORS_ORIGINS` | internal cBioPortal origins | Origins allowed uncredentialed cross-origin reads; pixel requests are same-origin, so this is not an access path |

Thumbnail artifacts are generated offline (see
[Offline preparation](#offline-preparation)); their dimensions, content
type, tile metadata, and sealed source (which carries the URLs) travel in each
slide row's `metadata.serving` object in the study's resource data. A slide
row without a `serving` object is not viewable. The online service does not
generate thumbnails or consult a manifest. Production deployments must run an
offline thumbnail batch; the service never renders a thumbnail from the source
image.

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
class. The ingress must not log query strings for `/wsi` (see
[Ingress](#ingress)): `enc` is a bearer value for its slide, and `key`
travels there too. Keep the backend resource data behind normal cBioPortal
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
