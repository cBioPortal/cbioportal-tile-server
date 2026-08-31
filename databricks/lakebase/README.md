# Lakebase provisioning for annotation storage

This bundle provisions the shared Lakebase resources that should back
annotation CRUD for the tile server:

- one Lakebase Autoscaling project
- one long-lived development branch
- one read-write endpoint for that branch

It is intentionally separate from the root `databricks.yml` bundle, which
manages the nightly WSI summary job. Annotation OLTP infrastructure should stay
isolated from the batch SQL job definition.

## Why this shape

For this feature, a shared development project is the right default:

- it avoids adding another stateful service to the Kubernetes stack
- it keeps annotation storage on standard Postgres semantics
- it matches Lakebase's project/branch/endpoint model
- it leaves room for short-lived feature branches later without provisioning a
  new server per PR

## What this bundle creates

The bundle declares:

- `postgres_projects.annotations`
- `postgres_branches.annotations_branch`
- `postgres_endpoints.annotations_rw`

It does **not** create a Postgres role, because the current bundle schema does
not expose a `postgres_roles` resource. Create the application role as a
follow-up CLI step after the project exists.

## Prerequisites

- Databricks CLI installed and authenticated
- a workspace profile with Lakebase permissions, for example `dev`
- a Databricks service principal that the backend will use

## Validate

From this directory:

```bash
databricks bundle validate \
  --profile dev \
  --target dev \
  --var="service_principal_name=<service-principal-name>"
```

## Deploy

```bash
databricks bundle deploy \
  --profile dev \
  --target dev \
  --var="service_principal_name=<service-principal-name>"
```

If you need a different project or branch name:

```bash
databricks bundle deploy \
  --profile dev \
  --target dev \
  --var="service_principal_name=<service-principal-name>" \
  --var="project_id=cbioportal-annotations-dev" \
  --var="project_display_name=cBioPortal Annotations Dev" \
  --var="branch_id=dev" \
  --var="endpoint_id=primary"
```

## Post-deploy role creation

Create a database role for the backend identity on the deployed branch:

```bash
databricks postgres create-role \
  "projects/<project-id>/branches/<branch-id>" \
  --profile dev \
  --role-id cbioportal-api
```

Then connect to the branch database and grant only the schema/table privileges
the annotation service needs.

## Operational notes

- Keep `enable_pg_native_login: false` unless you explicitly need password
  authentication.
- Treat this as shared dev infrastructure. Do not provision one project per PR.
- If a PR needs isolated data, create a short-lived branch from the shared
  project instead of another standalone server.
