#!/usr/bin/env bash
# tools/migrate_all_studies.sh
#
# Batch-generate cBioPortal WSI v2, pathology timeline, and PATIENT-level
# resource files for every MSK IMPACT study in the private repo. All cleanup,
# export, and resource changes are made in a locked candidate copy and the
# complete study directory is swapped in only after every step succeeds.
#
# Usage (from repo root):
#   bash tools/migrate_all_studies.sh [--dry-run] [--private-dir <path>]
#
# Env / flags:
#   PRIVATE_DIR  — path to automation_tool_datasets/ (default: ../private/automation_tool_datasets)
#   BASE_URL     — tile server URL (default: https://slides.cbioportal.org)
#   --dry-run    — passed through to the Python tool (no files written)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PRIVATE_DIR="${PRIVATE_DIR:-$REPO_ROOT/../private/automation_tool_datasets}"
BASE_URL="${BASE_URL:-https://slides.cbioportal.org}"
# Publication is fail-closed: the batch exporter must have the same URI
# allowlists as the online tile service.
export WSI_ALLOWED_SOURCE_PREFIXES="${WSI_ALLOWED_SOURCE_PREFIXES:-s3://mskmind-bkt/reef-slides/,s3://pathology/CRC_21-167/slides/,s3://pathology/CRC_21-167/crc_slides/,s3://pathology/CART_19-373/,s3://pathology/BR_20-226/slides/}"
export WSI_ALLOWED_THUMBNAIL_PREFIXES="${WSI_ALLOWED_THUMBNAIL_PREFIXES:-s3://mskmind-bkt/wsi-thumbnails/}"
DRY_RUN=""
LOG_FILE="$REPO_ROOT/docs/migration_$(date +%Y%m%d_%H%M%S).log"

# Parse flags
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN="--dry-run" ;;
    --private-dir=*) PRIVATE_DIR="${arg#*=}" ;;
    --private-dir) shift; PRIVATE_DIR="$1" ;;
  esac
done

STUDIES=(
  bladder_msk_2025
  bone_msk_2025
  breast_msk_2025
  msk_spectrum_tme_2022
  esca_msk_2025
  gist_msk_2025
  hnsc_msk_2025
  kidney_msk_2024
  luad_msk_2025
  lung_msk_2024
  mpnst_msk_2025
  paad_msk_2025
  prad_msk_2025
  prostate_msk_2025
  soft_tissue_msk_2025
)

echo "=== Migration started $(date) ===" | tee "$LOG_FILE"
echo "PRIVATE_DIR : $PRIVATE_DIR"       | tee -a "$LOG_FILE"
echo "BASE_URL    : $BASE_URL"           | tee -a "$LOG_FILE"
echo "DRY_RUN     : ${DRY_RUN:-no}"     | tee -a "$LOG_FILE"
echo ""                                  | tee -a "$LOG_FILE"

PASS=0; FAIL=0; SKIP=0

