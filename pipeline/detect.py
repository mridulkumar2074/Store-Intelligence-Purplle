"""
Detection pipeline: process a single camera clip, emit structured events.

Usage:
    python pipeline/detect.py \
        --store-id ST1008 \
        --camera-id CAM_ENTRY \
        --video "Store 1/CAM 3 - entry.mp4" \
        --camera-type entry \
        --layout data/store_layout.json \
        --api-url http://localhost:8000 \
        [--pos-csv data/pos_transactions.csv] \
        [--start-ts "2026-04-10T12:00:00Z"]
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from pipeline.emit import EventEmitter
from pipeline.tracker import ReIDManager, TrackState, extract_appearance
from pipeline.zone_classifier import Zone, ZoneClassifier
from pipeline.staff_detector import is_staff as detect_staff

logger = logging.getLogger("pipeline.detect")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Dwell emit cadence (emit ZONE_DWELL every N ms of continuous dwell)
DWELL_EMIT_INTERVAL_MS = 30_000
# Line crossing buffer: track must cross the threshold line and stay on the
# other side for this many consecutive frames before we commit the event.
CROSSING_CONFIRM_FRAMES = 3
# Re-entry cooldown: ignore re-entry if same visitor exited < 10s ago
REENTRY_COOLDOWN_S = 10
# Billing abandon: person left billing zone and no POS tx in next 5 min (offline mode)
POS_CORRELATION_WINDOW_S = 300


def _load_pos_timestamps(pos_csv: Optional[str], store_id: str) -> list[float]:
    if not pos_csv:
        return []
    result: list[float] = []
    try:
        with open(pos_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("store_id", "").strip() != store_id:
                    continue
                try:
                    d, t = row["order_date"].strip(), row["order_time"].strip()
                    dt = datetime.strptime(f"{d} {t}", "%d-%m-%Y %H:%M:%S")
                    result.append(dt.timestamp())
                except Exception:
                    pass
    except Exception:
        pass
    return result


def process_video(
    video_path: str,
    store_id: str,
    camera_id: str,
    camera_type: str,
    layout_path: str,
    emitter: EventEmitter,
    reid: ReIDManager,
    pos_timestamps: list[float],
    clip_start_ts: Optional[datetime] = None,
) -> None:
    try:
        import cv2
    except ImportError:
        logger.error("cv2 not installed. Install requirements-pipeline.txt.")
        sys.exit(1)

    try:
        from ultralytics import YOLO
    except ImportError:
        logger.error("ultralytics not installed. Install requirements-pipeline.txt.")
        sys.exit(1)

    model = YOLO("yolov8n.pt")
    zone_clf = ZoneClassifier(layout_path, store_id, camera_id)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.error("Cannot open video: %s", video_path)
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    frame_no = 0

    # For entry cameras: track which side of the line each track was on
    entry_line_y: Optional[int] = None
    if camera_type == "entry":
        with open(layout_path, encoding="utf-8") as f:
            layout = json.load(f)
        entry_cfg = layout["stores"].get(store_id, {}).get("entry_line", {})
        if entry_cfg.get("camera") == camera_id:
            entry_line_y = entry_cfg.get("y", 700)

    # Track state: previous centroid y for each track_id
    prev_y: dict[int, float] = {}
    crossing_buffer: dict[int, int] = {}   # track_id → consecutive frames below line
    billing_zone_occupants: dict[str, datetime] = {}   # visitor_id → join time
    session_seqs: dict[str, int] = {}

    def _ts(frame_no: int) -> datetime:
        base = clip_start_ts or datetime.now(timezone.utc)
        return base + timedelta(seconds=frame_no / fps)

    def _seq(vid: str) -> int:
        session_seqs[vid] = session_seqs.get(vid, 0) + 1
        return session_seqs[vid]

    active_tracks: dict[int, TrackState] = {}
    prev_track_ids: set[int] = set()

    logger.info("Processing %s (camera_type=%s, entry_line_y=%s)", video_path, camera_type, entry_line_y)

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame_no += 1
        current_ts = _ts(frame_no)

        # Run detection every frame (skip_frames=0 for accuracy)
        results = model.track(frame, persist=True, classes=[0], verbose=False)
        current_track_ids: set[int] = set()

        if results and results[0].boxes is not None:
            boxes = results[0].boxes
            for box in boxes:
                if box.id is None:
                    continue
                track_id = int(box.id.item())
                current_track_ids.add(track_id)
                conf = float(box.conf.item())
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                bbox = (x1, y1, x2, y2)
                cx = (x1 + x2) / 2
                cy = (y1 + y2) / 2

                appearance = extract_appearance(frame, bbox)
                staff_flag, _ = detect_staff(frame, bbox)

                # New track appeared
                if track_id not in active_tracks:
                    state, is_reentry = reid.register_new_track(track_id, bbox, appearance)
                    state.is_staff = staff_flag
                    active_tracks[track_id] = state

                    if camera_type == "entry" and entry_line_y is not None:
                        # Will emit ENTRY once crossing is confirmed
                        prev_y[track_id] = cy
                    else:
                        # For zone/billing cameras: just track zones
                        zone = zone_clf.classify(cx, cy)
                        if zone:
                            state.current_zone = zone.zone_id
                            state.zone_entered_at = time.time()
                            ev = emitter.make_zone_enter(
                                state.visitor_id, camera_id, current_ts, zone, conf,
                                staff_flag, _seq(state.visitor_id)
                            )
                            emitter.emit(ev)
                            if camera_type == "billing" and not staff_flag:
                                q_depth = len(billing_zone_occupants)
                                if q_depth > 0:
                                    ev2 = emitter.make_billing_queue_join(
                                        state.visitor_id, camera_id, current_ts, zone,
                                        q_depth, conf, _seq(state.visitor_id)
                                    )
                                    emitter.emit(ev2)
                                billing_zone_occupants[state.visitor_id] = current_ts
                else:
                    state = active_tracks[track_id]
                    reid.update_track(track_id, bbox, appearance)

                    # Entry/exit line crossing
                    if camera_type == "entry" and entry_line_y is not None:
                        prev = prev_y.get(track_id, cy)
                        was_above = prev < entry_line_y
                        now_below = cy >= entry_line_y

                        if was_above and now_below:
                            crossing_buffer[track_id] = crossing_buffer.get(track_id, 0) + 1
                            if crossing_buffer[track_id] == CROSSING_CONFIRM_FRAMES:
                                # Confirmed ENTRY
                                if is_reentry if track_id not in prev_track_ids else False:
                                    gap = int(time.time() - (reid.get_active(track_id) or state).first_seen)
                                    ev = emitter.make_reentry(
                                        state.visitor_id, camera_id, current_ts, conf, gap,
                                        _seq(state.visitor_id)
                                    )
                                else:
                                    ev = emitter.make_entry(
                                        state.visitor_id, camera_id, current_ts, conf,
                                        staff_flag, _seq(state.visitor_id)
                                    )
                                emitter.emit(ev)
                        elif not was_above or not now_below:
                            crossing_buffer.pop(track_id, None)
                        prev_y[track_id] = cy

                    # Zone tracking (zone + billing cameras)
                    if camera_type in ("zone", "billing"):
                        zone = zone_clf.classify(cx, cy)
                        new_zone_id = zone.zone_id if zone else None
                        old_zone_id = state.current_zone

                        if new_zone_id != old_zone_id:
                            # Zone changed
                            if old_zone_id is not None and state.zone_entered_at:
                                # Find old zone object
                                old_zone_obj = next(
                                    (z for z in zone_clf.zones if z.zone_id == old_zone_id), None
                                )
                                dwell_ms = int((time.time() - state.zone_entered_at) * 1000)
                                if old_zone_obj:
                                    ev = emitter.make_zone_exit(
                                        state.visitor_id, camera_id, current_ts, old_zone_obj,
                                        dwell_ms, conf, staff_flag, _seq(state.visitor_id)
                                    )
                                    emitter.emit(ev)

                                    # Billing queue abandon check (offline)
                                    if camera_type == "billing" and not staff_flag:
                                        if state.visitor_id in billing_zone_occupants:
                                            join_ts = billing_zone_occupants.pop(state.visitor_id)
                                            join_epoch = join_ts.timestamp()
                                            exit_epoch = current_ts.timestamp()
                                            purchased = any(
                                                join_epoch <= pts <= exit_epoch + POS_CORRELATION_WINDOW_S
                                                for pts in pos_timestamps
                                            )
                                            if not purchased:
                                                ev2 = emitter.make_billing_queue_abandon(
                                                    state.visitor_id, camera_id, current_ts,
                                                    old_zone_obj, dwell_ms, conf,
                                                    _seq(state.visitor_id)
                                                )
                                                emitter.emit(ev2)

                            state.current_zone = new_zone_id
                            state.zone_entered_at = time.time() if new_zone_id else None
                            state.dwell_seconds = 0

                            if zone:
                                ev = emitter.make_zone_enter(
                                    state.visitor_id, camera_id, current_ts, zone, conf,
                                    staff_flag, _seq(state.visitor_id)
                                )
                                emitter.emit(ev)
                                if camera_type == "billing" and not staff_flag:
                                    q_depth = len(billing_zone_occupants)
                                    if q_depth > 0:
                                        ev2 = emitter.make_billing_queue_join(
                                            state.visitor_id, camera_id, current_ts, zone,
                                            q_depth, conf, _seq(state.visitor_id)
                                        )
                                        emitter.emit(ev2)
                                    billing_zone_occupants[state.visitor_id] = current_ts
                        else:
                            # Same zone — update dwell and emit ZONE_DWELL every 30s
                            if zone and state.zone_entered_at:
                                dwell_ms = int((time.time() - state.zone_entered_at) * 1000)
                                intervals = dwell_ms // DWELL_EMIT_INTERVAL_MS
                                prev_intervals = int(state.dwell_seconds * 1000) // DWELL_EMIT_INTERVAL_MS
                                if intervals > prev_intervals:
                                    ev = emitter.make_zone_dwell(
                                        state.visitor_id, camera_id, current_ts, zone,
                                        dwell_ms, conf, staff_flag, _seq(state.visitor_id)
                                    )
                                    emitter.emit(ev)
                                state.dwell_seconds = dwell_ms / 1000

        # Tracks that disappeared
        lost_ids = prev_track_ids - current_track_ids
        for tid in lost_ids:
            state = active_tracks.pop(tid, None)
            if state and camera_type == "entry":
                current_ts_exit = _ts(frame_no)
                prev_y_val = prev_y.get(tid, 0)
                if entry_line_y and prev_y_val < entry_line_y:
                    # Was above line when lost → exiting
                    ev = emitter.make_exit(
                        state.visitor_id, camera_id, current_ts_exit, 0.7,
                        state.is_staff, _seq(state.visitor_id)
                    )
                    emitter.emit(ev)
            if state:
                reid.close_track(tid)
            prev_y.pop(tid, None)
            crossing_buffer.pop(tid, None)

        prev_track_ids = current_track_ids

    cap.release()
    emitter.flush()
    logger.info("Finished processing %s — %d frames", video_path, frame_no)


def main() -> None:
    parser = argparse.ArgumentParser(description="Store Intelligence Detection Pipeline")
    parser.add_argument("--store-id", required=True)
    parser.add_argument("--camera-id", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--camera-type", required=True, choices=["entry", "zone", "billing"])
    parser.add_argument("--layout", default="data/store_layout.json")
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--pos-csv", default=None)
    parser.add_argument("--start-ts", default=None, help="ISO-8601 clip start timestamp")
    args = parser.parse_args()

    clip_start = None
    if args.start_ts:
        clip_start = datetime.fromisoformat(args.start_ts.replace("Z", "+00:00"))

    pos_ts = _load_pos_timestamps(args.pos_csv, args.store_id)
    emitter = EventEmitter(args.api_url, args.store_id)
    reid = ReIDManager()

    process_video(
        video_path=args.video,
        store_id=args.store_id,
        camera_id=args.camera_id,
        camera_type=args.camera_type,
        layout_path=args.layout,
        emitter=emitter,
        reid=reid,
        pos_timestamps=pos_ts,
        clip_start_ts=clip_start,
    )


if __name__ == "__main__":
    main()
