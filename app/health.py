from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import EventRow
from app.models import HealthResponse, StoreHealth

STALE_THRESHOLD_MINUTES = 10
VERSION = "1.0.0"


def get_health(db: Session) -> HealthResponse:
    now = datetime.now(timezone.utc)

    try:
        # Discover all known stores from events table
        store_rows = (
            db.query(EventRow.store_id, func.max(EventRow.timestamp))
            .group_by(EventRow.store_id)
            .all()
        )
        db_status = "ok"
    except Exception as exc:
        return HealthResponse(
            service="store-intelligence",
            version=VERSION,
            checked_at=now,
            stores=[],
            db_status=f"error: {exc}",
        )

    stores: list[StoreHealth] = []
    for store_id, last_ts in store_rows:
        lag_seconds: float | None = None
        feed_status = "OK"

        if last_ts is not None:
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            lag_seconds = round((now - last_ts).total_seconds(), 1)
            if lag_seconds > STALE_THRESHOLD_MINUTES * 60:
                feed_status = "STALE_FEED"

        stores.append(
            StoreHealth(
                store_id=store_id,
                status="ok",
                last_event_ts=last_ts,
                lag_seconds=lag_seconds,
                feed_status=feed_status,
            )
        )

    return HealthResponse(
        service="store-intelligence",
        version=VERSION,
        checked_at=now,
        stores=stores,
        db_status=db_status,
    )
