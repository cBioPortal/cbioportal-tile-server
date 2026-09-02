# cBioPortal WSI tile-server runbook

This service is a source-bound pixel reader. cBioPortal owns authentication,
study authorization, hierarchy, and the slide access bundle. The tile server
does not resolve image IDs, query Databricks, read a resource index, search,
or expose clinical metadata.

## Source of truth

- `../cbioportal` serves `/api/wsi/v2/hierarchy/{studyId}/{patientId}` and
  `/api/wsi/v2/slides/{studyId}/{imageId}/access` from ClickHouse.
- `../cbioportal-frontend` requests that access bundle and sends its exact
  URLs plus the returned Bearer capability to this service.
- The deployment repository owns ingress, probes, secrets, and rollout
  resources.

The backend access response is the only online input needed beyond the shared
secret. It contains `sourceUrl`, `thumbnail.sourceUrl`, dimensions, intrinsic
tile metadata, and a v2 token. The token binds both URLs by SHA-256.

## Production topology

The existing `/wsi` ingress may route to this service. Keep `/health` and
`/ready` public for orchestration; all other routes require a Bearer token.
Ingress and deployment configuration are authoritative for timeouts, network
policy, worker count, and block-cache volumes.

## Required environment

```text
AWS_ENDPOINT_URL=<S3-compatible endpoint>
AWS_ACCESS_KEY_ID=<object-store key>
AWS_SECRET_ACCESS_KEY=<object-store secret>
WSI_AUTH_SECRET=<same at-least-32-byte secret as cBioPortal>
WSI_AUTH_AUDIENCE=cbioportal-wsi
WSI_AUTH_MAX_TTL=300
WSI_ALLOWED_SOURCE_SCHEMES=s3
WSI_ALLOWED_SOURCE_PREFIXES=s3://mskmind-bkt/reef-slides/,s3://pathology/CRC_21-167/slides/,s3://pathology/CRC_21-167/crc_slides/,s3://pathology/CART_19-373/,s3://pathology/BR_20-226/slides/
WSI_ALLOWED_THUMBNAIL_PREFIXES=s3://mskmind-bkt/wsi-thumbnails/
REDIS_URL=<password-protected Redis URL>
```

The backend should use `wsi.access-token-ttl-seconds=300`. Do not set a tile
server TTL lower than the backend TTL. `WSI_AUTH_REQUIRED` is retained as a
legacy configuration key but authentication is mandatory for pixel routes.
The URI prefix allowlists are a de-identification boundary; leave them empty
only for an isolated non-publishing unit test.

## Endpoints and smoke checks

```bash
curl -fsS https://cbioportal.example.org/wsi/health
curl -fsS https://cbioportal.example.org/wsi/ready
curl -i https://cbioportal.example.org/wsi/tiles/zxy/0/0/0?source=s3%3A%2F%2Fbucket%2Fslide.svs
```

The final command must return `401` without `Authorization`. With a fresh
bundle from cBioPortal, use the returned source URL and token:

```bash
curl -fsS \
  -H "Authorization: Bearer ${WSI_TOKEN}" \
  "https://cbioportal.example.org/wsi/tiles/zxy/0/0/0?source=$(python -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$WSI_SOURCE")"
```

Also verify that changing one character of the source URL returns `403`, that
an expired token returns `401`, and that the thumbnail endpoint accepts only
the artifact URL bound in the same token.

## Data preparation

The offline association pipeline loads each WSI slide row with:

```text
source_url, tile_metadata_json, thumbnail_url,
thumbnail_width, thumbnail_height, thumbnail_content_type
```

The thumbnail generator writes the artifact and intrinsic metadata to the
thumbnail registry. The cBioPortal core importer publishes `can_serve_tiles=true` only when all
fields are complete; otherwise the hierarchy reports the slide as unavailable.
No registry or manifest is mounted into the online tile-server pod.

Inspect or resume an interrupted run from its original candidate snapshot:

