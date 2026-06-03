from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.database import EventRow
from app.models import HeatmapZone, StoreHeatmap

# Zone display name lookup (fallback to zone_id suffix)
_ZONE_NAMES: dict[str, str] = {}


def get_heatmap(db: Session, store_id: str, window_hours: int = 24) -> StoreHeatmap:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    # Count unique visitor sessions in window for confidence check
    session_count = (
        db.query(func.count(func.distinct(EventRow.visitor_id)))
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "ENTRY",
            EventRow.timestamp >= cutoff,
        )
        .scalar()
        or 0
    )
    data_confidence = session_count >= 20

    # Zone visit frequency from ZONE_ENTER events
    enter_rows = (
        db.query(EventRow.zone_id, func.count(EventRow.event_id))
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "ZONE_ENTER",
            EventRow.zone_id.isnot(None),
            EventRow.timestamp >= cutoff,
        )
        .group_by(EventRow.zone_id)
        .all()
    )

    # Avg dwell per zone from ZONE_DWELL events
    dwell_rows = (
        db.query(EventRow.zone_id, func.avg(EventRow.dwell_ms))
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "ZONE_DWELL",
            EventRow.zone_id.isnot(None),
            EventRow.timestamp >= cutoff,
        )
        .group_by(EventRow.zone_id)
        .all()
    )

    # Zone types
    zone_type_rows = (
        db.query(EventRow.zone_id, func.max(EventRow.sku_zone))
        .filter(
            EventRow.store_id == store_id,
            EventRow.zone_id.isnot(None),
            EventRow.timestamp >= cutoff,
        )
        .group_by(EventRow.zone_id)
        .all()
    )

    freq_map: dict[str, int] = {r[0]: r[1] for r in enter_rows}
    dwell_map: dict[str, float] = {r[0]: float(r[1] or 0) for r in dwell_rows}
    type_map: dict[str, str] = {r[0]: (r[1] or "SHELF") for r in zone_type_rows}

    all_zones = set(freq_map) | set(dwell_map)
    if not all_zones:
        return StoreHeatmap(
            store_id=store_id,
            window_hours=window_hours,
            data_confidence=data_confidence,
            zones=[],
        )

    max_freq = max(freq_map.values(), default=1) or 1
    max_dwell = max(dwell_map.values(), default=1) or 1

    zones: list[HeatmapZone] = []
    for zone_id in sorted(all_zones):
        freq = freq_map.get(zone_id, 0)
        avg_dwell = dwell_map.get(zone_id, 0.0)
        # Combined normalised score: 60% visit frequency, 40% dwell
        norm = round(((freq / max_freq) * 60 + (avg_dwell / max_dwell) * 40), 1)
        zone_name = _friendly_name(zone_id)
        zones.append(
            HeatmapZone(
                zone_id=zone_id,
                zone_name=zone_name,
                zone_type=type_map.get(zone_id, "SHELF"),
                visit_frequency=freq,
                avg_dwell_ms=round(avg_dwell, 1),
                normalised_score=norm,
            )
        )

    zones.sort(key=lambda z: z.normalised_score, reverse=True)
    return StoreHeatmap(
        store_id=store_id,
        window_hours=window_hours,
        data_confidence=data_confidence,
        zones=zones,
    )


def _friendly_name(zone_id: str) -> str:
    if zone_id in _ZONE_NAMES:
        return _ZONE_NAMES[zone_id]
    parts = zone_id.rsplit("_", 1)
    return parts[-1].replace("_", " ").title()
