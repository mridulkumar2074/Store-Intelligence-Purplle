from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import EventRow, POSTransaction
from app.models import Anomaly, AnomalySeverity, AnomalyType, StoreAnomalies

# Tuning constants
QUEUE_SPIKE_MULTIPLIER = 2.0   # current > 2x baseline = spike
QUEUE_SPIKE_ABS_MIN = 4        # absolute threshold to avoid noise
CONVERSION_DROP_THRESHOLD = 0.80  # today < 80% of 7-day avg
DEAD_ZONE_MINUTES = 30
STALE_FEED_MINUTES = 10


def get_anomalies(db: Session, store_id: str) -> StoreAnomalies:
    now = datetime.now(timezone.utc)
    anomalies: list[Anomaly] = []

    anomalies += _check_queue_spike(db, store_id, now)
    anomalies += _check_conversion_drop(db, store_id, now)
    anomalies += _check_dead_zones(db, store_id, now)

    return StoreAnomalies(store_id=store_id, checked_at=now, anomalies=anomalies)


def _check_queue_spike(db: Session, store_id: str, now: datetime) -> list[Anomaly]:
    # Current queue depth
    one_hour_ago = now - timedelta(hours=1)
    joins_recent = (
        db.query(func.count(func.distinct(EventRow.visitor_id)))
        .filter(
            EventRow.store_id == store_id,
            EventRow.event_type == "BILLING_QUEUE_JOIN",
            EventRow.timestamp >= one_hour_ago,
        )
        .scalar()
        or 0
    )
    # Baseline: avg queue joins over previous 3 hours
    three_hours_ago = now - timedelta(hours=4)
    joins_baseline = (
        db.query(func.count(EventRow.event_id))
        .filter(
            EventRow.store_id == store_id,
            EventRow.event_type == "BILLING_QUEUE_JOIN",
            EventRow.timestamp >= three_hours_ago,
            EventRow.timestamp < one_hour_ago,
        )
        .scalar()
        or 0
    )
    baseline_per_hour = joins_baseline / 3.0 if joins_baseline else 0

    if joins_recent >= QUEUE_SPIKE_ABS_MIN and (
        baseline_per_hour == 0 or joins_recent >= QUEUE_SPIKE_MULTIPLIER * baseline_per_hour
    ):
        severity = AnomalySeverity.CRITICAL if joins_recent > baseline_per_hour * 3 else AnomalySeverity.WARN
        return [
            Anomaly(
                anomaly_id=str(uuid.uuid4()),
                anomaly_type=AnomalyType.BILLING_QUEUE_SPIKE,
                severity=severity,
                description=(
                    f"Billing queue depth {joins_recent} in last hour vs "
                    f"baseline {baseline_per_hour:.1f}/hr"
                ),
                suggested_action="Open an additional billing counter or call staff to billing area immediately.",
                detected_at=now,
                metadata={
                    "current_queue_joins_last_hour": joins_recent,
                    "baseline_per_hour": round(baseline_per_hour, 1),
                },
            )
        ]
    return []


def _check_conversion_drop(db: Session, store_id: str, now: datetime) -> list[Anomaly]:
    today_str = now.date().isoformat()

    today_visitors = (
        db.query(func.count(func.distinct(EventRow.visitor_id)))
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "ENTRY",
            func.date(EventRow.timestamp) == today_str,
        )
        .scalar()
        or 0
    )
    if today_visitors == 0:
        return []

    today_pos = (
        db.query(func.count(func.distinct(POSTransaction.order_id)))
        .filter(
            POSTransaction.store_id == store_id,
            func.date(POSTransaction.transaction_ts) == today_str,
        )
        .scalar()
        or 0
    )
    today_rate = today_pos / today_visitors if today_visitors else 0.0

    # 7-day average (excluding today)
    seven_days_ago = now.date() - timedelta(days=7)
    hist_visitors = (
        db.query(func.count(func.distinct(EventRow.visitor_id)))
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "ENTRY",
            func.date(EventRow.timestamp) > seven_days_ago.isoformat(),
            func.date(EventRow.timestamp) < today_str,
        )
        .scalar()
        or 0
    )
    hist_pos = (
        db.query(func.count(func.distinct(POSTransaction.order_id)))
        .filter(
            POSTransaction.store_id == store_id,
            func.date(POSTransaction.transaction_ts) > seven_days_ago.isoformat(),
            func.date(POSTransaction.transaction_ts) < today_str,
        )
        .scalar()
        or 0
    )
    avg_rate = hist_pos / hist_visitors if hist_visitors > 0 else None

    if avg_rate is not None and avg_rate > 0 and today_rate < avg_rate * CONVERSION_DROP_THRESHOLD:
        drop_pct = round((1 - today_rate / avg_rate) * 100, 1)
        severity = AnomalySeverity.CRITICAL if drop_pct >= 40 else AnomalySeverity.WARN
        return [
            Anomaly(
                anomaly_id=str(uuid.uuid4()),
                anomaly_type=AnomalyType.CONVERSION_DROP,
                severity=severity,
                description=(
                    f"Conversion rate {today_rate:.1%} is {drop_pct}% below "
                    f"7-day average {avg_rate:.1%}"
                ),
                suggested_action=(
                    "Review zone traffic heatmap for dead zones. "
                    "Check if billing queue abandonment is elevated."
                ),
                detected_at=now,
                metadata={
                    "today_rate": round(today_rate, 4),
                    "seven_day_avg_rate": round(avg_rate, 4),
                    "drop_pct": drop_pct,
                },
            )
        ]
    return []


def _check_dead_zones(db: Session, store_id: str, now: datetime) -> list[Anomaly]:
    cutoff = now - timedelta(minutes=DEAD_ZONE_MINUTES)

    # Zones with any recent activity
    active_zones = set(
        r[0]
        for r in db.query(EventRow.zone_id)
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "ZONE_ENTER",
            EventRow.zone_id.isnot(None),
            EventRow.timestamp >= cutoff,
        )
        .distinct()
        .all()
    )

    # All zones that ever received traffic today
    all_zones = set(
        r[0]
        for r in db.query(EventRow.zone_id)
        .filter(
            EventRow.store_id == store_id,
            EventRow.is_staff == False,
            EventRow.event_type == "ZONE_ENTER",
            EventRow.zone_id.isnot(None),
            func.date(EventRow.timestamp) == now.date().isoformat(),
        )
        .distinct()
        .all()
    )

    dead_zones = all_zones - active_zones
    anomalies: list[Anomaly] = []
    for zone_id in sorted(dead_zones):
        anomalies.append(
            Anomaly(
                anomaly_id=str(uuid.uuid4()),
                anomaly_type=AnomalyType.DEAD_ZONE,
                severity=AnomalySeverity.INFO,
                description=f"Zone {zone_id} has had no customer visits in the last {DEAD_ZONE_MINUTES} minutes.",
                suggested_action=(
                    "Check camera feed for this zone. "
                    "Consider staff re-merchandising or a promotional display to drive traffic."
                ),
                detected_at=now,
                metadata={"zone_id": zone_id, "dead_minutes": DEAD_ZONE_MINUTES},
            )
        )
    return anomalies
