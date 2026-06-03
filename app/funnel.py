from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import EventRow, POSTransaction
from app.models import FunnelStage, StoreFunnel


def get_funnel(db: Session, store_id: str, for_date: Optional[str] = None) -> StoreFunnel:
    from datetime import date as date_type
    target_date = for_date or date_type.today().isoformat()

    # Unique sessions = distinct visitor_ids that had at least one ENTRY event today.
    # REENTRY events use the same visitor_id, so they naturally do not inflate this count.
    entry_sessions: set[str] = set(
        r[0]
        for r in db.query(EventRow.visitor_id)
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "ENTRY",
            func.date(EventRow.timestamp) == target_date,
        )
        .distinct()
        .all()
    )
    total_sessions = len(entry_sessions)

    # Stage 2: sessions that visited at least one zone
    zone_sessions: set[str] = set(
        r[0]
        for r in db.query(EventRow.visitor_id)
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "ZONE_ENTER",
            func.date(EventRow.timestamp) == target_date,
        )
        .distinct()
        .all()
    )
    zone_sessions &= entry_sessions  # only count those who had an ENTRY

    # Stage 3: sessions that reached the billing area.
    # A session counts if it has a BILLING_QUEUE_JOIN event (queue was present)
    # OR a ZONE_ENTER for any billing-type zone (first in line, no queue event).
    billing_queue_sessions: set[str] = set(
        r[0]
        for r in db.query(EventRow.visitor_id)
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "BILLING_QUEUE_JOIN",
            func.date(EventRow.timestamp) == target_date,
        )
        .distinct()
        .all()
    )
    billing_zone_sessions: set[str] = set(
        r[0]
        for r in db.query(EventRow.visitor_id)
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "ZONE_ENTER",
            EventRow.zone_id.like("%BILLING%"),
            func.date(EventRow.timestamp) == target_date,
        )
        .distinct()
        .all()
    )
    billing_sessions = (billing_queue_sessions | billing_zone_sessions) & entry_sessions

    # Stage 4: purchased — correlated via POS transactions
    # Count distinct transaction timestamps (each unique ts = one checkout event)
    pos_count = (
        db.query(func.count(func.distinct(POSTransaction.order_id)))
        .filter(
            POSTransaction.store_id == store_id,
            func.date(POSTransaction.transaction_ts) == target_date,
        )
        .scalar()
        or 0
    )
    purchase_count = min(pos_count, len(billing_sessions) if billing_sessions else pos_count)
    purchase_count = min(purchase_count, total_sessions)

    def drop_off(current: int, previous: int) -> float:
        if previous == 0:
            return 0.0
        return round((1 - current / previous) * 100, 1)

    now = datetime.now(timezone.utc)
    window_start = f"{target_date}T00:00:00Z"
    window_end = f"{target_date}T23:59:59Z"

    stages: list[FunnelStage] = [
        FunnelStage(stage="ENTRY", count=total_sessions, drop_off_pct=0.0),
        FunnelStage(
            stage="ZONE_VISIT",
            count=len(zone_sessions),
            drop_off_pct=drop_off(len(zone_sessions), total_sessions),
        ),
        FunnelStage(
            stage="BILLING_QUEUE",
            count=len(billing_sessions),
            drop_off_pct=drop_off(len(billing_sessions), len(zone_sessions)),
        ),
        FunnelStage(
            stage="PURCHASE",
            count=purchase_count,
            drop_off_pct=drop_off(purchase_count, len(billing_sessions)),
        ),
    ]

    return StoreFunnel(
        store_id=store_id,
        window_start=window_start,
        window_end=window_end,
        stages=stages,
        total_sessions=total_sessions,
    )
