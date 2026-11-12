# cBioPortal WSI tile-server runbook

This service is a plain pixel renderer. cBioPortal owns authentication,
study authorization, hierarchy, and the slide rows; the ingress enforces study
authorization on every pixel request. The tile server does not resolve image
IDs, query Databricks, read a resource index, search, authenticate users, or
expose clinical metadata.

## Source of truth

- `../cbioportal` serves the study's resource data, whose WSI rows carry each
  slide's opaque `key` and `sealed_source`.
- `../cbioportal-frontend` requests pixels with
  `?studyId=<study>&key=<slide key>&enc=<sealed source>` (thumbnails add the
  optional requested `width`/`height`); it never sees or sends a source URL,
  and sends no `Authorization` header.
- The deployment repository owns ingress, probes, secrets, and rollout
  resources, including the auth subrequest below.

`enc` is the slide's sealed source: the real `image_id` and both source URLs
encrypted by the data provider with AES-256-GCM under the raw 32-byte
`WSI_SOURCE_SEAL_KEY` (AAD = the slide's `key`). cBioPortal stores and serves
it verbatim and cannot decrypt it. The tile server opens it, applies
`check_source_uri` (scheme, approved prefix, extension, no traversal), and
reads only those URLs. An `enc` that does not open under the request's `key`
returns `404` (`slide not found`); a sealed source outside the URI policy
returns `404` (`source slide not found`) and is logged without its URL.
The serving contract version is `wsi-serving-v6`.

## Production topology

The existing `/wsi` ingress routes to this service and must authorize every
pixel request before forwarding it: an nginx `auth_request` subrequest to
cBioPortal's `GET /api/studies/{studyId}` (`studyId` from the request's query
string), forwarding the browser's `Cookie` header. Forward only on `2xx`.
The tile server requires `studyId` (`400` without it) so the ingress always
has a study to check, and refuses a repeated `studyId`, `key`, `enc`,
`width` or `height`, matching names case-insensitively as nginx `$arg_`
does, so the ingress and service cannot read different values. Other
parameters are ignored. Never expose the service without that ingress. Keep `/health` and `/ready` public for
orchestration. Ingress and deployment configuration are authoritative for
timeouts, network policy, worker count, and block-cache volumes.

Serve the service same-origin with cBioPortal (`/wsi` on the portal host).
The ingress check relies on the browser sending the portal session cookie
with each pixel request, and the service sets no credentialed CORS, so a
cross-origin deployment would fail closed rather than work.

