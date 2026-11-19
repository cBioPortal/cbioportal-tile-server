# cbioportal-tile-server

A tile server that lets cBioPortal display large images, such as digitized
pathology slides, in a pan-and-zoom viewer. It reads images from S3-compatible
object storage and serves deep-zoom JPEG tiles (`z/x/y`) and thumbnails to the
cBioPortal frontend.

At MSK, cBioPortal uses this service to show H&E and IHC whole-slide images
alongside the patients and samples they were taken from.

The pixel service only handles pixels. It does not know about patients,
samples, studies, slide hierarchy, or image IDs; cBioPortal owns that
metadata, and the ingress in front of this service authorizes every request
against cBioPortal (see [Request flow](#request-flow)). Slide annotations run
as a separate application from the same image (see
[Companion service](#companion-service)).

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
   forwarded; a `401` or `403` on a pixel request always comes from the
   ingress, never from this service.
4. The service opens `enc` with `WSI_SOURCE_SEAL_KEY` (bound to `key`),
   checks the URLs against the artifact policy, and reads pixels from them in
   object storage.

The tile server never resolves an image ID, queries cBioPortal metadata,
loads portal data files, or authenticates pixel requests. Client-supplied source URLs
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

The serving contract version is `wsi-serving-v6`, reported by `/ready` as
`serving_contract_version`.

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

## Companion service

The same image also runs a second, separate application, `app.companion`,
which serves slide annotations (with comment threads), slide notes, the
research assistant and research retrieval. The pixel application (`app.main`) serves none of its routes.

| Application | Command | Routes |
|-------------|---------|--------|
| Pixels | `gunicorn -c python:app.gunicorn_conf app.main:app` (the image's default `CMD`) | `/tiles`, `/thumbnails`, `/health`, `/ready`, `/metrics` |
| Companion | `gunicorn -c python:app.gunicorn_conf app.companion:app` | `/annotations`, `/slide-notes`, `/agent`, `/research/v1`, `/health`, `/ready` |

Both serve the same routes under `/wsi` as well. Run them as separate
deployments: override the container command for the companion.

### Identity from the proxy

The companion does not authenticate anyone itself. It trusts two request
headers that an authenticating reverse proxy sets:

1. For every companion request, the proxy makes an auth subrequest to the
   portal's `GET /api/studies/{studyId}/access`, carrying the browser's
   `Cookie` (and `Authorization`, if sent). `{studyId}` comes from the
   request's `studyId` query parameter. The portal answers:
   - `204` with `X-Auth-Request-User` (the signed-in user; absent when the
     portal runs without login) and `X-Auth-Request-Study`;
   - `401` when not signed in;
   - `403` when the user may not read the study, or the study is unknown.
2. On `2xx`, the proxy copies `X-Auth-Request-User` and
   `X-Auth-Request-Study` from the auth response onto the forwarded request,
   **replacing any copy the client sent**. With ingress-nginx
   `auth-response-headers` and nginx `auth_request_set` + `proxy_set_header`,
   a header the portal did not return is sent empty, which drops the
   client's copy too.

The companion then requires, on every route:

- `X-Auth-Request-Study` present and well formed, else `401`;
- `studyId` in the query string exactly once (names matched
  case-insensitively, as nginx does) and equal to `X-Auth-Request-Study`,
  else `403`;
- any study the request body names, and the stored row's study for an update
  or delete, equal to `X-Auth-Request-Study`, else `403`;
- `X-Auth-Request-User` present, else `401`. Only when
  `ANNOTATION_ALLOW_ANONYMOUS=true` (development stacks whose portal has no
  login) does a request without it act as `ANNOTATION_ANONYMOUS_USER`.

An annotation's `created_by` is `X-Auth-Request-User`, and only that user may
update or delete it.

**Trusted-proxy requirement.** Anyone who can reach the companion without
going through that proxy can claim any user. Deploy it:

- on the pod network, never `hostNetwork`;
- behind a Service that a NetworkPolicy opens to the ingress controller's
  namespace only (no other namespaces, no node ports);
- with no Ingress rule that reaches it without the auth subrequest. Probe
  routes (`/health`, `/ready`) read no identity and may be routed without
  auth.

A client that holds another credential the portal accepts in the auth
subrequest (HTTP basic, a data-access token) can use the companion as that
user; that is the portal's own authorization.

### Browser request rules

The portal's session cookie authorizes companion requests, so unsafe methods
(`POST`, `PUT`, `PATCH`, `DELETE`) must also:

- send `X-Requested-With: XMLHttpRequest`, else `403`;
- send `Content-Type: application/json` when there is a body, else `415`;
- send no `Origin`, or one that is the request's own host or is listed in
  `COMPANION_ALLOWED_ORIGINS`, else `403`.

The companion sends no CORS headers, so a page on another site cannot send
the custom header. Request bodies are capped at `COMPANION_MAX_BODY_BYTES`
(`413` above it), and every response is `Cache-Control: private, no-store`.

### Companion routes

Every route except the health and readiness routes takes `?studyId=<study>`.
The assistant stores proposals and writes annotations in-process with the
request's user; it calls no companion route over HTTP.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/annotations?studyId=&slide_id=` | The slide's live annotations in the study that the user may see (see [Visibility](#visibility)), each with its `comment_count`; rejected machine-made ones only with `include_rejected=true`; deleted ones never |
| POST | `/annotations?studyId=` | Create; the body's `study_id` must be `studyId`; machine provenance only from a trusted writer; `target.selector` must pass the [selector rules](#selector-rules) (`422`) |
| PUT | `/annotations/{id}?studyId=` | Update (creator only; `version` is an optimistic lock; `409` when the row changed since it was read, `404` when it was deleted); a sent `target` must pass the [selector rules](#selector-rules) |
| POST | `/annotations/batch?studyId=` | Create 1–100 annotations in one transaction (all or none), returned in request order; every item's study must be `studyId` |
| POST | `/annotations/{id}/review?studyId=` | `{"decision": "accept"\|"reject"\|"reset", "version"}` on a machine-made annotation: accept or reject by any user who can see it, reset by its reviewer only (see [Reviewing](#reviewing-machine-made-annotations)) |
| DELETE | `/annotations/{id}?studyId=` | Soft-delete (creator only); the comment thread is kept for a restore; `404` when it is missing or already deleted (see [Deleting and restoring](#deleting-and-restoring)) |
| POST | `/annotations/{id}/restore?studyId=` | `{"version"}`: bring a deleted annotation back under its id (creator only), `version` bumped; `404` unless it is deleted, `409` when `version` is not the deleted row's |
| POST | `/annotations/batch-update?studyId=` | `{"items": [{"id", "version", "body"?, "target"?, "visible_to"?}]}`: 1–100 updates under the PUT rules, all or none (see [Batches](#batch-update-delete-review-and-restore)) |
| POST | `/annotations/batch-delete?studyId=` | `{"items": [{"id", "version"}]}`: 1–100 soft deletes (creator only), all or none |
| POST | `/annotations/batch-review?studyId=` | `{"items": [{"id", "version", "decision"}]}`: 1–100 reviews (`accept`, `reject` or `reset`) under the review rules, all or none |
| POST | `/annotations/batch-restore?studyId=` | `{"items": [{"id", "version"}]}`: 1–100 restores of deleted annotations (creator only), all or none |
| GET | `/annotations/{id}/comments?studyId=[&offset=0][&limit=100]` | A page of the annotation's comment thread, oldest first, for anyone who can see the live annotation: `{"annotation_id", "comments", "total", "offset", "limit", "has_more", "next_offset"}`; `limit` 1–500 (default 100), `offset` 0–1000000 (see [Comment threads](#comment-threads)) |
| POST | `/annotations/{id}/comments?studyId=` | Add `{"body"}` to the thread; `422` once the annotation holds 1000 comments (deleted ones included) |
| PUT | `/annotations/{id}/comments/{cid}?studyId=` | Edit `{"body"}` (author only) |
| DELETE | `/annotations/{id}/comments/{cid}?studyId=` | Delete (author only; kept, flagged deleted) |
| GET | `/slide-notes?studyId=&slide_id=[&offset=0][&limit=100]` | A page of the slide's notes the user may see, with a tag summary over all of them: `{"slide_id", "notes", "tags", "total", "offset", "limit", "has_more", "next_offset"}`; `limit` 1–500 (default 100), `offset` 0–1000000 (see [Slide notes and tags](#slide-notes-and-tags)) |
| POST | `/slide-notes?studyId=` | Create `{"slide_id", "study_id", "text"?, "tags"?, "visible_to"?}`; the body's `study_id` must be `studyId`; `422` once the slide holds 1000 notes in the study (from all users) |
| PUT | `/slide-notes/{id}?studyId=` | Update `{"version", "text"?, "tags"?, "visible_to"?}` (creator only; `version` is an optimistic lock) |
| DELETE | `/slide-notes/{id}?studyId=[&version=]` | Delete (creator only; `409` when a given `version` is stale) |
| GET | `/agent/health` | Assistant status (reads no identity) |
| POST | `/agent/chat?studyId=` | Assistant turn as server-sent events; the body's `context.study_id` must be `studyId`. `WSI_AGENT_ENABLED` |
| GET | `/agent/actions?studyId=&session_id=` | The user's proposals for the session in the study |
| POST | `/agent/actions/{id}/{apply,reject,complete}?studyId=` | Decide a proposal; the proposal's study must be `studyId` |
| POST | `/agent/actions/{id}/commit-annotations?studyId=` | Write a proposal's annotations as the user, in the same transaction that completes it |
| GET | `/research/v1/health` | Research status (reads no identity) |
| GET | `/research/v1/slides/{study}/{slide}/models?studyId=` | Models for a slide; `{study}` must be `studyId`. `WSI_RESEARCH_ENABLED` |
| POST | `/research/v1/slides/{study}/{slide}/{region-search,similar-slides}?studyId=` | Retrieval; `{study}` must be `studyId` |
| POST | `/research/v1/mcp?studyId=` | JSON-RPC tools for cBioAgent; `arguments.study_id` must be `studyId` |
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness: annotation storage reachable |

#### Visibility

`visible_to` decides who besides the creator sees an annotation (the
creator always does):

- `null`, **the default when `visible_to` is left out**, keeps it private
  to its creator;
- `[]` shares it with everyone who can read the study;
- a list of group names shares it with those groups.

A loader or client that means to share its annotations with the study must
send `"visible_to": []` explicitly; one that sends nothing writes rows only
its own user can list. The creator can change it later with `PUT` (or
`batch-update`) and `"visible_to": []`.

#### Reviewing machine-made annotations

An annotation is machine-made when its `body.provenance.source` is `agent`,
`assistant`, `ai`, `model` or `precomputed` (any letter case). Any user who
can see it in its study may review it, not only its creator:

- `accept` sets `provenance.review_status: "accepted"`;
- `reject` sets `provenance.review_status: "rejected"`. The row is kept as
  an audit record of the model's output and the reviewer's decision; the
  list leaves it out unless `include_rejected=true`, and a later review can
  accept it again;
- both record `reviewed_by` (the proxy's user) and `reviewed_at` (UTC), keep
  every other field, and bump `version`. `version` must match the stored
  one (`409` otherwise).
- `reset` undoes the current review: it removes `review_status`,
  `reviewed_by` and `reviewed_at` from the provenance (the annotation is
  unreviewed again, and a rejected one is listed again without
  `include_rejected`), keeps every other field and bumps `version`.
  **Only the reviewer** (the stored `reviewed_by`) may reset; anyone else,
  the annotation's creator included, gets `403`. Once another user reviews
  it, only that user may reset it. A review stored without `reviewed_by`
  (written by a trusted writer) cannot be reset over this route. An
  annotation with no review answers `422`. The version check runs first,
  so a stale reset answers `409`.

```http
POST /annotations/5d0c…/review?studyId=luad_mskcc
{"decision": "reset", "version": 2}

200 {"id": "5d0c…", "version": 3,
     "body": {…, "provenance": {"source": "precomputed", "model": "m1"}}, …}
```

A human-made annotation answers `403`; one the user cannot see (or a
deleted one) answers `404`. Updating and deleting stay creator-only.

#### Who may write machine provenance

`provenance.source` decides who may review an annotation, so a browser may
not set it to a machine source:

- `POST /annotations` and `POST /annotations/batch` answer `422` when an
  item's `provenance.source` is a machine source, or its provenance carries
  `review_status`, `reviewed_by` or `reviewed_at`, unless the user is listed
  in `ANNOTATION_TRUSTED_WRITERS`. A batch with one such item saves none.
- `PUT /annotations/{id}` answers `422` when the new body names a machine
  source the stored row does not have (making a human annotation
  machine-made, or changing one machine source to another). The creator may
  still edit a machine-made annotation that keeps its source. The review
  fields always keep their stored values; only the review route sets them.
- The assistant writes its annotations in-process when a proposal is
  committed (`/agent/actions/{id}/commit-annotations`), not through these
  routes, so it needs no entry and stores `source: "agent"` as the
  committing user.
- A bulk loader of model output either writes to storage directly or calls
  these routes as a service identity listed in `ANNOTATION_TRUSTED_WRITERS`
  (an `X-Auth-Request-User` the portal asserts for it).
- Stored rows are never re-checked: existing machine-made rows stay
  listable, reviewable and editable by their creator.

Do not list `ANNOTATION_ANONYMOUS_USER` in `ANNOTATION_TRUSTED_WRITERS`:
with anonymous use on, every request without a user would be trusted.

#### Batch update, delete, review and restore

`batch-update`, `batch-delete`, `batch-review` and `batch-restore` take
1–100 `items`, each naming an annotation `id` (once per batch) and its
current `version`. The rows are read and locked, every item is checked
under the rule of its single-item route (`PUT`, `DELETE`, `/review`,
`/restore`), and only then is anything written, all in one transaction:
either every item is saved or none is.

| Status | When | Body |
|--------|------|------|
| `200` | Every item saved | The rows in request order: updated, reviewed or restored (`version` bumped), or, for `batch-delete`, as they were before deletion (same `version`, the one to restore with) |
| `422` | Fewer than 1 or more than 100 items, a repeated `id`, a malformed item (including a `target` the [selector rules](#selector-rules) refuse), an item the provenance rules refuse, or a `reset` of an annotation with no review | FastAPI's `detail` list; each `loc` is `["body", "items", <index>, ...]` |
| `404` | An item's annotation does not exist or is deleted (for a review, also when not visible to the user; for a restore, when it is not deleted) | `{"msg", "index", "id", "loc"}` for the first such item |
| `403` | An item's annotation is in another study, not the user's (update, delete, restore), not machine-made (review), or a `reset` by someone other than its reviewer | `{"msg", "index", "id", "loc"}` for the first such item |
| `409` | Any item's `version` is stale | `{"msg", "conflicts": [{"index", "id", "loc", "version", "current_version"}]}` listing every stale item |

Checks run in request order: the first missing, forbidden or invalid item
fails the batch at once, while stale versions are collected and answered
together, so a client can refetch exactly those rows.

#### Selector rules

The viewer hands `target.selector.value` to Annotorious, which renders an
`SvgSelector` as SVG markup, so every create and update (single, `batch`
and `batch-update`) checks the selector it is sent and answers `422`
(FastAPI's `detail` list, `loc` ending in `target`) unless it is one of the
shapes the viewer writes. Stored rows are never re-checked: an existing
annotation stays readable, and a `PUT` or batch item that leaves `target`
out keeps it.

- The selector object holds `type`, `value` and, optionally, `conformsTo`
  (an `http(s)` URL), nothing else; `value` is at most 1,000,000
  characters.
- `FragmentSelector`: `xywh=x,y,w,h` or `xywh=pixel:x,y,w,h`, four numbers.
- `SvgSelector`: one `<svg>` element (no attribute but the SVG `xmlns`)
  holding at least one shape, directly or inside one level of `<g>`
  (no attribute but `fill-rule`). The shapes and their only attributes:

  | Element | Attributes |
  |---------|------------|
  | `<polygon>`, `<polyline>` | `points`: numbers separated by commas or whitespace |
  | `<path>` | `d`: path commands `M L H V C S Q T A Z` (either case) and numbers |
  | `<circle>` | `cx`, `cy`, `r` (a point is `r="0"`) |
  | `<ellipse>` | `cx`, `cy`, `rx`, `ry` |
  | `<line>` | `x1`, `y1`, `x2`, `y2` |
  | `<rect>` | `x`, `y`, `width`, `height`, `rx`, `ry` |

  Any shape (and `<g>`) may also carry `fill-rule` (`nonzero` or
  `evenodd`), as a region with holes does:
  `<svg><g><path fill-rule="evenodd" d="M … Z M … Z" /></g></svg>`. A
  number is a decimal with an optional sign and exponent (`NaN`,
  `Infinity` and units are refused). Shapes may be self-closing or have an
  empty closing tag.
- Refused: any other element (`script`, `foreignObject`, `image`, `use`,
  `a`, `style`, nested `<g>`, ...), any other attribute (`on*`, `style`,
  `class`, `href`, `xlink:href`, `transform`, ...), text, entities and
  character references, comments, CDATA, processing instructions and
  DOCTYPEs.

#### Comment threads

Each annotation has a comment thread, separate from the annotation's own
note (`body.comment`, which only its creator edits):

- anyone who can see the annotation in the authorized study may read the
  thread and add to it; an annotation in another study answers `403`, one
  the user cannot see (`visible_to`) answers `404`, as a missing one does;
- only a comment's author may edit or delete it, and only while they can
  still see the annotation;
- a deleted comment is kept, flagged `deleted`, and left out of the thread
  and of `comment_count`;
- deleting the annotation keeps its thread: while the annotation is
  deleted, every comment route answers `404` for it (as for a missing
  one), to its creator too; a restore brings the thread back as it was,
  and the [retention purge](#deleting-and-restoring) removes it with the
  annotation;
- a comment `body` is 1–4000 characters after trimming surrounding
  whitespace (`422` otherwise).

A comment is `{"id", "annotation_id", "study_id", "author", "body",
"created_at", "updated_at"}`; `author` is `X-Auth-Request-User`.

An annotation holds at most 1000 comments, deleted ones included (so
deleting and reposting cannot grow a thread without bound); the next `POST`
answers `422`. The list is paged: `offset` (default `0`, at most
`1000000`) and `limit` (default `100`, `1`–`500`; `422` outside those)
pick a slice of the live comments, oldest first:

```json
{
  "annotation_id": "5d0c…",
  "comments": [
    {"id": "…", "annotation_id": "5d0c…", "study_id": "luad_mskcc", "author": "alice",
     "body": "Agree", "created_at": "2026-11-18T14:02:11Z", "updated_at": "2026-11-18T14:02:11Z"}
  ],
  "total": 1,
  "offset": 0,
  "limit": 100,
  "has_more": false,
  "next_offset": null
}
```

`total` counts the thread's live comments (as `comment_count` does);
`next_offset` is the `offset` of the next page, `null` on the last
(`has_more` is then `false`). Comments that are deleted between page
requests shift later pages back by one.

At every start the companion also deletes comments whose annotation row no
longer exists (left by anything that removed annotation rows outside these
routes). A deleted annotation keeps its row until the purge, so its thread
is not swept.

#### Deleting and restoring

`DELETE /annotations/{id}` and `batch-delete` are soft deletes: they set
the row's `deleted_at` and change nothing else (not `version`, not the
comment thread). A deleted annotation:

- is left out of `GET /annotations` (even with `include_rejected=true`),
  and its id answers `404` to `PUT`, `/review`, `DELETE`, every batch but
  `batch-restore`, and every comment route;
- still holds its id, so no new annotation can take it, and its comments
  still count toward the thread's 1000-comment cap;
- does not affect slide notes or other annotations' `comment_count`.

Its creator brings it back, under the same id and with its thread, with
`POST /annotations/{id}/restore` (or `batch-restore`), sending the version
the row had when it was deleted:

```http
POST /annotations/5d0c…/restore?studyId=luad_mskcc
{"version": 2}

200 {"id": "5d0c…", "version": 3, "comment_count": 4, "created_by": "alice", …}
```

The restored annotation is the stored row with `version` bumped and
`updated_at` set, so a client still holding the pre-delete version gets
`409` on its next write. A rejected annotation stays rejected. `404` when
the id is missing, live, or already purged; `403` for anyone but the
creator or another study; `409` for another `version`.

**Retention purge.** Annotations deleted more than
`ANNOTATION_DELETED_RETENTION_DAYS` days ago (default `30`) are removed for
good, with their comment threads, at every start and once a day while the
companion runs (each worker runs it; it is idempotent). `0` turns the purge
off and keeps deleted annotations restorable forever. Live annotations are
never purged. Annotations deleted before soft delete existed (schema
version 3) were removed outright and cannot be restored.

#### Slide notes and tags

A slide note belongs to a slide (`slide_id`, the opaque 12-hex slide key)
in a study, not to a shape. It carries `text` (up to 4000 characters), up
to 20 `tags`, or both, never neither (`422`).

- **Tags** are 1–40 characters of letters, digits, spaces and `_ - . : / + #`,
  starting with a letter or digit. Runs of whitespace become one space, and
  a tag repeated in another letter case is dropped; otherwise a tag is
  stored as sent.
- **Visibility** is the annotation model: the creator always sees a note;
  `visible_to: null` (the default) keeps it private, `[]` shows it to
  everyone who can read the study, and a list of group names to those
  groups.
- **Writes**: only the creator may update or delete a note. `PUT` needs
  the current `version` and bumps it; `DELETE` checks `version` when it is
  given. Fields left out of a `PUT` keep their stored values.
- **Cap**: a slide holds at most 1000 notes in a study, from all users;
  the next `POST` answers `422`.

`GET /slide-notes` takes `offset` (default `0`, at most `1000000`) and
`limit` (default `100`, `1`–`500`; `422` outside those) over the notes the
user may see, oldest first, and answers:

```json
{
  "slide_id": "0123456789ab",
  "notes": [
    {"id": "…", "slide_id": "0123456789ab", "study_id": "luad_mskcc", "text": "Margin involved",
     "tags": ["QC", "LUAD"], "created_by": "alice", "visible_to": [], "version": 1,
     "created_at": "2026-11-18T14:02:11Z", "updated_at": "2026-11-18T14:02:11Z"}
  ],
  "tags": [{"tag": "QC", "count": 1}, {"tag": "LUAD", "count": 1}],
  "total": 1,
  "offset": 0,
  "limit": 100,
  "has_more": false,
  "next_offset": null
}
```

`notes` is the page, oldest first; `total` counts every note the user may
see, and `tags` counts, for each tag, all those notes that carry it (not
only the page's), most used first. `next_offset` is the `offset` of the
next page, `null` on the last.

### Annotation storage schema

| Table | Holds |
|-------|-------|
| `annotations` | `id`, `slide_id`, `study_id`, `body` (JSON), `target` (JSON), `created_by`, `visible_to` (JSON list or null), `version`, `created_at`, `updated_at`, `deleted_at` (null while live); partial index on `deleted_at` for deleted rows |
| `annotation_comments` | `id`, `annotation_id`, `study_id`, `author`, `body`, `created_at`, `updated_at`, `deleted`; indexed on `(annotation_id, created_at)` |
| `slide_notes` | `id`, `slide_id`, `study_id`, `text`, `tags` (JSON list), `created_by`, `visible_to`, `version`, `created_at`, `updated_at`; indexed on `(slide_id, study_id)` |

On Postgres, `annotation_schema_migrations` records the version of the
`annotations` component (now `3`: version 1 created `annotations`, version 2
added `annotation_comments` and `slide_notes`, version 3 added the nullable
`annotations.deleted_at` and its partial index). Each start takes the schema
advisory lock and applies only the steps above the recorded version; every
step only creates tables, nullable columns without a default and indexes,
so stored annotations are kept as they are (version 3 rewrites no row, and
every existing row stays live). On SQLite, each start creates any missing
table, column and index.

### Companion configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANNOTATION_DATABASE_URL` | — | Postgres/Lakebase DSN; SQLite at `ANNOTATION_DB_PATH` when unset |
| `ANNOTATION_DB_PATH` | `/data/annotations.db` | SQLite path |
| `ANNOTATION_DB_POOL_MIN_SIZE` / `ANNOTATION_DB_POOL_MAX_SIZE` | `1` / `8` | Postgres pool size per worker |
| `ANNOTATION_DB_COMMAND_TIMEOUT` | `30` | Postgres statement timeout, seconds |
| `ANNOTATION_DB_MAX_INACTIVE_CONNECTION_LIFETIME` | `300` | Close a pooled Postgres connection idle this many seconds |
| `ANNOTATION_DB_MAX_CONNECTION_AGE_SECONDS` | `1800` | Replace every pooled connection once the pool's connections are this old (`0` = never); keep it below the credential lifetime |
| `ANNOTATION_DATABASE_PASSWORD_PROVIDER` | — | `databricks`: log in with a Lakebase database credential (an OAuth token, about 1 h) fetched for each new connection instead of the DSN's password |
| `ANNOTATION_LAKEBASE_INSTANCE` | — | Lakebase database instance name(s), comma-separated, for that credential |
| `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET` | — | Workspace and service principal that request the credential (OAuth machine-to-machine); `DATABRICKS_TOKEN` instead of the client pair is accepted |
| `ANNOTATION_ALLOW_ANONYMOUS` | `false` | Development only: accept requests without `X-Auth-Request-User` |
| `ANNOTATION_ANONYMOUS_USER` | `anonymous` | The user those requests act as |
| `COMPANION_ALLOWED_ORIGINS` | — | Comma-separated origins, besides the request's own host, allowed to send unsafe requests |
| `COMPANION_MAX_BODY_BYTES` | `4194304` | Largest accepted request body |
| `N_WORKERS` | `4` | Gunicorn workers; use `1` for the first start on an empty SQLite database |
| `ANNOTATION_LOCAL_DEVELOPMENT` | `false` | Development only: the user `local-development` may update and delete any annotation |
| `ANNOTATION_DELETED_RETENTION_DAYS` | `30` | Days a deleted annotation stays restorable before the purge removes it and its comment thread; `0` keeps them forever (see [Deleting and restoring](#deleting-and-restoring)) |
| `ANNOTATION_TRUSTED_WRITERS` | — | Comma-separated users (`X-Auth-Request-User`) that may create machine-made annotations and set review fields over HTTP (see [Who may write machine provenance](#who-may-write-machine-provenance)) |
| `WSI_AGENT_ENABLED` | `false` | Enable the server-side research assistant; every `/agent` route but `/agent/health` answers `404` while it is off |
| `WSI_AGENT_PROVIDER` | `bedrock` | Assistant provider: `bedrock` (production), `openai`, or `databricks` |
| `BEDROCK_AGENT_MODEL` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Bedrock model used by the research assistant |
| `BEDROCK_AWS_REGION` | `us-east-1` | Bedrock runtime region |
| `BEDROCK_AWS_PROFILE` | — | Explicit AWS profile used for Bedrock credentials |
| `BEDROCK_AWS_ENDPOINT_URL` | Regional Bedrock endpoint | Optional Bedrock runtime endpoint override |
| `OPENAI_API_KEY_FILE` | — | Secret-mounted file containing the server-side OpenAI key |
| `WSI_AGENT_MODEL` | `BEDROCK_AGENT_MODEL` | Assistant model for any provider: Bedrock model ID, OpenAI model, or Databricks serving endpoint name |
| `DATABRICKS_HOST` | profile `host` | Workspace URL for `WSI_AGENT_PROVIDER=databricks`; the OpenAI Responses API is called at `<host>/serving-endpoints` |
| `DATABRICKS_TOKEN` / `DATABRICKS_TOKEN_FILE` | profile `token` | Workspace token (or a file holding it) for the Databricks provider |
| `DATABRICKS_CONFIG_FILE` / `DATABRICKS_CONFIG_PROFILE` | `~/.databrickscfg` / `DEFAULT` | Databricks CLI profile used when host or token is unset |
| `WSI_AGENT_TIMEOUT_SECONDS` | `60` | Maximum model request duration |
| `WSI_AGENT_RATE_LIMIT_PER_MINUTE` | `10` | Per-user assistant request limit |
| `WSI_RESEARCH_ENABLED` | `false` | Enable manifest-backed research retrieval; `/research/v1` routes but `/research/v1/health` answer `404` while it is off |
| `WSI_RESEARCH_MANIFEST_URI` | — | Local or S3 URI for the published study-qualified research manifest |
| `WSI_RESEARCH_RATE_LIMIT_PER_MINUTE` | `30` | Per-user region/similar-slide retrieval limit |
| `WSI_RESEARCH_REGION_MODEL` | `quiltnet_pmb` | Tile-level model for text-to-region search (`quiltnet_pmb` or `reef_v2_hoptimus1_keep`) |
| `WSI_RESEARCH_EMBEDDING_URL` | — | Internal region retrieval worker URL (the QuiltNet worker is the `research` Compose profile) |
| `WSI_RESEARCH_EMBEDDING_URLS` | — | Optional per-model worker URLs, `model=url,model=url`; other models use `WSI_RESEARCH_EMBEDDING_URL` |
| `WSI_RESEARCH_EMBEDDING_TIMEOUT_SECONDS` | `60` | Timeout for semantic retrieval worker calls, including cold model/artifact load |
| `WSI_RESEARCH_STATIC_FALLBACK` | `true` | Return labeled fallback regions when semantic retrieval is unavailable |
| `QUILTNET_MODEL_NAME` | `hf-hub:wisdomik/QuiltNet-B-16-PMB` | QuiltNet text encoder used by the retrieval worker |

The companion needs none of the pixel settings (seal key, object storage,
Redis).

With the Lakebase provider, `ANNOTATION_DATABASE_URL` names the host,
database and the service principal's Postgres role (its client ID) with no
password, for example
`postgresql://<client-id>@<instance-host>:5432/<database>?sslmode=require`.
The credential is generated through the workspace REST API
(`POST /api/2.0/database/credentials`) and cached until five minutes before it
expires; neither it nor the client secret is logged.

### ingress-nginx

The `$wsi_study` map from [Ingress](#ingress) matches `studyId` on any path,
so the companion Ingress uses it unchanged:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: wsi-companion
  annotations:
    nginx.ingress.kubernetes.io/auth-url: "http://cbioportal.<namespace>.svc.cluster.local:8080/api/studies/$wsi_study/access"
    nginx.ingress.kubernetes.io/auth-method: GET
    nginx.ingress.kubernetes.io/auth-response-headers: "X-Auth-Request-User,X-Auth-Request-Study"
    # No auth cache: a write or logout must take effect at once.
    # The assistant's chat carries a viewport image of up to ~2.8 MB.
    nginx.ingress.kubernetes.io/proxy-body-size: "4m"
    # Server-sent events from /agent/chat.
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "300"
    nginx.ingress.kubernetes.io/enable-access-log: "false"
spec:
  ingressClassName: nginx
  rules:
    - host: cbioportal.example.org
      http:
        paths:
          - path: /wsi/annotations
            pathType: Prefix
            backend: {service: {name: wsi-companion, port: {number: 8080}}}
          - path: /wsi/slide-notes
            pathType: Prefix
            backend: {service: {name: wsi-companion, port: {number: 8080}}}
          # When the assistant and research retrieval are enabled:
          - path: /wsi/agent/chat
            pathType: Exact
            backend: {service: {name: wsi-companion, port: {number: 8080}}}
          - path: /wsi/agent/actions
            pathType: Prefix
            backend: {service: {name: wsi-companion, port: {number: 8080}}}
          - path: /wsi/research/v1/slides/
            pathType: Prefix
            backend: {service: {name: wsi-companion, port: {number: 8080}}}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: wsi-companion
spec:
  podSelector:
    matchLabels: {app: wsi-companion}
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels: {kubernetes.io/metadata.name: ingress-nginx}
      ports:
        - port: 8080
```

Keep `underscores_in_headers` off (the controller default), so
`X_Auth_Request_User` cannot stand in for the real header.

### Plain nginx

```nginx
location ~ ^/wsi/(annotations|slide-notes|agent|research)(/.*)?$ {
    if ($wsi_study = "!") {
        return 400;
    }
    auth_request /_wsi_companion_access;
    auth_request_set $wsi_auth_user  $upstream_http_x_auth_request_user;
    auth_request_set $wsi_auth_study $upstream_http_x_auth_request_study;
    proxy_pass http://companion:8080;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-For $remote_addr;
    # Always set both, so a client's copy never passes through.
    proxy_set_header X-Auth-Request-User  $wsi_auth_user;
    proxy_set_header X-Auth-Request-Study $wsi_auth_study;
    proxy_set_header Cookie "";
    client_max_body_size 4m;
    # Server-sent events from /agent/chat.
    proxy_buffering off;
    proxy_read_timeout 300s;
}

location = /_wsi_companion_access {
    internal;
    proxy_pass http://portal/api/studies/$wsi_study/access;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Cookie $http_cookie;
    proxy_set_header Host $http_host;
}
```

A location that sets any `proxy_set_header` inherits none from the server
block, so each companion location sets both identity headers itself.

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

These routes run in the companion service (see
[Companion service](#companion-service)). When enabled, `/research/v1`
exposes only study-scoped, bounded references to
published model assets and candidate regions. `/research/v1/mcp` provides the
same read-only region and similar-slide tools to cBioAgent. The assistant's
annotation tools (on every provider) create pending proposals in the agent
action store; the browser applies them through `/annotations/batch` only after
user approval. Its `wsi_find_regions` tool searches the current slide with
`WSI_RESEARCH_REGION_MODEL` and stores the ranked regions as a retrieval run;
`wsi_propose_retrieval_annotations` turns chosen ranks of that run into one
annotation-batch proposal.

`reef_v2_hoptimus1_keep` scores stored REEF v2 H-optimus-1 tile features,
mapped into KEEP text space, in a separate worker. Its manifest entries carry
the slide's internal image ID, which the tile server sends only to that worker;
API responses, assistant tool output and stored retrieval runs keep only the
documented region fields. `tools/build_keep_manifest.py` builds such a
manifest from the slide table's sealed sources and prints counts only.

Semantic search is served by the optional `quiltnet-search` Compose service.
Start it with `WSI_RESEARCH_EMBEDDING_URL=http://quiltnet-search:8080 docker
compose --profile research up --build`; the companion service remains the
study boundary and the worker receives only manifest artifact references and
the interpreted query plan.


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
service, so it performs no study authorization; do not expose it. Its
`companion` service listens on `127.0.0.1:${COMPANION_PORT:-8081}` and expects
the identity headers a proxy would set; send them yourself to try it. Use the
same `WSI_SOURCE_SEAL_KEY` here and wherever the study's sealed sources were
produced.

The optional WSI research assistant is disabled by default. Enable it only in
an environment with an approved secret manager or a read-only mounted key
file, set `OPENAI_API_KEY_FILE` to the path visible inside the container, and
set `WSI_AGENT_ENABLED=true` on the companion service. The browser holds no
credential for it beyond the portal session; the OpenAI credential stays in
the companion process. Assistant
annotation and viewer changes are persisted as pending proposals and require
explicit approval in the viewer before application.

With `WSI_AGENT_PROVIDER=databricks` the assistant runs through the same
Agents SDK path against a Databricks model-serving endpoint named by
`WSI_AGENT_MODEL` (for example `databricks-gpt-5-6-terra`). The workspace
token stays in the companion process and Agents SDK tracing is disabled.

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
