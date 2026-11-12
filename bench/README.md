# Repeatable tile benchmark

`tile_bench.py` is the standard HTTP benchmark for comparing direct tile-server access with nginx or Traefik and for comparing cold versus warm cache behavior.

Each request names a slide the way the frontend does:
`?studyId=<study>&key=<slide key>&enc=<sealed source>`. Describe each slide in
an access JSON file (the same shape `single_user_pan_bench.py` reads):

```json
{"studyId": "...", "key": "...", "enc": "...", "tileMetadata": {"max_zoom": 9, "tile_size": 256, "dimensions": {"width": 40000, "height": 30000}}}
```

Through the ingress, export the portal session cookie as `WSI_BENCH_COOKIE`
(for example `SESSION=...`) so the ingress authorizes the study. `enc` is a
bearer value for its slide: keep access files out of the repository, and
neither the harness nor its output records `enc` or the cookie.

The fixed workload is approximately:

- 95% ZXY tile requests, biased toward high zoom levels and inside each slide's bounds
- 5% 256×256 thumbnail requests

The request sequence is deterministic for `--seed`. Use the same access files, request count, concurrency, seed, and timeout when comparing runs. `--warmup` requests are excluded from measurements. Results report both attempted and successful throughput, plus failure rate and latency by status.

Example:

```bash
WSI_BENCH_COOKIE='SESSION=...' python bench/tile_bench.py \
  --base-url https://cbioportal.example.org/wsi \
  --access-json /secure/path/slide-a.json --access-json /secure/path/slide-b.json \
  --requests 1000 --concurrency 20 --warmup 100 \
  --cache-mode warm \
  --json-out results/nginx-warm.json
```

For a cold run, restart the tile-server workers and clear the Redis tile cache before invoking the command. For a warm run, invoke it again without restarting. The benchmark does not clear or alter server caches itself.

## JMeter harness

For distributed load generation or HTML reports, use the equivalent headless JMeter plan:

```bash
jmeter -n -t bench/tile-benchmark.jmx \
  -JBASE_URL=http://pllimsksparky3:3001/wsi \
  -JSTUDY_ID=msk_spectrum_tme_2022 -JKEY="$SLIDE_KEY" -JENC="$ENC" \
  -JCOOKIE="SESSION=$SESSION" -JMAX_ZOOM=9 -JTILE_GRID=8 \
  -Jusers=20 -Jramp_seconds=20 -Jduration_seconds=60 \
  -l results/jmeter-warm.jtl -e -o results/jmeter-warm-report
```

The plan drives one slide with the same 95/5 tile/thumbnail mix. Keep the Python harness as the deterministic regression check; use JMeter for sustained or distributed capacity tests.

Record at least p50, p95, p99, throughput, HTTP status counts, topology (`direct`, `nginx`, or `traefik`), image tag, worker count, `MAX_OPEN_SLIDES`, and `BLOCKCACHE_PATH` in the result artifact.