Working ingress-nginx and plain nginx configurations are in the README's
[Ingress](../README.md#ingress) section. In short:

- The auth subrequest takes the study from `$request_uri` through a `map`:
  inside an auth subrequest `$arg_studyId` is empty.
- An auth cache, if any, must be keyed on the session cookie (and any other
  credential the portal accepts) and the study. A key without the session
  cookie lets one user's approval authorize another user's request.
- The ingress must set `X-Forwarded-For` to the client address
  (ingress-nginx: `compute-full-forwarded-for: "false"`; `use-forwarded-headers`
  `"true"` only behind a trusted L7 load balancer listed in
  `proxy-real-ip-cidr`). The cache-miss rate limit is keyed on the last
  `X-Forwarded-For` entry (else the peer address) and the slide `key`; if a
  hop appends its own peer address instead, every request carries the same
  last entry and all clients share one bucket.
- The ingress must not log query strings for `/wsi`, because `enc` is a
  bearer value for its slide.

| Status | Returned by | Meaning |
|--------|-------------|---------|
| `401`, `403` | Ingress only | No session, or the user cannot read `studyId`; the service never returns them |
| `400` | Service | Missing, malformed, or repeated parameter; client-supplied source |
| `404` | Service | `enc` does not open under `key`; sealed source outside the URI policy; source or thumbnail object missing; tile outside the slide |
| `422` | Service | Overview tile needs preprocessing (`overview_requires_preprocessing`) |
| `429` | Service | Cache-miss rate limit (`Retry-After`) |
| `500` | Service | Unexpected slide read failure |
| `502` | Service | Stored thumbnail unreadable, over `THUMBNAIL_MAX_DECODE_PIXELS`, or unavailable |
| `503` | Service | Busy, source temporarily unavailable, or seal key not configured (`Retry-After`) |

Under ingress-nginx, a study the portal answers `404` for reaches the client
as a `500` from the ingress, since `auth_request` accepts only `2xx`, `401`,
and `403`.

## Required environment

```text
AWS_ENDPOINT_URL=<S3-compatible endpoint>
AWS_ACCESS_KEY_ID=<object-store key>
AWS_SECRET_ACCESS_KEY=<object-store secret>
WSI_SOURCE_SEAL_KEY=<standard base64 of the 32-byte key the data provider seals sources with>
WSI_ALLOWED_SOURCE_SCHEMES=s3
WSI_ALLOWED_SOURCE_PREFIXES=<approved source URI prefixes>
WSI_ALLOWED_THUMBNAIL_PREFIXES=<approved thumbnail URI prefixes>
REDIS_URL=<password-protected Redis URL>
```

The URI prefix allowlists are a de-identification boundary; leave them empty
only for an isolated non-publishing unit test.

## Endpoints and smoke checks

```bash
curl -fsS https://cbioportal.example.org/wsi/health
curl -fsS https://cbioportal.example.org/wsi/ready
curl -i https://cbioportal.example.org/wsi/tiles/zxy/0/0/0
```

Through the ingress, the final command must be refused (no study to
authorize); directly against the service it returns `400`. With a session
cookie for a user who can read the study and a slide row's `key` and
`sealed_source`:

```bash
curl -fsS -G -H "Cookie: SESSION=${SESSION}" \
  --data-urlencode "studyId=${STUDY_ID}" \
  --data-urlencode "key=${SLIDE_KEY}" \
  --data-urlencode "enc=${ENC}" \
  "https://cbioportal.example.org/wsi/tiles/zxy/0/0/0"
```

Also verify that the same request without the cookie, or with a study the
user cannot read, is refused by the ingress; that adding `source=...` or an
`X-WSI-Source` header returns `400`; and that swapping in another slide's
`key` (keeping `enc`) returns `404`. `tests/smoke.py` runs these checks
except the unreadable-study one: with `WSI_SMOKE_COOKIE` set it targets the
ingress and requires the parameterless tile request to be refused and the
cookieless slide request to return `401` or `403`; without it, it targets the
service directly and requires `400` for the parameterless request.

`WSI_SOURCE_SEAL_KEY` is required: the process refuses to start, `/ready`
returns `503`, and every pixel request returns `503` unless it decodes to
exactly 32 bytes. It is held only by the data provider that seals sources and
by this service; never give it to cBioPortal. Rotating it means re-sealing
every published `sealed_source` and re-importing the study files.

## Data preparation

Each servable slide's row in the study's resource data carries a
`metadata.serving` object:

```text
key, sealed_source, tile_metadata_json,
thumbnail_width, thumbnail_height, thumbnail_content_type
```

`sealed_source` seals the `image_id`, source URL, and thumbnail URL with
`WSI_SOURCE_SEAL_KEY`; the study files never carry them in clear. A slide row
without a `serving` object is not viewable. No registry or manifest is
mounted into the online tile-server pod.

Thumbnail generation, study-file export, and any batch scheduling are owned by
the deployment's offline preparation pipeline, not this repository. A slide
becomes servable only after its thumbnail is published, the study files are
exported, and cBioPortal core has imported them. See the README's
"Offline preparation" section for the full contract.

The frontend only requests `/thumbnails` and never uploads artifacts.

## Response and cache policy

- Tiles: `private, max-age=3600`, vary on `Cookie`.
- Thumbnails: `private, max-age=300`, vary on `Cookie`.
- Redis is an optimization, never an authorization boundary.
- Overview decodes that exceed `MAX_DECODE_PIXELS` return HTTP 422 with
  `overview_requires_preprocessing`.

Application logs must not include `enc` values, source URLs, patient IDs, or
slide IDs. Keep operation type, dimensions, status, timing, and exception class.

## Local integration

The local cBioPortal compose rehearsal should give the tile server the
`WSI_SOURCE_SEAL_KEY` the local study's sealed sources were produced with, and
route `/wsi` through an ingress that performs the study auth subrequest. For
mounted local slides, explicitly set `WSI_ALLOWED_SOURCE_SCHEMES=s3,file` and
include `file:///app/testdata/` in both URI prefix allowlists; production
should remain `s3` only.
