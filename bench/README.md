# Repeatable tile benchmark

`tile_bench.py` is the standard HTTP benchmark for comparing direct tile-server access with nginx or Traefik and for comparing cold versus warm cache behavior.

The fixed workload is approximately:

- 90% ZXY tile requests, biased toward high zoom levels
- 5% metadata requests
- 5% 256×256 thumbnail requests

The request sequence is deterministic for `--seed`. Use the same slide IDs, study ID, request count, concurrency, seed, and timeout when comparing runs. `--warmup` requests are excluded from measurements.

When `--max-zoom` and `--tile-grid` are omitted, the harness fetches each slide's metadata and generates in-bounds tile coordinates. Those options remain available as fallbacks for servers that do not expose metadata to the benchmark token. Results report both attempted and successful throughput, plus failure rate and latency by status.

Example:

```bash
python bench/tile_bench.py \
  --base-url http://localhost:3001/wsi \
  --study-id coad_msk_2025 \
  --slide-id 2908638 --slide-id 4186363 \
  --bearer-token "$WSI_BENCH_TOKEN" \
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
  -JSTUDY_ID=coad_msk_2025 -JSLIDE_IDS=2908638,4186363 \
  -JBEARER_TOKEN="$WSI_BENCH_TOKEN" -JMAX_ZOOM=9 -JTILE_GRID=8 \
  -Jusers=20 -Jramp_seconds=20 -Jduration_seconds=60 \
  -l results/jmeter-warm.jtl -e -o results/jmeter-warm-report
```

The plan uses the same 90/5/5 tile/metadata/thumbnail mix. Keep the Python harness as the deterministic regression check; use JMeter for sustained or distributed capacity tests.

Record at least p50, p95, p99, throughput, HTTP status counts, topology (`direct`, `nginx`, or `traefik`), image tag, worker count, `MAX_OPEN_SLIDES`, and `BLOCKCACHE_PATH` in the result artifact.
