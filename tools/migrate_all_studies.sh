#!/usr/bin/env bash
# tools/migrate_all_studies.sh
#
# Batch-generate cBioPortal pathology timeline and PATIENT-level resource files
# for every MSK IMPACT study in the private repo. Runs the WSI cleanup passes,
# regenerates PATHOLOGY SLIDES timeline events from the canonical shared
# pathology snapshot, and then refreshes PATIENT-level slide resources.
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
    echo "  dry run — skipping study-file cleanup and timeline generation" | tee -a "$LOG_FILE"
    cleanup_rc=0
    timepoint_cleanup_rc=0
    timeline_rc=0
  else
    python3 "$REPO_ROOT/tools/generate_wsi_clinical_attrs.py" \
      --study-dir "$dir" 2>&1 | tee -a "$LOG_FILE"
    cleanup_rc=${PIPESTATUS[0]}
    python3 "$REPO_ROOT/tools/generate_wsi_timepoint_clinical_attrs.py" \
      --study-dir "$dir" 2>&1 | tee -a "$LOG_FILE"
    timepoint_cleanup_rc=${PIPESTATUS[0]}
    python3 "$REPO_ROOT/tools/generate_pathology_timeline_files.py" \
      --study-dir "$dir" 2>&1 | tee -a "$LOG_FILE"
    timeline_rc=${PIPESTATUS[0]}
  fi
  python3 "$REPO_ROOT/tools/generate_resource_patient.py" \
    --study-dir "$dir" \
    --base-url "$BASE_URL" \
    ${DRY_RUN:+--dry-run} 2>&1 | tee -a "$LOG_FILE"
  rc=${PIPESTATUS[0]}
  set -e

  if [ $cleanup_rc -eq 0 ] && [ $timepoint_cleanup_rc -eq 0 ] && [ $timeline_rc -eq 0 ] && [ $rc -eq 0 ]; then
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
    echo "  FAILED (cleanup=$cleanup_rc timepoint_cleanup=$timepoint_cleanup_rc timeline=$timeline_rc resource=$rc)" | tee -a "$LOG_FILE"
    FAIL=$((FAIL+1))
  fi
  echo "" | tee -a "$LOG_FILE"
done

echo "=== Done $(date) — pass=$PASS fail=$FAIL skip=$SKIP ===" | tee -a "$LOG_FILE"
echo "Log: $LOG_FILE"

[ $FAIL -eq 0 ]
