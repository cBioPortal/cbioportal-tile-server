# Thumbnail Artifact Serving Plan

## Objective

Replace live thumbnail generation with a predictable artifact-serving path that
pre-renders thumbnails for every servable slide and exposes them through a
dedicated route.

## Final design

- Runtime route: `GET /thumbnails/{slide_id}`
- Source of truth: a published manifest in object storage
- Artifact format: one JPEG master per slide
- Master size: max edge length `1024` pixels
- Inventory scope: every inventory row whose `path` is servable (`s3://...`)
- Request policy: downsize masters for smaller requests, never upscale for
  larger requests
- Fallback policy: generate a missing artifact in a capped, process-isolated
  worker and return a placeholder JPEG only when generation fails or times out

## Why this replaces the earlier hardening pass

The earlier bounded-decode plan reduced risk, but it still left thumbnail
requests tied to slide opens, overview decodes, and request-time storage
latency. That is the wrong runtime profile for a high-fanout navigator image.

Serving pre-rendered artifacts is simpler operationally:

- normal thumbnail requests no longer open WSI files; missing artifacts use a
  short-lived, capped worker instead
- thumbnail latency is bounded by object-store fetch plus optional resize
- slides that need preprocessing are handled offline once, not per request
- the API contract is explicit instead of relying on hidden fallback behavior

## Implementation steps

### 1. Route replacement

- Remove the legacy `GET /tiles/{slide_id}/thumbnail` route.
- Add `GET /thumbnails/{slide_id}`.
- Keep `width` and `height` query parameters.
- Keep auth requirements aligned with the rest of the WSI API.

### 2. Artifact manifest

Publish a JSON manifest shaped like:

```json
{
  "version": 1,
  "generated_at": "2026-08-03T00:00:00+00:00",
  "master_size": 1024,
  "slides": {
    "1492807": {
      "uri": "s3://bucket/wsi-thumbnails/masters/1492807.jpg",
      "width": 1024,
      "height": 768,
      "content_type": "image/jpeg"
    }
  }
}
```

Runtime should cache this manifest in-process and refresh it on a short
interval.

### 3. Offline generation

Add a batch generator that:

1. queries all servable inventory rows
2. opens each source slide offline
3. renders a single `1024`-pixel JPEG master
4. uploads that JPEG to object storage
5. writes the manifest after the batch completes

Failures should be recorded separately so the manifest contains only valid,
published artifacts.

### 4. Runtime serving rules

For `GET /thumbnails/{slide_id}`:

1. look up the slide in the manifest
2. if missing, generate and persist the master in a capped, process-isolated
   worker; return a placeholder JPEG with status headers only if generation
   fails or times out
3. fetch the master JPEG
4. if the request is smaller than the master, downsize and return JPEG
5. if the request is larger than the master, return the master unchanged

Recommended headers:

- `X-Thumbnail-Status: ok|placeholder`
- `X-Thumbnail-Reason: master|resized|missing|unavailable`

### 5. Keep existing tile hardening

This change does not relax the overview guard on `/tiles/{slide_id}/zxy/...`.
Unsafe overview decodes should still fail with
`{"error":"overview_requires_preprocessing"}` until those slides are fixed
offline.

## Test plan

### Unit tests

1. manifest record loads correctly
2. smaller requests downsize the master
3. larger requests do not upscale the master

### API tests

1. `GET /thumbnails/{slide_id}` returns JPEG plus status headers
2. missing manifest entry returns placeholder JPEG
3. legacy `/tiles/{slide_id}/thumbnail` path is absent

## Success criteria

The rollout is successful when:

- thumbnail requests normally serve artifacts, while misses use only capped,
  process-isolated WSI decoding
- every servable slide has a published thumbnail artifact
- the new route is the only thumbnail route
- request-time thumbnail failures are limited to missing artifacts or storage
  errors rather than pathological slide behavior
