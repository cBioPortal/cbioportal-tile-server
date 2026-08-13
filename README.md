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

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Public liveness probe |
| GET | `/ready` | Public readiness probe (auth configuration only) |
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
| `WSI_AUTH_AUDIENCE` | `cbioportal-wsi` | Capability audience |
| `WSI_AUTH_MAX_TTL` | `300` | Maximum capability lifetime in seconds |
| `WSI_ALLOWED_SOURCE_SCHEMES` | `s3` | Comma-separated schemes accepted in source URLs |
| `REDIS_URL` | `redis://redis:6379` | Optional tile/thumbnail cache |
| `TILE_SIZE` | `256` | Tile edge length |
| `JPEG_QUALITY` | `85` | JPEG encoding quality |
| `MAX_DECODE_PIXELS` | `4194304` | Maximum on-demand tile decode |
| `MAX_OPEN_SLIDES` | `64` | Open-slide LRU capacity |
| `MAX_IMAGE_OPERATIONS` | `2` | Concurrent pixel operations per worker |
| `N_WORKERS` | `4` | Gunicorn worker count |
| `BLOCKCACHE_PATH` | — | Optional local range-read cache |
| `CORS_ORIGINS` | internal cBioPortal origins | Allowed browser origins |
| `RATE_LIMIT_PER_MINUTE` | `120` | Per-capability request limit; `0` disables it |

Thumbnail artifacts are generated offline by
`tools/generate_slide_thumbnails.py` and their URL, dimensions, content type,
and tile metadata are loaded into the cBioPortal WSI slide table. A slide is
published with `can_serve_tiles=false` until all of those fields are present.
The online service does not generate thumbnails or consult a manifest.

Tile and thumbnail responses are private-cacheable and vary on `Authorization`.
Redis is an optimization only; a cache outage does not change authorization.
If a requested overview cannot be decoded within the configured pixel bound,
the tile endpoint returns HTTP 422 with
`{"error":"overview_requires_preprocessing"}`.

## Quick start

```bash
python3 tools/write_dev_env.py
printf 'WSI_AUTH_SECRET=%s\nREDIS_PASSWORD=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 24)" >> .env
docker compose up --build
```

The compose file is a local rehearsal. Configure the same secret, audience,
and compatible TTL in cBioPortal and this service.

## Offline preparation

The backend data pipeline publishes one WSI release containing hierarchy rows
plus the pixel contract fields (`source_url`, `tile_metadata_json`,
`thumbnail_url`, dimensions, and content type). The thumbnail generator uses
the same intrinsic tile metadata when creating the registry artifact. The
loader and SQL pipeline are intentionally offline tooling; none of their
metadata clients are imported by the FastAPI runtime.

The cBioPortal ingestion boundary is the study-scoped `meta_wsi.txt` and
`data_wsi.txt` pair. Export it from the canonical association table with:

```bash
python3 tools/export_materialized_hierarchy_snapshot.py \
  --study-dir /path/to/study \
  --study-id study_id
```

Load the validated files into cBioPortal ClickHouse with:

```bash
python3 tools/load_clickhouse_hierarchy.py /path/to/study/meta_wsi.txt \
  --version 20260811030000
```

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
