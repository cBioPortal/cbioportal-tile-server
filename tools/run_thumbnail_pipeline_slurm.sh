#!/usr/bin/env bash
set -euo pipefail

WORKDIR="/gpfs/mskmind_ess/limr/repos/cbioportal-tile-server"
SHARED_ROOT="${SLURM_SHARED_DIR:-${WORKDIR}/.slurm-thumbnail-work}"
LOG_DIR=""

configure_paths() {
  local run_dir="${SLURM_SHARED_RUN_DIR:-${THUMBNAIL_RUN_DIR:-${SHARED_ROOT}/local}}"
  LOG_DIR="${SLURM_LOG_DIR:-${run_dir}/logs}"
  export THUMBNAIL_TMPDIR="${run_dir}/tmp/job-${SLURM_JOB_ID:-$$}-task-${SLURM_ARRAY_TASK_ID:-0}"
  export TMPDIR="${THUMBNAIL_TMPDIR}"
  export TEMP="${THUMBNAIL_TMPDIR}"
  export TMP="${THUMBNAIL_TMPDIR}"
  export BLOCKCACHE_PATH="${run_dir}/blockcache/job-${SLURM_JOB_ID:-$$}-task-${SLURM_ARRAY_TASK_ID:-0}"
}

usage() {
  cat >&2 <<'EOF'
usage:
  tools/run_thumbnail_pipeline_slurm.sh submit <manifest-uri> <root-uri> [task-count] [concurrency]
  tools/run_thumbnail_pipeline_slurm.sh worker
  tools/run_thumbnail_pipeline_slurm.sh publish
EOF
  exit 2
}

source_env() {
  configure_paths
  cd "$WORKDIR"
  mkdir -p "$LOG_DIR"
  mkdir -p "$THUMBNAIL_TMPDIR"
  mkdir -p "$BLOCKCACHE_PATH"
  set -a
  source .env >/dev/null 2>&1
  set +a
  export PYTHONUNBUFFERED=1
}

prepare_candidates() {
  local manifest_uri="$1"
  local root_uri="$2"
  local requested_tasks="$3"
  local run_id
  run_id="$(date +%Y%m%d%H%M%S)"
  local run_dir="${SHARED_ROOT}/${run_id}"
  mkdir -p "$run_dir"
  local candidate_file="${run_dir}/candidates.jsonl"
  local meta_file="${run_dir}/run-meta.json"

  export THUMBNAIL_RUN_DIR="$run_dir"
  source_env
  uv run python - <<'PY' "$candidate_file" "$meta_file" "$manifest_uri" "$root_uri" "$requested_tasks"
import json
import sys
from datetime import UTC, datetime

from app.config import settings
from tools.generate_slide_thumbnails import _ensure_registry_table
from tools.generate_slide_thumbnails import discover_candidate_rows
from tools.generate_slide_thumbnails import write_candidate_rows

candidate_file, meta_file, manifest_uri, root_uri, requested_tasks = sys.argv[1:]
requested_tasks = max(1, int(requested_tasks))
warehouse_id = settings.databricks_warehouse_id
_ensure_registry_table(warehouse_id)
candidates = discover_candidate_rows(
    warehouse_id=warehouse_id,
    retry_failures_only=False,
)
write_candidate_rows(candidate_file, candidates)
task_count = min(requested_tasks, max(1, len(candidates))) if candidates else 0
manifest_version = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
payload = {
    "candidate_count": len(candidates),
    "task_count": task_count,
    "manifest_uri": manifest_uri,
    "root_uri": root_uri,
    "master_size": settings.thumbnail_master_size,
    "warehouse_id": warehouse_id,
    "manifest_version": manifest_version,
}
with open(meta_file, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
print(json.dumps(payload))
PY
  echo "run_dir=${run_dir}"
}

submit_array() {
  if [[ $# -lt 2 ]]; then
    usage
  fi
  local manifest_uri="$1"
  local root_uri="$2"
  local task_count="${3:-32}"
  local concurrency="${4:-4}"

  mkdir -p "$SHARED_ROOT"
  local prep_output
  prep_output="$(prepare_candidates "$manifest_uri" "$root_uri" "$task_count")"
  echo "$prep_output"

  local run_dir
  run_dir="$(printf '%s\n' "$prep_output" | awk -F= '/^run_dir=/{print $2}' | tail -1)"
  [[ -n "$run_dir" ]] || { echo "failed to determine run_dir" >&2; exit 1; }
  local log_dir="${run_dir}/logs"
  mkdir -p "$log_dir"
  local meta_file="${run_dir}/run-meta.json"
  local candidate_count
  candidate_count="$(python - <<'PY' "$meta_file"
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
print(int(payload["candidate_count"]))
PY
)"
  if [[ "$candidate_count" -eq 0 ]]; then
    echo "no thumbnail candidates discovered"
    exit 0
  fi

  local actual_tasks
  actual_tasks="$(python - <<'PY' "$meta_file"
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
print(int(payload["task_count"]))
PY
)"
  local worker_job
  worker_job="$(
    sbatch --parsable \
      --job-name=slide-thumbnails \
      --partition=hpc \
      --cpus-per-task=2 \
      --mem=8G \
      --time=08:00:00 \
      --array="0-$((actual_tasks - 1))%${concurrency}" \
      --export=ALL,SLURM_SHARED_RUN_DIR="$run_dir",SLURM_LOG_DIR="$log_dir" \
      --output="${log_dir}/slide-thumbnails-%A_%a.out" \
      --error="${log_dir}/slide-thumbnails-%A_%a.err" \
      "$0" worker
  )"
  local publish_job
  publish_job="$(
    sbatch --parsable \
      --job-name=slide-thumbnails-publish \
      --partition=hpc \
      --cpus-per-task=1 \
      --mem=4G \
      --time=01:00:00 \
      --dependency="afterok:${worker_job}" \
      --export=ALL,SLURM_SHARED_RUN_DIR="$run_dir",SLURM_LOG_DIR="$log_dir" \
      --output="${log_dir}/slide-thumbnails-publish-%j.out" \
      --error="${log_dir}/slide-thumbnails-publish-%j.err" \
      "$0" publish
  )"
  echo "worker_job=${worker_job}"
  echo "publish_job=${publish_job}"
  echo "run_dir=${run_dir}"
}

