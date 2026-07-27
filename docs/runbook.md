# cBioPortal WSI tile-server runbook

This repository contains the standalone WSI tile service. It serves pathology
slides and metadata to cBioPortal; it is not the cBioPortal frontend or the
Spring backend. The Kubernetes deployment is currently named `slide-viewer`
for compatibility, although the service implementation lives here.

## Source of truth

Use these repositories together when changing WSI:

- `../cbioportal-frontend` obtains a study-scoped capability from
  `/api/wsi/access-token?studyId=<study>` and sends it on WSI requests.
- `../cbioportal` authenticates the user, checks study-read permission, and
  issues the capability.
- `../knowledgesystems-k8s-deployment` owns the production Kubernetes
  deployment, ingress, secrets wiring, and smoke test.
- `../cbioportal-docker-compose` provides the local nginx rehearsal.

The production manifests are under:

```text
../knowledgesystems-k8s-deployment/argocd/aws/666628074417/clusters/cbioportal-prod/apps/slide-viewer/
```

Do not edit those manifests from this repository, and preserve any unrelated
working-tree changes in that deployment repository.

## Production endpoint and topology

The current ingress is path-based:

```text
https://cbioportal.mskcc.org/wsi/...
```

It routes the `/wsi` prefix to the Kubernetes `slide-viewer` Service on port
80, which forwards to container port 8080. The ingress has 300-second proxy
timeouts, buffering disabled, a 1 MiB request limit, 100 requests/second per
source limit with burst multiplier 5, and 50 concurrent connections per
source.

There is no checked-in `slides.cbioportal.org` DNS record or ingress rule.
Do not create or document that CNAME unless the infrastructure/DNS owner
explicitly introduces it. The existing production route does not require a
new CNAME.

The current deployment is one replica on `workload-class: x86-general`, with
3 GiB memory requested, 4 GiB limited, and a 20 GiB `emptyDir` block cache.
Readiness and liveness both use `/health`. The NetworkPolicy in the deployment
repository permits ingress only from the `ingress-nginx` namespace.

The deployed image is currently named `cbioportal/cbioportal-slide-viewer`
with a CI/CD-managed tag. Keep that legacy image/release name aligned with the
deployment repository; do not silently rename it when publishing this service.

## Production configuration

The current ConfigMap sets:

```text
AWS_ENDPOINT_URL=http://pmindecs.mskcc.org:9020
DATABRICKS_WAREHOUSE_ID=0b49b7d78734ad5c
KEYCLOAK_JWKS_URL=<MSK Keycloak JWKS endpoint>
ANNOTATION_AUTH_ENABLED=true
TILE_CACHE_TTL=86400
THUMBNAIL_CACHE_TTL=86400
METADATA_CACHE_TTL=86400
MAX_DECODE_PIXELS=4194304
BLOCKCACHE_PATH=/cache/slide-blocks
BLOCKCACHE_BLOCK_SIZE=8388608
MAX_OPEN_SLIDES=1
MAX_IMAGE_OPERATIONS=2
N_WORKERS=2
TILE_SIZE=256
JPEG_QUALITY=85
CORS_ORIGINS=https://cbioportal.mskcc.org,https://triage.cbioportal.mskcc.org
```

Credentials are supplied by the `slide-viewer-secrets` Secret:
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `DATABRICKS_HOST`,
`DATABRICKS_TOKEN`, `REDIS_URL`, and `ANNOTATION_DATABASE_URL`. The tile
server receives `WSI_AUTH_SECRET` from the `cbioportal-msk-blue` Secret.
The blue and green cBioPortal backend deployments are configured with the
same secret and with audience `cbioportal-wsi` and a 300-second token TTL.
`WSI_AUTH_REQUIRED` is `true` in production.

The production values above are the memory-bound starting point for a 4 GiB
pod. Increase `N_WORKERS`, `MAX_OPEN_SLIDES`, or `MAX_IMAGE_OPERATIONS` only
after measuring RSS under representative slide load.

Use a password-protected Redis URL in shared environments. Redis is a cache,
not an authorization boundary. Keep it private and do not place WSI metadata
or image responses behind a public/shared cache.

## Authentication contract

The browser obtains a capability from the cBioPortal backend:

```text
GET /api/wsi/access-token?studyId=coad_msk_2025
```

The backend requires an authenticated user with read access to the requested
study. Anonymous requests return `401`; a user without study access receives
`403`. The token contains `sub`, `aud=cbioportal-wsi`, `scope=wsi:read`,
`study_id`, `iat`, and `exp`.

The frontend caches tokens per study and sends:

