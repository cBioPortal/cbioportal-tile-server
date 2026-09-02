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

Thumbnail generation and registry publication are an offline prerequisite to
the API. They are not performed by the frontend or by the FastAPI request
handlers. The production sequence is:

1. A separate cron, Slurm, or equivalent scheduled job runs
   `tools/run_thumbnail_pipeline_slurm.sh` (or an equivalent wrapper around
   `tools/generate_slide_thumbnails.py`).
2. The batch reads eligible `slide_inventory` rows and source slides from the
   S3/Dell ECS-compatible store, writes immutable master JPEGs back to that
   store, and upserts
   `cdsi_prod.pathology_data_mining.slide_thumbnail_registry` with
   `artifact_uri`, `tile_metadata_json`, `width`, `height`, and
   `content_type`.
3. The thumbnail publisher automatically runs
   `tools/generate_thumbnail_variants.py` after the master registry is
   published. It creates 128×96 navigation derivatives under a
   manifest-versioned prefix and publishes their serving pointers. The first
   run adds the serving-pointer columns to the existing registry; reruns are
   idempotent.
4. The PDM Databricks WSI bundle publishes the serving manifest, then computes
   canonical associations and `can_serve_tiles`. The bundle must run only
   after the thumbnail batch has completed for the input inventory (use a job
   dependency or completion watermark).
5. The exporter carries `SOURCE_URL`, `TILE_METADATA_JSON`, `THUMBNAIL_URL`,
   dimensions, and content type into `meta_wsi.txt`/`data_wsi.txt`, and writes
   the standard pathology timeline pair with procedure offsets and provenance.
   Every canonical slide with an available timeline date is represented;
   explicitly classified non-H&E/IHC slides use the `Other` timeline subtype
   and an all-slides linkout. Rows without a usable timeline date remain in
   the WSI hierarchy but cannot be placed on the timeline.
6. cBioPortal core imports the WSI snapshot and timeline files through the
   standard study importer and is the sole ClickHouse writer.

The frontend is read-only: it requests the backend access bundle and then
requests `/thumbnails`; it has no ECS/S3 upload credentials and never writes
Databricks tables. `app/thumbnail_worker.py` is a controlled on-demand CLI
that can write a generated JPEG to the configured S3/ECS-compatible location,
but it does not update `slide_thumbnail_registry` and is not a production
publication mechanism. Keep it limited to development, rehearsal, or explicit
remediation.

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
| `WSI_THUMBNAIL_REGISTRY_TABLE` | `cdsi_prod.pathology_data_mining.slide_thumbnail_registry` | Three-part Unity Catalog table used by the offline thumbnail publisher |
| `WSI_CANONICAL_ASSOCIATION_TABLE` | `cdsi_prod.pathology_data_mining.canonical_slide_associations` | Three-part table read by metadata and export tooling |
| `WSI_SUMMARY_TABLE` | `cdsi_prod.pathology_data_mining.sample_wsi_summary` | Three-part summary table used by clinical-file tooling |
| `WSI_STAIN_CLASSIFICATION_TABLE` | `cdsi_prod.pathology_data_mining.slide_stain_classification` | Approved offline H-Optimus release consumed by canonical stain routing |

Thumbnail artifacts are generated offline by
`tools/generate_slide_thumbnails.py` and their URL, dimensions, content type,
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

The backend data pipeline publishes one WSI snapshot containing hierarchy rows
plus the pixel contract fields (`source_url`, `tile_metadata_json`,
`thumbnail_url`, dimensions, and content type). The thumbnail generator uses
the same intrinsic tile metadata when creating the registry artifact. The
exporter and SQL pipeline are intentionally offline tooling; none of their
metadata clients are imported by the FastAPI runtime.

Run the thumbnail batch as a separate scheduled process before the canonical
Databricks refresh. The Slurm wrapper runs the navigation-variant job after
master publication. For a standalone backfill or repair, run:

```bash
python3 tools/generate_thumbnail_variants.py \
  --warehouse-id "$DATABRICKS_WAREHOUSE_ID" \
  --variant-root-uri s3://mskmind-bkt/wsi-thumbnails/variants/nav-128x96
```

A successful run must publish both the object-store artifacts and the matching
registry rows; writing only a JPEG or only a manifest is insufficient. Legacy
successful rows with missing `tile_metadata_json` must be backfilled or
regenerated before export.

The cBioPortal ingestion boundary is the study-scoped `meta_wsi.txt`/
`data_wsi.txt` pair plus the generated pathology timeline pair. Export them
from the canonical association table with:

```bash
python3 tools/export_materialized_hierarchy_snapshot.py \
  --study-dir /path/to/study \
  --study-id study_id
```

If an association snapshot was exported before the thumbnail registry finished,
hydrate it before importing. This joins only complete, source-matched registry
records; slides without a successful artifact remain explicitly non-servable:

```bash
python3 tools/hydrate_wsi_asset_metadata.py \
  --meta-wsi /path/to/study/meta_wsi.txt \
  --registry-jsonl /path/to/thumbnail-results.jsonl \
  --output-data-wsi /path/to/study/data_wsi.txt \
  --report-json /path/to/study/wsi-hydration-report.json
```