worker_mode() {
  [[ -n "${SLURM_SHARED_RUN_DIR:-}" ]] || { echo "SLURM_SHARED_RUN_DIR is required" >&2; exit 2; }
  source_env
  local meta_file="${SLURM_SHARED_RUN_DIR}/run-meta.json"
  local candidate_file="${SLURM_SHARED_RUN_DIR}/candidates.jsonl"
  local summary_path="${LOG_DIR}/slide-thumbnail-summary-${SLURM_ARRAY_JOB_ID}-${SLURM_ARRAY_TASK_ID}.json"
  local failures_path="${LOG_DIR}/slide-thumbnail-failures-${SLURM_ARRAY_JOB_ID}-${SLURM_ARRAY_TASK_ID}.json"

  uv run python - <<'PY' "$meta_file" "$candidate_file" "$summary_path" "$failures_path" "${SLURM_ARRAY_TASK_ID}" "${SLURM_ARRAY_TASK_COUNT}"
import json
import sys

from tools.generate_slide_thumbnails import _slice_candidate_rows
from tools.generate_slide_thumbnails import _summary_payload
from tools.generate_slide_thumbnails import process_candidate_rows
from tools.generate_slide_thumbnails import read_candidate_rows

meta_file, candidate_file, summary_path, failures_path, task_index, task_count = sys.argv[1:]
task_index = int(task_index)
task_count = int(task_count)
with open(meta_file, "r", encoding="utf-8") as handle:
    meta = json.load(handle)
rows = read_candidate_rows(candidate_file)
shard_rows = _slice_candidate_rows(rows, task_index=task_index, task_count=task_count)
failures = process_candidate_rows(
    warehouse_id=meta["warehouse_id"],
    root_uri=meta["root_uri"],
    master_size=int(meta["master_size"]),
    rows=shard_rows,
    manifest_version=meta["manifest_version"],
)
summary = _summary_payload(
    manifest={
        "generated_at": meta["manifest_version"],
        "manifest_version": meta["manifest_version"],
        "slides": {},
    },
    failures=failures,
    candidates=shard_rows,
)
with open(summary_path, "w", encoding="utf-8") as handle:
    json.dump(summary, handle, indent=2, sort_keys=True)
with open(failures_path, "w", encoding="utf-8") as handle:
    json.dump(failures, handle, indent=2, sort_keys=True)
print(json.dumps(summary))
PY
}

publish_mode() {
  [[ -n "${SLURM_SHARED_RUN_DIR:-}" ]] || { echo "SLURM_SHARED_RUN_DIR is required" >&2; exit 2; }
  source_env
  local meta_file="${SLURM_SHARED_RUN_DIR}/run-meta.json"
  local publish_summary="${LOG_DIR}/slide-thumbnail-publish-summary-${SLURM_JOB_ID}.json"

  uv run python - <<'PY' "$meta_file" "$publish_summary"
import json
import sys

from tools.generate_slide_thumbnails import publish_manifest_for_current_inventory

meta_file, publish_summary = sys.argv[1:]
with open(meta_file, "r", encoding="utf-8") as handle:
    meta = json.load(handle)
manifest = publish_manifest_for_current_inventory(
    warehouse_id=meta["warehouse_id"],
    manifest_uri=meta["manifest_uri"],
    master_size=int(meta["master_size"]),
    manifest_version=meta["manifest_version"],
)
summary = {
    "manifest_uri": meta["manifest_uri"],
    "manifest_version": meta["manifest_version"],
    "manifest_slide_count": len(manifest["slides"]),
}
with open(publish_summary, "w", encoding="utf-8") as handle:
    json.dump(summary, handle, indent=2, sort_keys=True)
print(json.dumps(summary))
PY
}

main() {
  local mode="${1:-}"
  case "$mode" in
    submit)
      shift
      submit_array "$@"
      ;;
    worker)
      worker_mode
      ;;
    publish)
      publish_mode
      ;;
    *)
      usage
      ;;
  esac
}

main "$@"