```text
Authorization: Bearer <token>
```

The tile server rejects WSI tokens whose lifetime exceeds 300 seconds and
applies an in-process limit of 120 expensive requests per client per minute.
Production ingress should apply distributed rate limits as well.

The tile server validates the signature, algorithm, audience, scope, subject,
issued-at time, and expiry. It does not currently validate the `study_id`
claim against a slide-to-study mapping. Therefore backend-issued,
study-scoped tokens do not yet prove study-level isolation at the tile-server
boundary. Do not describe this as complete until that mapping check is
implemented and tested.

`/health` is public for Kubernetes probes. All other routes require a valid
Bearer token when `WSI_AUTH_REQUIRED=true`.

## Local integration

The supported local rehearsal uses:

| Component | Address | Responsibility |
|---|---|---|
| Frontend dev server | `http://localhost:3000` | Browser UI |
| cBioPortal backend | `http://localhost:8090` | Login, authorization, token issuance |
| Tile server | `http://localhost:8081` | Direct WSI API |
| WSI nginx | `http://localhost:3001` | Same-origin browser entrypoint |

From `../cbioportal-docker-compose`:

```bash
docker compose \
  -f docker-compose.yml \
  -f addon/slide-viewer/docker-compose.slide-viewer.yml \
  -f addon/wsi-nginx/docker-compose.wsi-nginx.yml \
  up -d wsi-nginx
```

The compose slide-viewer overlay is a local rehearsal and contains legacy
defaults. Verify authentication, secret names, Redis protection, and runtime
environment before treating it as production-equivalent.

## Health and smoke checks

Local health checks:

```bash
curl -fsS http://localhost:8081/health
curl -fsS http://localhost:3001/wsi/health
```

For production, use the deployment repository's smoke test. It requires a
short-lived capability and explicit paths:

```bash
cd ../knowledgesystems-k8s-deployment
export CBIOPORTAL_URL=https://cbioportal.mskcc.org
export WSI_PATIENT_PATH=/wsi/patient/<patient-id>
export WSI_TILE_PATH=/wsi/tiles/<slide-id>/zxy/4/0/0
export WSI_BEARER_TOKEN='<short-lived-token>'
tests/smoke/slide-viewer-routing.sh
```

The test first requires unauthenticated patient and tile requests to return
`401` or `403`, then verifies both routes succeed with the Bearer token.
Also verify anonymous token requests return `401`, unauthorized studies
return `403`, tokens are cached per study, and metadata responses are not
publicly cacheable.

## Cache and response policy

- Tiles: `private, max-age=3600`.
- Thumbnails: `private, max-age=300`.
- Patient hierarchy, slide metadata, and search: `private, no-store`.
- Thumbnails and metadata now use 24-hour Redis TTLs by default.
- Avoid `TILE_CACHE_TTL=0` in production unless Redis capacity has been sized
  explicitly for the resulting working set.

## Overview decode guard

Thumbnail and overview-tile requests are memory-bounded. If a slide lacks a
safe overview pyramid level, the server returns HTTP `422` with
`{"error":"overview_requires_preprocessing"}` and logs the selected pyramid
level, requested decode dimensions, requested pixel count, and decode limit.
Prepare those slides offline by generating a proper low-resolution pyramid or
a precomputed thumbnail before exposing them to the service.

## ETL and study operations

The nightly Databricks Asset Bundle is defined in `databricks.yml` and runs:

1. `tools/wsi_canonical_associations_pipeline.sql`
2. `tools/wsi_summary_pipeline.sql`

The bundle output is loaded into the cBioPortal ClickHouse
`wsi_patient_hierarchy` table. The tile server does not load or cache the
patient hierarchy; it serves slide paths, metadata, and image tiles only.
The loader accepts one materialized hierarchy per JSONL line and publishes the
manifest only after all rows are inserted:

```bash
python3 tools/load_clickhouse_hierarchy.py hierarchy.jsonl --version 20260723030000
```

Preview migrations before writing:

```bash
bash tools/migrate_all_studies.sh --dry-run
```

For a real study update, review generated files in the private dataset
repository before opening the study-data PR. Do not remove legacy resource
files until replacement files and the reload plan are approved.

## Validation and ownership

Run tile-server tests with:

```bash
python3 -m pytest -q
```

Frontend WSI tests and local end-to-end study-access tests are defined in
`../cbioportal-frontend`. Production rollout, image tags, Kubernetes changes,
DNS/TLS, secret rotation, ingress policy, observability, rollback, and the
study-to-slide mapping check are owned outside this repository. Update this
runbook when those sources of truth change.
