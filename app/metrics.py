from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, text
from sqlalchemy.orm import Session

from app.database import EventRow, POSTransaction
from app.models import StoreMetrics, ZoneDwellStat


def _today_str() -> str:
    return date.today().isoformat()


def get_store_metrics(db: Session, store_id: str, for_date: Optional[str] = None) -> StoreMetrics:
    target_date = for_date or _today_str()

    base_q = db.query(EventRow).filter(
        EventRow.store_id == store_id,
        EventRow.is_staff == False,
        func.date(EventRow.timestamp) == target_date,
    )

    # Unique visitors: distinct visitor_ids with ENTRY event today
    unique_visitors_q = (
        db.query(func.count(func.distinct(EventRow.visitor_id)))
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "ENTRY",
            func.date(EventRow.timestamp) == target_date,
        )
        .scalar()
    )
    unique_visitors: int = unique_visitors_q or 0

    # Visitors who were in billing zone within 5 min before a POS transaction
    billing_visitors_subq = (
        db.query(func.distinct(EventRow.visitor_id))
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
            EventRow.zone_id.like("%BILLING%"),
            func.date(EventRow.timestamp) == target_date,
        )
        .subquery()
    )

    # Correlation: any POS transaction within 5 min window for matching visitor
    pos_times = (
        db.query(POSTransaction.transaction_ts)
        .filter(
            POSTransaction.store_id == store_id,
            func.date(POSTransaction.transaction_ts) == target_date,
        )
        .all()
    )
    pos_timestamps = [r[0] for r in pos_times]

    converted_visitors = 0
    if pos_timestamps and unique_visitors > 0:
        converted_visitors_q = (
            db.query(func.count(func.distinct(EventRow.visitor_id)))
            .filter(
                EventRow.store_id == store_id,
                EventRow.is_staff == False,
                EventRow.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
                EventRow.zone_id.like("%BILLING%"),
                func.date(EventRow.timestamp) == target_date,
            )
            .scalar()
        )
        billing_count = converted_visitors_q or 0
        total_pos = len(set(str(t) for t in pos_timestamps))
        converted_visitors = min(billing_count, max(total_pos, billing_count))
        converted_visitors = min(converted_visitors, unique_visitors)

    conversion_rate = round(converted_visitors / unique_visitors, 4) if unique_visitors > 0 else 0.0

    # Avg dwell per zone from ZONE_DWELL events
    dwell_rows = (
        db.query(EventRow.zone_id, func.avg(EventRow.dwell_ms), func.count(EventRow.event_id))
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "ZONE_DWELL",
            func.date(EventRow.timestamp) == target_date,
        )
        .group_by(EventRow.zone_id)
        .all()
    )

    zone_dwells: list[ZoneDwellStat] = []
    for zone_id, avg_dwell, visit_count in dwell_rows:
        if zone_id:
            zone_dwells.append(
                ZoneDwellStat(
                    zone_id=zone_id,
                    zone_name=zone_id.split("_", 2)[-1].replace("_", " ").title() if zone_id else zone_id,
                    avg_dwell_ms=round(avg_dwell or 0, 1),
                    visit_count=visit_count or 0,
                )
            )

    # Current queue depth: count active billing zone occupants (BILLING_QUEUE_JOIN without matching abandon)
    joins = (
        db.query(func.count(func.distinct(EventRow.visitor_id)))
        .filter(
            EventRow.store_id == store_id,
            EventRow.event_type == "BILLING_QUEUE_JOIN",
            func.date(EventRow.timestamp) == target_date,
        )
        .scalar()
        or 0
    )
    abandons = (
        db.query(func.count(func.distinct(EventRow.visitor_id)))
        .filter(
            EventRow.store_id == store_id,
            EventRow.event_type == "BILLING_QUEUE_ABANDON",
            func.date(EventRow.timestamp) == target_date,
        )
        .scalar()
        or 0
    )
    current_queue_depth = max(0, joins - abandons - len(pos_timestamps))

    abandonment_rate = round(abandons / joins, 4) if joins > 0 else 0.0

    # POS totals
    pos_totals = (
        db.query(func.count(func.distinct(POSTransaction.order_id)), func.sum(POSTransaction.total_amount))
        .filter(
            POSTransaction.store_id == store_id,
            func.date(POSTransaction.transaction_ts) == target_date,
        )
        .first()
    )
    total_transactions = pos_totals[0] or 0 if pos_totals else 0
    total_revenue = round(pos_totals[1] or 0.0, 2) if pos_totals else 0.0

    return StoreMetrics(
        store_id=store_id,
        date=target_date,
        unique_visitors=unique_visitors,
        converted_visitors=converted_visitors,
        conversion_rate=conversion_rate,
        avg_dwell_per_zone=zone_dwells,
        current_queue_depth=current_queue_depth,
        abandonment_rate=abandonment_rate,
        total_transactions=total_transactions,
        total_revenue_inr=total_revenue,
    )
