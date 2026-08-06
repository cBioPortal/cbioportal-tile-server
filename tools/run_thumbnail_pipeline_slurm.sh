#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="${SCRIPT_DIR}/$(basename -- "${BASH_SOURCE[0]}")"
DEFAULT_WORKDIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
WORKDIR="${THUMBNAIL_WORKDIR:-${DEFAULT_WORKDIR}}"
SHARED_ROOT="${SLURM_SHARED_DIR:-${WORKDIR}/.slurm-thumbnail-work}"
PYTHON_BIN="${THUMBNAIL_PYTHON:-${WORKDIR}/.venv/bin/python}"
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
  tools/run_thumbnail_pipeline_slurm.sh submit --manifest-uri URI --root-uri URI [options]
  tools/run_thumbnail_pipeline_slurm.sh retry --manifest-uri URI --root-uri URI [options]
  tools/run_thumbnail_pipeline_slurm.sh worker
  tools/run_thumbnail_pipeline_slurm.sh publish

options:
  --slides-per-task N  default: 2000
  --concurrency N      default: 2
  --limit N            optional candidate limit for a canary run
EOF
  exit 2
}

source_env() {
  cd "$WORKDIR"
  set -a
  source .env >/dev/null 2>&1
  set +a
  if [[ -n "${SSL_CERT_FILE:-}" && ! -r "$SSL_CERT_FILE" ]]; then
    unset SSL_CERT_FILE
  fi
  PYTHON_BIN="${THUMBNAIL_PYTHON:-${WORKDIR}/.venv/bin/python}"
  [[ -x "$PYTHON_BIN" ]] || { echo "Python executable not found: $PYTHON_BIN" >&2; return 1; }
  configure_paths
  mkdir -p "$LOG_DIR" "$THUMBNAIL_TMPDIR" "$BLOCKCACHE_PATH"
  export PYTHONUNBUFFERED=1
}