for study in "${STUDIES[@]}"; do
  dir="$PRIVATE_DIR/$study"
  if [ ! -d "$dir" ]; then
    echo "SKIP $study — directory not found" | tee -a "$LOG_FILE"
    SKIP=$((SKIP+1))
    continue
  fi

  echo ">>> $study  $(date +%H:%M:%S)" | tee -a "$LOG_FILE"

  set +e
  if [ -n "$DRY_RUN" ]; then
    echo "  dry run — skipping study-file cleanup and WSI/timeline generation" | tee -a "$LOG_FILE"
    cleanup_rc=0
    timepoint_cleanup_rc=0
    export_rc=0
    rc=0
  else
    cleanup_rc=1
    timepoint_cleanup_rc=1
    export_rc=1
    rc=1
    lock_file="${dir}.wsi-publish.lock"
    exec {lock_fd}>"$lock_file"
    if flock -n "$lock_fd"; then
      candidate_dir=$(mktemp -d "${dir}.wsi-candidate.XXXXXX")
      backup_dir=""
      if cp -a "$dir/." "$candidate_dir/"; then
        # All transformations happen in the candidate. The live study remains
        # untouched until every validation and resource generation step passes.
        python3 "$REPO_ROOT/tools/generate_wsi_clinical_attrs.py" \
          --study-dir "$candidate_dir" 2>&1 | tee -a "$LOG_FILE"
        cleanup_rc=${PIPESTATUS[0]}
        python3 "$REPO_ROOT/tools/generate_wsi_timepoint_clinical_attrs.py" \
          --study-dir "$candidate_dir" 2>&1 | tee -a "$LOG_FILE"
        timepoint_cleanup_rc=${PIPESTATUS[0]}
        python3 "$REPO_ROOT/tools/export_materialized_hierarchy_snapshot.py" \
          --study-dir "$candidate_dir" \
          --study-id "$study" \
          --output-dir "$candidate_dir" 2>&1 | tee -a "$LOG_FILE"
        export_rc=${PIPESTATUS[0]}
      fi
      if [ "$cleanup_rc" -eq 0 ] && [ "$timepoint_cleanup_rc" -eq 0 ] && [ "$export_rc" -eq 0 ]; then
        python3 "$REPO_ROOT/tools/generate_resource_patient.py" \
          --study-dir "$candidate_dir" \
          --base-url "$BASE_URL" 2>&1 | tee -a "$LOG_FILE"
        rc=${PIPESTATUS[0]}
        if [ "$rc" -eq 0 ]; then
          for f in data_resource_sample.txt meta_resource_sample.txt; do
            if [ -f "$candidate_dir/$f" ]; then
              rm "$candidate_dir/$f"
              echo "  removed: $f" | tee -a "$LOG_FILE"
            fi
          done
        fi
      fi
      if [ "$cleanup_rc" -eq 0 ] && [ "$timepoint_cleanup_rc" -eq 0 ] \
          && [ "$export_rc" -eq 0 ] && [ "$rc" -eq 0 ]; then
        backup_dir=$(mktemp -d "${dir}.wsi-backup.XXXXXX")
        rmdir "$backup_dir"
        if mv "$dir" "$backup_dir" && mv "$candidate_dir" "$dir"; then
          candidate_dir=""
          rm -rf "$backup_dir"
          backup_dir=""
        else
          # Restore the original directory if either replacement move fails.
          if [ -d "$backup_dir" ] && [ ! -e "$dir" ]; then
            mv "$backup_dir" "$dir"
          fi
          export_rc=1
          rc=1
        fi
      fi
      if [ -n "${candidate_dir:-}" ] && [ -d "$candidate_dir" ]; then
        rm -rf "$candidate_dir"
      fi
      if [ -n "${backup_dir:-}" ] && [ -d "$backup_dir" ] && [ ! -e "$dir" ]; then
        mv "$backup_dir" "$dir"
      fi
    else
      echo "  FAILED — another WSI publication is already running for $study" | tee -a "$LOG_FILE"
    fi
    flock -u "$lock_fd"
    eval "exec ${lock_fd}>&-"
  fi
  if [ -n "$DRY_RUN" ]; then
    python3 "$REPO_ROOT/tools/generate_resource_patient.py" \
      --study-dir "$dir" \
      --base-url "$BASE_URL" \
      ${DRY_RUN:+--dry-run} 2>&1 | tee -a "$LOG_FILE"
    rc=${PIPESTATUS[0]}
  fi
  set -e

  if [ "$cleanup_rc" -eq 0 ] && [ "$timepoint_cleanup_rc" -eq 0 ] && [ "$export_rc" -eq 0 ] && [ "$rc" -eq 0 ]; then
    PASS=$((PASS+1))
    if [ -z "$DRY_RUN" ]; then
      for f in data_resource_sample.txt meta_resource_sample.txt; do
        if [ -f "$dir/$f" ]; then
          rm "$dir/$f"
          echo "  removed: $f" | tee -a "$LOG_FILE"
        fi
      done
    fi
  else
    echo "  FAILED (cleanup=$cleanup_rc timepoint_cleanup=$timepoint_cleanup_rc export=$export_rc resource=$rc)" | tee -a "$LOG_FILE"
    FAIL=$((FAIL+1))
  fi
  echo "" | tee -a "$LOG_FILE"
done

echo "=== Done $(date) — pass=$PASS fail=$FAIL skip=$SKIP ===" | tee -a "$LOG_FILE"
echo "Log: $LOG_FILE"

[ $FAIL -eq 0 ]
