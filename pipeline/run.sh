#!/usr/bin/env bash
# ============================================================
# run.sh  — Process all CCTV clips for one or all stores and
#            stream events into the Store Intelligence API.
#
# Usage:
#   bash pipeline/run.sh [--store ST1008] [--api-url http://localhost:8000]
#
# Environment variables:
#   API_URL        (default: http://localhost:8000)
#   CLIPS_BASE_DIR (default: current working directory)
#   LAYOUT_FILE    (default: data/store_layout.json)
#   POS_CSV        (default: data/pos_transactions.csv)
# ============================================================
set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
CLIPS_BASE="${CLIPS_BASE_DIR:-.}"
LAYOUT="${LAYOUT_FILE:-data/store_layout.json}"
POS_CSV="${POS_CSV:-data/pos_transactions.csv}"
STORE_FILTER=""
START_TS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --store)      STORE_FILTER="$2"; shift 2 ;;
    --api-url)    API_URL="$2";      shift 2 ;;
    --start-ts)   START_TS="$2";    shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

run_camera() {
  local store_id="$1"
  local camera_id="$2"
  local video_path="$3"
  local camera_type="$4"

  if [[ ! -f "$video_path" ]]; then
    log "WARN: video not found: $video_path — skipping"
    return
  fi

  log "Processing store=$store_id camera=$camera_id type=$camera_type"
  python -m pipeline.detect \
    --store-id   "$store_id" \
    --camera-id  "$camera_id" \
    --video      "$video_path" \
    --camera-type "$camera_type" \
    --layout     "$LAYOUT" \
    --api-url    "$API_URL" \
    --pos-csv    "$POS_CSV" \
    ${START_TS:+--start-ts "$START_TS"}
  log "Done: $camera_id"
}

process_store_1() {
  local base="$CLIPS_BASE/Store 1"
  run_camera "ST1008" "CAM_ENTRY"    "$base/CAM 3 - entry.mp4"   "entry"
  run_camera "ST1008" "CAM_ZONE_01"  "$base/CAM 1 - zone.mp4"    "zone"
  run_camera "ST1008" "CAM_ZONE_02"  "$base/CAM 2 - zone.mp4"    "zone"
  run_camera "ST1008" "CAM_BILLING"  "$base/CAM 5 - billing.mp4" "billing"
}

process_store_2() {
  local base="$CLIPS_BASE/Store 2"
  run_camera "ST1076" "CAM_ENTRY_01" "$base/entry 1.mp4"         "entry"
  run_camera "ST1076" "CAM_ENTRY_02" "$base/entry 2.mp4"         "entry"
  run_camera "ST1076" "CAM_ZONE_01"  "$base/zone.mp4"            "zone"
  run_camera "ST1076" "CAM_BILLING"  "$base/billing_area.mp4"    "billing"
}

log "Store Intelligence Detection Pipeline starting"
log "API_URL=$API_URL  LAYOUT=$LAYOUT"

case "$STORE_FILTER" in
  ST1008) process_store_1 ;;
  ST1076) process_store_2 ;;
  "")
    process_store_1
    process_store_2
    ;;
  *)
    log "ERROR: Unknown store: $STORE_FILTER"
    exit 1
    ;;
esac

log "All clips processed."
