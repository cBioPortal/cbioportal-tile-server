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
  tools/run_thumbnail_pipeline_slurm.sh resume --run-dir DIR [options]
  tools/run_thumbnail_pipeline_slurm.sh status --run-dir DIR
  tools/run_thumbnail_pipeline_slurm.sh worker
  tools/run_thumbnail_pipeline_slurm.sh publish

options:
  --slides-per-task N  default: 2000
  --concurrency N      default: 2
  --mem SIZE           default: 8G
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

cleanup_process_scoped_paths() {
  local run_root="${SLURM_SHARED_RUN_DIR:-${THUMBNAIL_RUN_DIR:-}}"
  local path
  for path in "${THUMBNAIL_TMPDIR:-}" "${BLOCKCACHE_PATH:-}"; do
    [[ -n "$path" && -n "$run_root" && "$path" == "$run_root/"* ]] || continue
    rm -rf -- "$path"
  done
}

parse_submit_options() {
  SUBMIT_MANIFEST_URI=""
  SUBMIT_ROOT_URI=""
  SUBMIT_SLIDES_PER_TASK=2000
  SUBMIT_CONCURRENCY=2
  SUBMIT_MEM=8G
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
      --mem)
        [[ $# -ge 2 ]] || usage
        SUBMIT_MEM="$2"
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
  [[ "$SUBMIT_MEM" =~ ^[1-9][0-9]*G$ ]] || usage
  if [[ -n "$SUBMIT_LIMIT" ]]; then
    [[ "$SUBMIT_LIMIT" =~ ^[1-9][0-9]*$ ]] || usage
  fi
}

parse_resume_options() {
  RESUME_RUN_DIR=""
  RESUME_CONCURRENCY=4
  RESUME_MEM=8G

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --run-dir)
        [[ $# -ge 2 ]] || usage
        RESUME_RUN_DIR="$2"
        shift 2
        ;;
      --concurrency)
        [[ $# -ge 2 ]] || usage
        RESUME_CONCURRENCY="$2"
        shift 2
        ;;
      --mem)
        [[ $# -ge 2 ]] || usage
        RESUME_MEM="$2"
        shift 2
        ;;
      *)
        usage
        ;;
    esac
  done

  [[ -n "$RESUME_RUN_DIR" && -f "$RESUME_RUN_DIR/run-meta.json" ]] || usage
  [[ "$RESUME_CONCURRENCY" =~ ^[1-9][0-9]*$ ]] || usage
  [[ "$RESUME_MEM" =~ ^[1-9][0-9]*G$ ]] || usage
}

acquire_submission_lock() {
  mkdir -p "$SHARED_ROOT"
  exec 8>"${SHARED_ROOT}/submission.lock"
  flock -n 8 || {
    echo "another thumbnail submission or resume is active" >&2
    return 1
  }
}

assert_no_active_run() {
  local manifest_uri="$1"
  local excluded_run_dir="${2:-}"
  local metadata_path
  while IFS= read -r metadata_path; do
    local run_dir
    run_dir="$(dirname "$metadata_path")"
    [[ "$run_dir" == "$excluded_run_dir" ]] && continue
    while IFS=$'\t' read -r metadata_manifest worker_job publisher_job; do
      [[ "$metadata_manifest" == "$manifest_uri" ]] || continue
      local job_id
      for job_id in "$worker_job" "$publisher_job"; do
        [[ "$job_id" =~ ^[0-9]+$ ]] || continue
        if squeue -h -j "$job_id" -o "%T" 2>/dev/null | rg -q \
          '^(PENDING|RUNNING|CONFIGURING|COMPLETING|SUSPENDED)$'; then
          echo "active thumbnail run exists for ${manifest_uri}: ${run_dir}" >&2
          return 1
        fi
      done
    done < <("$PYTHON_BIN" - "$metadata_path" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    meta = json.load(handle)
print("\t".join(
    str(meta.get(key) or "")
    for key in ("manifest_uri", "worker_job", "publisher_job")
))
PY
    )
  done < <(rg --files --hidden -g 'run-meta.json' "$SHARED_ROOT" 2>/dev/null || true)
}

prepare_candidates() {
  local mode="$1"
  local manifest_uri="$2"
  local root_uri="$3"
  local slides_per_task="$4"
  local limit="$5"
  local run_id
  run_id="$(date -u +%Y%m%d%H%M%S%N)-$$"
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
manifest_version = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
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
  cleanup_process_scoped_paths
  echo "run_dir=${run_dir}"
}

submit_worker_array() {
  local run_dir="$1"
  local log_dir="$2"
  local array_expression="$3"
  local concurrency="$4"
  local memory="$5"
  sbatch --parsable \
    --job-name=slide-thumbnails \
    --partition=hpc \
    --cpus-per-task=2 \
    --mem="$memory" \
    --time=24:00:00 \
    --requeue \
    --signal=B:TERM@120 \
    --array="${array_expression}%${concurrency}" \
    --export=ALL,SLURM_SHARED_RUN_DIR="$run_dir",SLURM_LOG_DIR="$log_dir" \
    --output="${log_dir}/slide-thumbnails-%A_%a.out" \
    --error="${log_dir}/slide-thumbnails-%A_%a.err" \
    --wrap="exec bash '$SCRIPT_PATH' worker"
}

submit_publisher() {
  local run_dir="$1"
  local log_dir="$2"
  local dependency="${3:-}"
  local dependency_arg=()
  if [[ -n "$dependency" ]]; then
    dependency_arg=(--dependency="afterany:${dependency}")
  fi
  sbatch --parsable \
    --job-name=slide-thumbnails-publish \
    --partition=hpc \
    --cpus-per-task=1 \
    --mem=4G \
    --time=08:00:00 \
    "${dependency_arg[@]}" \
    --export=ALL,SLURM_SHARED_RUN_DIR="$run_dir",SLURM_LOG_DIR="$log_dir" \
    --output="${log_dir}/slide-thumbnails-publish-%j.out" \
    --error="${log_dir}/slide-thumbnails-publish-%j.err" \
    --wrap="exec bash '$SCRIPT_PATH' publish"
}

record_job_ids() {
  local run_dir="$1"
  local worker_job="${2:-}"
  local publish_job="${3:-}"
  "$PYTHON_BIN" - "$run_dir" "$worker_job" "$publish_job" <<'PY'
import json
import sys
from pathlib import Path

from tools.generate_slide_thumbnails import _atomic_write_json

run_dir, worker_job, publish_job = sys.argv[1:]
meta_path = Path(run_dir) / "run-meta.json"
with meta_path.open("r", encoding="utf-8") as handle:
    meta = json.load(handle)
meta.update({
    "worker_job": worker_job or None,
    "publisher_job": publish_job or None,
})
_atomic_write_json(meta_path, meta)
PY
}

submit_array() {
  local mode="$1"
  shift
  parse_submit_options "$@"
  mkdir -p "$SHARED_ROOT"
  acquire_submission_lock
  assert_no_active_run "$SUBMIT_MANIFEST_URI"

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
  worker_job="$(submit_worker_array "$run_dir" "$log_dir" "0-$((actual_tasks - 1))" "$SUBMIT_CONCURRENCY" "$SUBMIT_MEM")"
  local publish_job
  if ! publish_job="$(submit_publisher "$run_dir" "$log_dir" "$worker_job")"; then
    echo "publisher submission failed; canceling worker array ${worker_job}" >&2
    scancel "$worker_job" || true
    return 1
  fi
  record_job_ids "$run_dir" "$worker_job" "$publish_job"
  echo "worker_job=${worker_job}"
  echo "publish_job=${publish_job}"
  echo "run_dir=${run_dir}"
}

resume_array() {
  parse_resume_options "$@"
  local run_dir
  run_dir="$(cd "$RESUME_RUN_DIR" && pwd)"
  acquire_submission_lock
  export SLURM_SHARED_RUN_DIR="$run_dir"
  export SLURM_LOG_DIR="${run_dir}/logs"
  source_env
  local manifest_uri
  manifest_uri="$($PYTHON_BIN -c 'import json,sys; print(json.load(open(sys.argv[1]))["manifest_uri"])' "${run_dir}/run-meta.json")"
  assert_no_active_run "$manifest_uri" "$run_dir"

  local audit_json
  audit_json="$($PYTHON_BIN - "$run_dir" <<'PY'
import json
import sys

from tools.generate_slide_thumbnails import audit_thumbnail_run

audit = audit_thumbnail_run(
    sys.argv[1],
    adopt_legacy=True,
    quarantine_incomplete=True,
)
audit.pop("tasks", None)
print(json.dumps(audit, sort_keys=True))
PY
)"
  echo "$audit_json"
  local array_expression
  array_expression="$(printf '%s' "$audit_json" | "$PYTHON_BIN" -c \
    'import json,sys; from tools.generate_slide_thumbnails import slurm_array_expression; print(slurm_array_expression(json.load(sys.stdin)["incomplete_task_indexes"]))')"
  local log_dir="${run_dir}/logs"
  if [[ -z "$array_expression" ]]; then
    local publish_job
    publish_job="$(submit_publisher "$run_dir" "$log_dir")"
    record_job_ids "$run_dir" "" "$publish_job"
    cleanup_process_scoped_paths
    echo "publish_job=${publish_job}"
    echo "run_dir=${run_dir}"
    return 0
  fi

  local worker_job
  worker_job="$(submit_worker_array "$run_dir" "$log_dir" "$array_expression" "$RESUME_CONCURRENCY" "$RESUME_MEM")"
  local publish_job
  if ! publish_job="$(submit_publisher "$run_dir" "$log_dir" "$worker_job")"; then
    echo "publisher submission failed; canceling worker array ${worker_job}" >&2
    scancel "$worker_job" || true
    return 1
  fi
  record_job_ids "$run_dir" "$worker_job" "$publish_job"
  cleanup_process_scoped_paths
  echo "worker_job=${worker_job}"
  echo "publish_job=${publish_job}"
  echo "array=${array_expression}"
  echo "run_dir=${run_dir}"
}

status_mode() {
  parse_resume_options "$@"
  local run_dir
  run_dir="$(cd "$RESUME_RUN_DIR" && pwd)"
  cd "$WORKDIR"
  PYTHON_BIN="${THUMBNAIL_PYTHON:-${WORKDIR}/.venv/bin/python}"
  [[ -x "$PYTHON_BIN" ]] || { echo "Python executable not found: $PYTHON_BIN" >&2; return 1; }
  "$PYTHON_BIN" - "$run_dir" <<'PY'
import json
import sys

from tools.generate_slide_thumbnails import audit_thumbnail_run

audit = audit_thumbnail_run(sys.argv[1])
audit.pop("tasks", None)
print(json.dumps(audit, indent=2, sort_keys=True))
PY
}

worker_mode() {
  [[ -n "${SLURM_SHARED_RUN_DIR:-}" ]] || { echo "SLURM_SHARED_RUN_DIR is required" >&2; exit 2; }
  source_env
  trap cleanup_process_scoped_paths EXIT TERM INT
  local meta_file="${SLURM_SHARED_RUN_DIR}/run-meta.json"
  local result_dir="${SLURM_SHARED_RUN_DIR}/results"
  local array_job_id="${SLURM_ARRAY_JOB_ID:-${SLURM_JOB_ID:-manual}}"
  local array_task_id="${SLURM_ARRAY_TASK_ID:-0}"
  local summary_path="${LOG_DIR}/slide-thumbnail-summary-${array_job_id}-${array_task_id}.json"
  local failures_path="${LOG_DIR}/slide-thumbnail-failures-${array_job_id}-${array_task_id}.json"

  mkdir -p "$result_dir"
  "$PYTHON_BIN" - <<'PY' "$meta_file" "$result_dir" "$summary_path" "$failures_path" "$array_task_id" "${SLURM_JOB_ID:-manual}"
import json
import os
import sys
from pathlib import Path

from tools.generate_slide_thumbnails import iter_candidate_rows
from tools.generate_slide_thumbnails import process_candidate_rows
from tools.generate_slide_thumbnails import write_task_completion_marker

meta_file, result_dir, summary_path, failures_path, task_index, job_id = sys.argv[1:]
with open(meta_file, "r", encoding="utf-8") as handle:
    meta = json.load(handle)
task_number = int(task_index)
task_path = Path(meta["candidate_dir"]) / f"task-{task_number:04d}.jsonl"
result_path = Path(result_dir) / f"task-{task_number:04d}.jsonl"
partial_path = Path(f"{result_path}.partial.{job_id}.{os.getpid()}")
partial_path.unlink(missing_ok=True)
candidate_count = sum(1 for _ in iter_candidate_rows(str(task_path)))
failures = process_candidate_rows(
    warehouse_id=meta["warehouse_id"],
    root_uri=meta["root_uri"],
    master_size=int(meta["master_size"]),
    rows=iter_candidate_rows(str(task_path)),
    manifest_version=meta["manifest_version"],
    result_path=str(partial_path),
    timeout_sec=int(meta["batch_timeout_sec"]),
)
os.replace(partial_path, result_path)
write_task_completion_marker(
    run_dir=Path(meta_file).parent,
    task_index=task_number,
    candidate_count=candidate_count,
    manifest_version=meta["manifest_version"],
    result_path=result_path,
)
summary = {
    "candidate_count": candidate_count,
    "failure_count": len(failures),
    "success_count": candidate_count - len(failures),
    "manifest_version": meta["manifest_version"],
    "task_index": task_number,
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
  local publish_lock="${SHARED_ROOT}/manifest-publish.lock"
  exec 9>"$publish_lock"
  flock -n 9 || { echo "another thumbnail manifest publication is active" >&2; exit 1; }
  local meta_file="${SLURM_SHARED_RUN_DIR}/run-meta.json"
  local publish_summary="${LOG_DIR}/slide-thumbnail-publish-summary-${SLURM_JOB_ID:-manual}.json"
  local publish_failures="${LOG_DIR}/slide-thumbnail-publish-failures-${SLURM_JOB_ID:-manual}.json"

  "$PYTHON_BIN" - <<'PY' "$meta_file" "$publish_summary" "$publish_failures" "${SLURM_SHARED_RUN_DIR}"
import json
import sys
from pathlib import Path

from tools.generate_slide_thumbnails import audit_thumbnail_run
from tools.generate_slide_thumbnails import cleanup_run_artifacts
from tools.generate_slide_thumbnails import _iter_result_records
from tools.generate_slide_thumbnails import publish_manifest_for_current_inventory
from tools.generate_slide_thumbnails import publish_registry_results

meta_file, summary_path, failures_path, run_dir = sys.argv[1:]
with open(meta_file, "r", encoding="utf-8") as handle:
    meta = json.load(handle)
audit = audit_thumbnail_run(run_dir)
if not audit["publishable"]:
    raise RuntimeError(
        "thumbnail run is not publishable; incomplete tasks: "
        + ",".join(str(index) for index in audit["incomplete_task_indexes"])
    )
result_paths = [
    str(Path(task["result_path"]))
    for task in audit["tasks"]
    if task["state"] == "complete"
]
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
    "candidate_count": audit["candidate_count"],
    "task_count": audit["task_count"],
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
    resume)
      shift
      resume_array "$@"
      ;;
    status)
      shift
      status_mode "$@"
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