parse_submit_options() {
  SUBMIT_MANIFEST_URI=""
  SUBMIT_ROOT_URI=""
  SUBMIT_SLIDES_PER_TASK=2000
  SUBMIT_CONCURRENCY=2
  SUBMIT_LIMIT=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --manifest-uri)
        [[ $# -ge 2 ]] || usage
        SUBMIT_MANIFEST_URI="$2"
        shift 2
        ;;
      --root-uri)
        [[ $# -ge 2 ]] || usage
        SUBMIT_ROOT_URI="$2"
        shift 2
        ;;
      --slides-per-task)
        [[ $# -ge 2 ]] || usage
        SUBMIT_SLIDES_PER_TASK="$2"
        shift 2
        ;;
      --concurrency)
        [[ $# -ge 2 ]] || usage
        SUBMIT_CONCURRENCY="$2"
        shift 2
        ;;
      --limit)
        [[ $# -ge 2 ]] || usage
        SUBMIT_LIMIT="$2"
        shift 2
        ;;
      *)
        usage
        ;;
    esac
  done

  [[ -n "$SUBMIT_MANIFEST_URI" && -n "$SUBMIT_ROOT_URI" ]] || usage
  [[ "$SUBMIT_SLIDES_PER_TASK" =~ ^[1-9][0-9]*$ ]] || usage
  [[ "$SUBMIT_CONCURRENCY" =~ ^[1-9][0-9]*$ ]] || usage
  if [[ -n "$SUBMIT_LIMIT" ]]; then
    [[ "$SUBMIT_LIMIT" =~ ^[1-9][0-9]*$ ]] || usage
  fi
}

prepare_candidates() {
  local mode="$1"
  local manifest_uri="$2"
  local root_uri="$3"
  local slides_per_task="$4"
  local limit="$5"
  local run_id
  run_id="$(date +%Y%m%d%H%M%S)"
  local run_dir="${SHARED_ROOT}/${run_id}"
  mkdir -p "$run_dir"
  local candidate_dir="${run_dir}/candidates"
  local meta_file="${run_dir}/run-meta.json"

  export THUMBNAIL_RUN_DIR="$run_dir"
  source_env
  "$PYTHON_BIN" - <<'PY' "$candidate_dir" "$meta_file" "$manifest_uri" "$root_uri" "$slides_per_task" "$limit" "$mode"
import json
import sys
from datetime import UTC, datetime

from app.config import settings
from tools.generate_slide_thumbnails import MAX_ARRAY_TASKS
from tools.generate_slide_thumbnails import _ensure_registry_table
from tools.generate_slide_thumbnails import discover_candidate_rows
from tools.generate_slide_thumbnails import write_candidate_shards

candidate_dir, meta_file, manifest_uri, root_uri, slides_per_task, limit, mode = sys.argv[1:]
retry_failures_only = mode == "retry"
_ensure_registry_table(settings.databricks_warehouse_id)
candidates = discover_candidate_rows(
    warehouse_id=settings.databricks_warehouse_id,
    retry_failures_only=retry_failures_only,
    limit=int(limit) if limit else None,
)
task_count = write_candidate_shards(
    candidate_dir,
    candidates,
    slides_per_task=int(slides_per_task),
    max_tasks=MAX_ARRAY_TASKS,
)
manifest_version = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
payload = {
    "candidate_count": len(candidates),
    "task_count": task_count,
    "candidate_dir": candidate_dir,
    "manifest_uri": manifest_uri,
    "root_uri": root_uri,
    "master_size": settings.thumbnail_master_size,
    "batch_timeout_sec": settings.thumbnail_batch_timeout_sec,
    "warehouse_id": settings.databricks_warehouse_id,
    "manifest_version": manifest_version,
    "mode": mode,
}
with open(meta_file, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
print(json.dumps(payload, sort_keys=True))
PY
  echo "run_dir=${run_dir}"
}

submit_array() {
  local mode="$1"
  shift
  parse_submit_options "$@"
  mkdir -p "$SHARED_ROOT"

  local prep_output
  prep_output="$(prepare_candidates "$mode" "$SUBMIT_MANIFEST_URI" "$SUBMIT_ROOT_URI" "$SUBMIT_SLIDES_PER_TASK" "${SUBMIT_LIMIT:-}")"
  echo "$prep_output"

  local run_dir
  run_dir="$(printf '%s\n' "$prep_output" | awk -F= '/^run_dir=/{print $2}' | tail -1)"
  [[ -n "$run_dir" ]] || { echo "failed to determine run_dir" >&2; exit 1; }
  local log_dir="${run_dir}/logs"
  local meta_file="${run_dir}/run-meta.json"
  local candidate_count
  candidate_count="$($PYTHON_BIN -c 'import json,sys; print(json.load(open(sys.argv[1]))["candidate_count"])' "$meta_file")"
  if [[ "$candidate_count" -eq 0 ]]; then
    echo "no thumbnail candidates discovered; publishing the current registry manifest"
    export SLURM_SHARED_RUN_DIR="$run_dir"
    export SLURM_LOG_DIR="$log_dir"
    publish_mode
    return 0
  fi

  local actual_tasks
  actual_tasks="$($PYTHON_BIN -c 'import json,sys; print(json.load(open(sys.argv[1]))["task_count"])' "$meta_file")"
  local worker_job
  worker_job="$({
    sbatch --parsable \
      --job-name=slide-thumbnails \
      --partition=hpc \
      --cpus-per-task=2 \
      --mem=8G \
      --time=24:00:00 \
      --array="0-$((actual_tasks - 1))%${SUBMIT_CONCURRENCY}" \
      --export=ALL,SLURM_SHARED_RUN_DIR="$run_dir",SLURM_LOG_DIR="$log_dir" \
      --output="${log_dir}/slide-thumbnails-%A_%a.out" \
      --error="${log_dir}/slide-thumbnails-%A_%a.err" \
      --wrap="exec bash '$SCRIPT_PATH' worker"
  })"
  local publish_job
  if ! publish_job="$({
    sbatch --parsable \
      --job-name=slide-thumbnails-publish \
      --partition=hpc \
      --cpus-per-task=1 \
      --mem=4G \
      --time=08:00:00 \
      --dependency="afterany:${worker_job}" \
      --export=ALL,SLURM_SHARED_RUN_DIR="$run_dir",SLURM_LOG_DIR="$log_dir" \
      --output="${log_dir}/slide-thumbnails-publish-%j.out" \
      --error="${log_dir}/slide-thumbnails-publish-%j.err" \
      --wrap="exec bash '$SCRIPT_PATH' publish"
  })"; then
    echo "publisher submission failed; canceling worker array ${worker_job}" >&2
    scancel "$worker_job" || true
    return 1
  fi
  echo "worker_job=${worker_job}"
  echo "publish_job=${publish_job}"
  echo "run_dir=${run_dir}"
}

worker_mode() {
  [[ -n "${SLURM_SHARED_RUN_DIR:-}" ]] || { echo "SLURM_SHARED_RUN_DIR is required" >&2; exit 2; }
  source_env
  local meta_file="${SLURM_SHARED_RUN_DIR}/run-meta.json"
  local result_dir="${SLURM_SHARED_RUN_DIR}/results"
  local array_job_id="${SLURM_ARRAY_JOB_ID:-${SLURM_JOB_ID:-manual}}"
  local array_task_id="${SLURM_ARRAY_TASK_ID:-0}"
  local summary_path="${LOG_DIR}/slide-thumbnail-summary-${array_job_id}-${array_task_id}.json"
  local failures_path="${LOG_DIR}/slide-thumbnail-failures-${array_job_id}-${array_task_id}.json"

  mkdir -p "$result_dir"
  "$PYTHON_BIN" - <<'PY' "$meta_file" "$result_dir" "$summary_path" "$failures_path" "$array_task_id"
import json
import sys
from pathlib import Path

from tools.generate_slide_thumbnails import iter_candidate_rows
from tools.generate_slide_thumbnails import process_candidate_rows

meta_file, result_dir, summary_path, failures_path, task_index = sys.argv[1:]
with open(meta_file, "r", encoding="utf-8") as handle:
    meta = json.load(handle)
task_path = Path(meta["candidate_dir"]) / f"task-{int(task_index):04d}.jsonl"
result_path = Path(result_dir) / f"task-{int(task_index):04d}.jsonl"
candidate_count = sum(1 for _ in iter_candidate_rows(str(task_path)))
failures = process_candidate_rows(
    warehouse_id=meta["warehouse_id"],
    root_uri=meta["root_uri"],
    master_size=int(meta["master_size"]),
    rows=iter_candidate_rows(str(task_path)),
    manifest_version=meta["manifest_version"],
    result_path=str(result_path),
    timeout_sec=int(meta["batch_timeout_sec"]),
)
summary = {
    "candidate_count": candidate_count,
    "failure_count": len(failures),
    "success_count": candidate_count - len(failures),
    "manifest_version": meta["manifest_version"],
    "task_index": int(task_index),
}
with open(summary_path, "w", encoding="utf-8") as handle:
    json.dump(summary, handle, indent=2, sort_keys=True)
with open(failures_path, "w", encoding="utf-8") as handle:
    json.dump(failures, handle, indent=2, sort_keys=True)
print(json.dumps(summary, sort_keys=True))
PY
}

publish_mode() {
  [[ -n "${SLURM_SHARED_RUN_DIR:-}" ]] || { echo "SLURM_SHARED_RUN_DIR is required" >&2; exit 2; }
  source_env
  local meta_file="${SLURM_SHARED_RUN_DIR}/run-meta.json"
  local publish_summary="${LOG_DIR}/slide-thumbnail-publish-summary-${SLURM_JOB_ID:-manual}.json"
  local publish_failures="${LOG_DIR}/slide-thumbnail-publish-failures-${SLURM_JOB_ID:-manual}.json"

  "$PYTHON_BIN" - <<'PY' "$meta_file" "$publish_summary" "$publish_failures" "${SLURM_SHARED_RUN_DIR}"
import json
import sys
from pathlib import Path

from tools.generate_slide_thumbnails import cleanup_run_artifacts
from tools.generate_slide_thumbnails import _iter_result_records
from tools.generate_slide_thumbnails import publish_manifest_for_current_inventory
from tools.generate_slide_thumbnails import publish_registry_results

meta_file, summary_path, failures_path, run_dir = sys.argv[1:]
with open(meta_file, "r", encoding="utf-8") as handle:
    meta = json.load(handle)
result_paths = sorted(str(path) for path in (Path(run_dir) / "results").glob("task-*.jsonl"))
stats = publish_registry_results(meta["warehouse_id"], result_paths)
manifest = publish_manifest_for_current_inventory(
    warehouse_id=meta["warehouse_id"],
    manifest_uri=meta["manifest_uri"],
    master_size=int(meta["master_size"]),
    manifest_version=meta["manifest_version"],
)
failures = [record for record in _iter_result_records(result_paths) if record.get("status") != "success"]
summary = {
    **stats,
    "manifest_uri": meta["manifest_uri"],
    "manifest_version": meta["manifest_version"],
    "manifest_slide_count": len(manifest["slides"]),
    "result_file_count": len(result_paths),
}
with open(summary_path, "w", encoding="utf-8") as handle:
    json.dump(summary, handle, indent=2, sort_keys=True)
with open(failures_path, "w", encoding="utf-8") as handle:
    json.dump(failures, handle, indent=2, sort_keys=True)

cleanup_run_artifacts(run_dir)
print(json.dumps(summary, sort_keys=True))
PY
}

main() {
  local mode="${1:-}"
  case "$mode" in
    submit|retry)
      shift
      submit_array "$mode" "$@"
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
