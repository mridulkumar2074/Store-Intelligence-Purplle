from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import EventRow
from app.models import IngestRequest, IngestResult, StoreEvent


def _to_row(ev: StoreEvent) -> EventRow:
    meta = ev.metadata
    return EventRow(
        event_id=str(ev.event_id),
        store_id=ev.store_id,
        camera_id=ev.camera_id,
        visitor_id=ev.visitor_id,
        event_type=ev.event_type.value,
        timestamp=ev.timestamp,
        zone_id=ev.zone_id,
        dwell_ms=ev.dwell_ms,
        is_staff=ev.is_staff,
        confidence=ev.confidence,
        queue_depth=meta.queue_depth,
        sku_zone=meta.sku_zone,
        session_seq=meta.session_seq,
        group_id=meta.group_id,
        group_size=meta.group_size,
        reentry_gap_seconds=meta.reentry_gap_seconds,
    )


def ingest_events(db: Session, payload: IngestRequest) -> IngestResult:
    accepted = rejected = duplicate = 0
    errors: list[dict[str, Any]] = []

    seen_ids: set[str] = set()

    for ev in payload.events:
        ev_id = str(ev.event_id)
        if ev_id in seen_ids:
            duplicate += 1
            continue
        seen_ids.add(ev_id)

        try:
            row = _to_row(ev)
            db.add(row)
            db.flush()
            accepted += 1
        except IntegrityError:
            db.rollback()
            duplicate += 1
        except Exception as exc:
            db.rollback()
            rejected += 1
            errors.append({"event_id": ev_id, "error": str(exc)})

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        rejected += accepted
        accepted = 0
        errors.append({"error": f"commit failed: {exc}"})

    return IngestResult(accepted=accepted, rejected=rejected, duplicate=duplicate, errors=errors)