```bash
tools/run_thumbnail_pipeline_slurm.sh status \
  --run-dir /gpfs/path/.slurm-thumbnail-work/20260806180612

tools/run_thumbnail_pipeline_slurm.sh resume \
  --run-dir /gpfs/path/.slurm-thumbnail-work/20260806180612 \
  --concurrency 4
```

The batch path reads the effective, fingerprint-bound S3 pointers from
`cdsi_prod.pathology_data_mining.wsi_serving_manifest`, stores publication state
in `cdsi_prod.pathology_data_mining.slide_thumbnail_registry`, and keeps its
temporary files, logs, and subprocess handoff data under the shared run
directory rather than `/tmp`. Array workers write results atomically and create
completion markers only after every candidate in a shard has a result. The
dependent publisher audits every task before performing serialized registry
updates and publishing the manifest, even when individual slides fail. An
interrupted run can resume only missing or incomplete task indexes without
changing its candidate snapshot. Block cache is disabled for offline
generation so one-time slide reads do not accumulate on GPFS. Successful
publication removes candidate, result, temporary, and block-cache directories
while retaining summaries, failure logs, and quarantined partial results.

## Production thumbnail publication

Thumbnail preparation is a separate scheduled workload, not part of the
frontend, Compose stack, or online tile-server request path. Schedule
`tools/run_thumbnail_pipeline_slurm.sh` (or an equivalent cron/workflow) to
run `tools/generate_slide_thumbnails.py` against the eligible serving manifest. The
job must:

1. read source slides from the configured S3/Dell ECS-compatible store;
2. write immutable master JPEGs to that store; and
3. upsert `cdsi_prod.pathology_data_mining.slide_thumbnail_registry` with the
   artifact URI, intrinsic `tile_metadata_json`, dimensions, and content type.

The production canonical-association and summary refresh is owned by
`../pdm_databricks_pipelines`; the Databricks bundle in this repository is
paused. Do not publish a thumbnail batch until it has completed for the input
serving manifest. The PDM serving manifest matches source URI and the
`source_fingerprint` embedded in `tile_metadata_json`; an in-place ECS rewrite
therefore forces thumbnail regeneration even when the URL is unchanged. Rows
marked successful without `tile_metadata_json` or its source fingerprint must
be regenerated before publication.

The frontend only requests `/thumbnails` and never uploads artifacts. The
tile-server `app/thumbnail_worker.py` CLI can be used for development,
rehearsal, or controlled remediation, but it writes only an object-store
artifact and does not populate the registry. It must not be used as the
production source of truth.

## Study snapshot publication

When refreshing all private study directories, run
`tools/migrate_all_studies.sh` with the same URI-prefix environment variables
used by the exporter. The script takes an external per-study lock, copies the
study into a sibling candidate directory, performs cleanup/export/resource
generation there, and only then swaps the complete candidate directory into
place. A failed validation or export removes the candidate and leaves the
previous study snapshot untouched.

## Response and cache policy

- Tiles: `private, max-age=3600`, vary on `Authorization`.
- Thumbnails: `private, max-age=300`, vary on `Authorization`.
- Redis is an optimization, never an authorization boundary.
- Overview decodes that exceed `MAX_DECODE_PIXELS` return HTTP 422 with
  `overview_requires_preprocessing`.

Application logs must not include tokens, source URLs, patient IDs, or slide
IDs. Keep operation type, dimensions, status, timing, and exception class.

## Local integration

The local cBioPortal compose rehearsal should pass the same
`WSI_AUTH_SECRET`/audience to the backend and tile server. For mounted local
slides, explicitly set `WSI_ALLOWED_SOURCE_SCHEMES=s3,file` and include
`file:///app/testdata/` in both URI prefix allowlists; production should remain
`s3` only. Generate a v2 access bundle through the backend before
testing a pixel request.

## Thumbnail batch operations

Run `tools/generate_slide_thumbnails.py` (usually through the Slurm wrapper)
outside the API process and on a schedule. It writes immutable artifacts and
registry rows; a successful batch must be followed by the Databricks canonical
refresh, study-file export, and cBioPortal core study import before a slide
becomes servable.