The output replacement is atomic. The command reports hydrated, unchanged,
incomplete, and source-mismatch rows; only complete successful registry rows
become servable. `materialize_dev_wsi_snapshot.py` performs this same join
automatically before loading its isolated dev namespace, so the explicit
command is only needed when repairing an already-exported study directory. Add
`--fail-on-incomplete` in CI when a release must contain a complete pixel
bundle for every association; omit it for a study that intentionally retains
unavailable source rows as non-servable provenance.

Load the exported files through the standard cBioPortal importer. The core
importer validates and resolves the complete snapshot, then writes the
normalized ClickHouse hierarchy and the sample- and patient-level WSI count
attributes used by the Study View Clinical Data tab:

```bash
metaImport.py -s /path/to/study
```

The count attributes are derived from matched WSI placements during the same
import, so a separate clinical count file is optional. If one is generated for
an existing workflow, `tools/generate_wsi_sample_count_clinical_file.py` writes
its metadata beside the count data without modifying the source `meta_wsi.txt`.

WSI is not supported by incremental (`metaImport.py -d`) imports. Build the
inactive blue/green database from a fresh schema and import each WSI snapshot
once; discard and rebuild the inactive database after a failed or repeated
import.

The tile server is deliberately not a ClickHouse writer. It receives source
URLs and tile metadata from cBioPortal's WSI access endpoint and serves the
authorized pixel and thumbnail requests.

### Image-assisted stain routing

The offline stain-classifier publisher writes an approved release to
`WSI_STAIN_CLASSIFICATION_TABLE`. The canonical association job joins the
latest approved row by `image_id`, applies exact binary adjudications first,
and then permits only high-confidence H&E-to-IHC promotions. Metadata IHC is
never downgraded by the model; missing scores and ambiguous reviews retain
metadata behavior. The resulting `is_hne`, `is_ihc`, and `slide_type` values
are consumed by the normal WSI importer, ClickHouse hierarchy, and frontend
filters. Tile authorization and pixel delivery remain unchanged.

The canonical association table normalizes whitespace, control characters, and
known stain aliases before emitting `stain_group` and `stain_name`. The source
values remain available as `stain_group_raw` and `stain_name_raw`; ambiguous
recuts, controls, and special stains are not silently forced into the binary
H&E/IHC classes.

Binary metadata precedence is explicit: valid manual labels win, FISH names
remain non-binary unless manually adjudicated, recognized H&E/IHC groups win
over contradictory names, and name inference is limited to blank groups. The
reviewed exact `SSL H&E` pattern is promoted to H&E; other SSL patterns remain
in the review queue. Run the `stain_metadata_audit.sql` query in the
[`pdm_databricks_pipelines` WSI bundle](https://github.com/pathology-data-mining/pdm_databricks_pipelines/tree/main/pathology_data_mining/wsi_summary)
against the source tables to inspect coverage, conflicts, and the ranked
non-binary queue before publishing a release.

For development or rehearsal, use the Databricks `dev` profile and its
warehouse, and point the WSI tables at an isolated `cdsi_dev.wsi_test`
schema. The dev materializer loads only the supplied snapshot and master
thumbnail registry; it does not generate or publish thumbnail variants. Set
`THUMBNAIL_MANIFEST_URI` to a separate object-store prefix such as
`s3://mskmind-bkt/wsi-thumbnails-dev/manifest.json`. The dev workspace does
not expose the production PHI catalogs, so load a validated study snapshot and
the offline registry with:

```bash
DATABRICKS_CONFIG_PROFILE=dev PYTHONPATH=. .venv/bin/python \
  tools/materialize_dev_wsi_snapshot.py \
  --meta-wsi /path/to/meta_wsi.txt \
  --registry-jsonl /path/to/thumbnail-results.jsonl \
  --namespace cdsi_dev.wsi_test \
  --warehouse-id a52519fa662ce69d \
  --artifact-root-uri s3://mskmind-bkt/wsi-thumbnails-dev/masters \
  --manifest-uri s3://mskmind-bkt/wsi-thumbnails-dev/manifest.json
```

This writes only the dev source, registry, canonical, and summary tables and
publishes the supplied manifest below the separate `wsi-thumbnails-dev/`
prefix. The materializer rejects registry rows whose artifact URI is not
exactly below the dev artifact root, retains failed registry rows for
diagnostics, and publishes only complete successful rows in the manifest. The
production Databricks SQL templates and bundle commands are maintained in the
[`pdm_databricks_pipelines` WSI bundle](https://github.com/pathology-data-mining/pdm_databricks_pipelines/tree/main/pathology_data_mining/wsi_summary).
The tile server no longer ships or schedules a competing production pipeline.

Beta does not require these dev tables. To prepare a beta study, run the
exporter against the production canonical association table using a read-only
Databricks identity, then import the resulting study files into the beta
ClickHouse database. The imported `THUMBNAIL_URL` values point to the already
published production S3 artifacts; beta does not connect to or write the
production Databricks tables.

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
