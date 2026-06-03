"""
Generate sample_events/events.jsonl with synthetic but schema-valid events.

Usage:
    python sample_events/generate_sample.py \
        --store ST1008 \
        --date 2026-04-10 \
        --visitors 30 \
        --out sample_events/events.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

STORE_ID = "ST1008"
CAMERAS = {
    "entry": "CAM_ENTRY",
    "zone1": "CAM_ZONE_01",
    "zone2": "CAM_ZONE_02",
    "billing": "CAM_BILLING",
}
ZONES = [
    ("ST1008_SKINCARE",       "SKINCARE",  "CAM_ZONE_01"),
    ("ST1008_CENTER_DISPLAY", "MAKEUP",    "CAM_ZONE_01"),
    ("ST1008_MAKEUP",         "MAKEUP",    "CAM_ZONE_01"),
    ("ST1008_FRAGRANCE",      "FRAGRANCE", "CAM_ZONE_02"),
    ("ST1008_HAIRCARE",       "HAIRCARE",  "CAM_ZONE_02"),
    ("ST1008_BILLING",        None,        "CAM_BILLING"),
]

random.seed(42)


def _visitor_id(n: int) -> str:
    h = hashlib.sha1(str(n).encode()).hexdigest()[:6]
    return f"VIS_{h}"


def _event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    ts: datetime,
    zone_id: str | None = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.92,
    metadata: dict | None = None,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": ts.isoformat(),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": round(confidence, 3),
        "metadata": metadata or {},
    }


def generate(store_id: str, base_date: str, num_visitors: int) -> list[dict[str, Any]]:
    base = datetime.fromisoformat(f"{base_date}T10:00:00+00:00")
    events: list[dict[str, Any]] = []

    # 2 staff members
    for si in range(2):
        vid = f"STAFF_{si:02d}"
        staff_entry_ts = base + timedelta(minutes=random.randint(0, 5))
        events.append(_event(store_id, CAMERAS["entry"], vid, "ENTRY", staff_entry_ts,
                             is_staff=True, confidence=0.95,
                             metadata={"session_seq": 1}))
        # Staff move through all zones
        t = staff_entry_ts
        for zone_id, sku, cam in ZONES:
            t += timedelta(seconds=random.randint(30, 120))
            events.append(_event(store_id, cam, vid, "ZONE_ENTER", t,
                                 zone_id=zone_id, is_staff=True,
                                 metadata={"session_seq": si + 2, "sku_zone": sku}))
            dwell = random.randint(60, 300) * 1000
            t += timedelta(milliseconds=dwell)
            events.append(_event(store_id, cam, vid, "ZONE_EXIT", t,
                                 zone_id=zone_id, dwell_ms=dwell, is_staff=True,
                                 metadata={"session_seq": si + 3, "sku_zone": sku}))

    # Customers
    # billing_occupants tracks (visitor_id, exit_timestamp) so queue depth
    # is realistic — visitors who haven't exited yet count as in queue.
    billing_occupants: list[tuple[str, object]] = []
    seq_counter: dict[str, int] = {}

    def seq(vid: str) -> int:
        seq_counter[vid] = seq_counter.get(vid, 0) + 1
        return seq_counter[vid]

    for i in range(num_visitors):
        vid = _visitor_id(i)
        entry_ts = base + timedelta(minutes=random.randint(0, 600))
        conf = round(random.uniform(0.75, 0.98), 3)

        # Group entry (20% chance)
        group_id = None
        group_size = None
        if random.random() < 0.2:
            group_size = random.randint(2, 3)
            group_id = f"GRP_{i:04d}"

        events.append(_event(store_id, CAMERAS["entry"], vid, "ENTRY", entry_ts,
                             confidence=conf,
                             metadata={"session_seq": seq(vid), "group_id": group_id,
                                       "group_size": group_size}))

        # Visit 2-4 zones
        t = entry_ts + timedelta(seconds=random.randint(10, 30))
        visited_zones = random.sample(ZONES[:-1], k=random.randint(1, 4))
        for zone_id, sku, cam in visited_zones:
            events.append(_event(store_id, cam, vid, "ZONE_ENTER", t,
                                 zone_id=zone_id,
                                 confidence=conf,
                                 metadata={"session_seq": seq(vid), "sku_zone": sku}))
            dwell = random.randint(15, 300) * 1000
            # Emit ZONE_DWELL every 30s
            elapsed = 0
            while elapsed + 30_000 <= dwell:
                elapsed += 30_000
                dwell_ts = t + timedelta(milliseconds=elapsed)
                events.append(_event(store_id, cam, vid, "ZONE_DWELL", dwell_ts,
                                     zone_id=zone_id, dwell_ms=elapsed,
                                     confidence=conf,
                                     metadata={"session_seq": seq(vid), "sku_zone": sku}))
            t += timedelta(milliseconds=dwell)
            events.append(_event(store_id, cam, vid, "ZONE_EXIT", t,
                                 zone_id=zone_id, dwell_ms=dwell,
                                 confidence=conf,
                                 metadata={"session_seq": seq(vid), "sku_zone": sku}))
            t += timedelta(seconds=random.randint(5, 20))

        # 60% proceed to billing
        if random.random() < 0.60:
            billing_zone = ZONES[-1]
            zone_id, _, cam = billing_zone
            # Prune visitors who already exited billing
            billing_occupants = [(v, ex) for v, ex in billing_occupants if ex > t]
            q_depth = len(billing_occupants)

            # Always emit ZONE_ENTER for the billing area
            events.append(_event(store_id, cam, vid, "ZONE_ENTER", t,
                                 zone_id=zone_id, confidence=conf,
                                 metadata={"session_seq": seq(vid)}))
            # Emit BILLING_QUEUE_JOIN if others are already in billing
            if q_depth > 0:
                events.append(_event(store_id, cam, vid, "BILLING_QUEUE_JOIN", t,
                                     zone_id=zone_id, confidence=conf,
                                     metadata={"session_seq": seq(vid), "queue_depth": q_depth}))
            wait = random.randint(60, 300) * 1000
            exit_t = t + timedelta(milliseconds=wait)
            billing_occupants.append((vid, exit_t))
            t = exit_t

            # 15% abandon
            if random.random() < 0.15:
                events.append(_event(store_id, cam, vid, "BILLING_QUEUE_ABANDON", t,
                                     zone_id=zone_id, dwell_ms=wait,
                                     confidence=conf,
                                     metadata={"session_seq": seq(vid)}))

        # 10% re-entry
        if random.random() < 0.10:
            exit_ts = t + timedelta(seconds=random.randint(30, 120))
            events.append(_event(store_id, CAMERAS["entry"], vid, "EXIT", exit_ts,
                                 confidence=conf,
                                 metadata={"session_seq": seq(vid)}))
            reentry_ts = exit_ts + timedelta(seconds=random.randint(60, 240))
            events.append(_event(store_id, CAMERAS["entry"], vid, "REENTRY", reentry_ts,
                                 confidence=conf,
                                 metadata={"session_seq": seq(vid),
                                           "reentry_gap_seconds": int((reentry_ts - exit_ts).total_seconds())}))
        else:
            exit_ts = t + timedelta(seconds=random.randint(30, 300))
            events.append(_event(store_id, CAMERAS["entry"], vid, "EXIT", exit_ts,
                                 confidence=conf,
                                 metadata={"session_seq": seq(vid)}))

    events.sort(key=lambda e: e["timestamp"])
    return events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", default=STORE_ID)
    parser.add_argument("--date", default="2026-04-10")
    parser.add_argument("--visitors", type=int, default=30)
    parser.add_argument("--out", default="sample_events/events.jsonl")
    args = parser.parse_args()

    events = generate(args.store, args.date, args.visitors)
    with open(args.out, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    print(f"Written {len(events)} events to {args.out}")


if __name__ == "__main__":
    main()
