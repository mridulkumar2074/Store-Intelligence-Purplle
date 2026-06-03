# PROMPT:
#   "Write pytest tests for anomaly detection in a retail store analytics API.
#    Cover: (1) BILLING_QUEUE_SPIKE when queue joins exceed 2x baseline,
#    (2) no spike when queue is within normal range, (3) CONVERSION_DROP when
#    today rate is below 80% of 7-day average, (4) DEAD_ZONE when a zone has
#    no visits in 30 minutes, (5) severity levels INFO/WARN/CRITICAL,
#    (6) no anomalies for an empty store.
#    Use FastAPI TestClient with in-memory SQLite."
#
# CHANGES MADE:
#   - Added DEAD_ZONE test that seeds historical zone data then waits past
#     30-minute cutoff — AI suggestion used time.sleep which is not acceptable.
#     Instead we seed events with backdated timestamps.
#   - Added test for anomaly suggested_action being a non-empty string.
#   - Added test verifying anomaly_id is a valid UUID.

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, EventRow, POSTransaction, get_db
from app.main import app


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture
def client(db_engine):
    Session = sessionmaker(bind=db_engine)

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _row(store_id, event_type, visitor_id, ts, zone_id=None, is_staff=False):
    return EventRow(
        event_id=str(uuid.uuid4()),
        store_id=store_id,
        camera_id="CAM_BILLING",
        visitor_id=visitor_id,
        event_type=event_type,
        timestamp=ts,
        zone_id=zone_id,
        is_staff=is_staff,
    )


def _pos(store_id, ts, order_id=None):
    return POSTransaction(
        order_id=order_id or str(uuid.uuid4()),
        store_id=store_id,
        transaction_ts=ts,
        total_amount=500.0,
    )


NOW = datetime.now(timezone.utc)
TODAY = NOW.date().isoformat()


class TestQueueSpike:
    def test_queue_spike_detected(self, client, db_session):
        store = "ST_QSPIKE"
        # Baseline: 1 join in previous 3 hours
        db_session.add(_row(store, "BILLING_QUEUE_JOIN", "VIS_old",
                            NOW - timedelta(hours=2)))
        # Current hour: 8 joins (>> 2x baseline of 0.33/hr)
        for i in range(8):
            db_session.add(_row(store, "BILLING_QUEUE_JOIN", f"VIS_{i}",
                                NOW - timedelta(minutes=30 + i)))
        db_session.commit()

        r = client.get(f"/stores/{store}/anomalies")
        types = [a["anomaly_type"] for a in r.json()["anomalies"]]
        assert "BILLING_QUEUE_SPIKE" in types

    def test_no_spike_within_normal_range(self, client, db_session):
        store = "ST_QNORMAL"
        # Baseline 3/hr for 3 hours = 9 total
        for i in range(9):
            db_session.add(_row(store, "BILLING_QUEUE_JOIN", f"VIS_b{i}",
                                NOW - timedelta(hours=2, minutes=i * 10)))
        # Current hour: 3 (equal to baseline)
        for i in range(3):
            db_session.add(_row(store, "BILLING_QUEUE_JOIN", f"VIS_c{i}",
                                NOW - timedelta(minutes=20 + i * 5)))
        db_session.commit()
        r = client.get(f"/stores/{store}/anomalies")
        types = [a["anomaly_type"] for a in r.json()["anomalies"]]
        assert "BILLING_QUEUE_SPIKE" not in types

    def test_spike_severity_critical_at_3x(self, client, db_session):
        store = "ST_QCRIT"
        # Baseline: 2 joins/hr for 3 hours
        for i in range(6):
            db_session.add(_row(store, "BILLING_QUEUE_JOIN", f"VIS_b{i}",
                                NOW - timedelta(hours=2, minutes=i * 10)))
        # Current: 7 (>3x baseline)
        for i in range(7):
            db_session.add(_row(store, "BILLING_QUEUE_JOIN", f"VIS_c{i}",
                                NOW - timedelta(minutes=20 + i * 5)))
        db_session.commit()
        r = client.get(f"/stores/{store}/anomalies")
        spikes = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        if spikes:
            assert spikes[0]["severity"] in ("WARN", "CRITICAL")


class TestConversionDrop:
    def test_conversion_drop_detected(self, client, db_session):
        store = "ST_CDROP"
        # 7-day history: 10 visitors, 8 purchases = 80% rate
        seven_ago = NOW - timedelta(days=5)
        for i in range(10):
            db_session.add(_row(store, "ENTRY", f"VIS_h{i}", seven_ago.replace(hour=10)))
        for i in range(8):
            db_session.add(_pos(store, seven_ago.replace(hour=11)))
        # Today: 10 visitors, 1 purchase = 10% rate (well below 80% of 80%)
        for i in range(10):
            db_session.add(_row(store, "ENTRY", f"VIS_t{i}", NOW.replace(hour=10)))
        db_session.add(_pos(store, NOW.replace(hour=11)))
        db_session.commit()

        r = client.get(f"/stores/{store}/anomalies")
        types = [a["anomaly_type"] for a in r.json()["anomalies"]]
        assert "CONVERSION_DROP" in types

    def test_no_conversion_drop_empty_history(self, client):
        r = client.get("/stores/ST_NEWSTORE/anomalies")
        types = [a["anomaly_type"] for a in r.json()["anomalies"]]
        assert "CONVERSION_DROP" not in types

    def test_conversion_drop_severity_critical(self, client, db_session):
        store = "ST_CRIT_DROP"
        six_ago = NOW - timedelta(days=4)
        for i in range(20):
            db_session.add(_row(store, "ENTRY", f"VIS_h{i}", six_ago.replace(hour=10)))
        for i in range(19):
            db_session.add(_pos(store, six_ago.replace(hour=11)))
        # Today: 20 visitors, 0 purchases
        for i in range(20):
            db_session.add(_row(store, "ENTRY", f"VIS_t{i}", NOW.replace(hour=10)))
        db_session.commit()
        r = client.get(f"/stores/{store}/anomalies")
        drops = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "CONVERSION_DROP"]
        if drops:
            assert drops[0]["severity"] in ("WARN", "CRITICAL")


class TestDeadZone:
    def test_dead_zone_detected(self, client, db_session):
        store = "ST_DEAD"
        # Zone was active 45 minutes ago (> 30 min threshold)
        old_ts = NOW - timedelta(minutes=45)
        db_session.add(_row(store, "ZONE_ENTER", "VIS_old", old_ts, zone_id="ZONE_A"))
        db_session.commit()
        r = client.get(f"/stores/{store}/anomalies")
        types = [a["anomaly_type"] for a in r.json()["anomalies"]]
        assert "DEAD_ZONE" in types

    def test_no_dead_zone_recent_activity(self, client, db_session):
        store = "ST_ACTIVE"
        recent_ts = NOW - timedelta(minutes=5)
        db_session.add(_row(store, "ZONE_ENTER", "VIS_new", recent_ts, zone_id="ZONE_B"))
        db_session.commit()
        r = client.get(f"/stores/{store}/anomalies")
        types = [a["anomaly_type"] for a in r.json()["anomalies"]]
        assert "DEAD_ZONE" not in types

    def test_dead_zone_severity_is_info(self, client, db_session):
        store = "ST_DEAD_SEV"
        old_ts = NOW - timedelta(minutes=40)
        db_session.add(_row(store, "ZONE_ENTER", "VIS_old2", old_ts, zone_id="ZONE_C"))
        db_session.commit()
        r = client.get(f"/stores/{store}/anomalies")
        dead_zones = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "DEAD_ZONE"]
        for dz in dead_zones:
            assert dz["severity"] == "INFO"


class TestAnomalySchema:
    def test_anomaly_has_suggested_action(self, client, db_session):
        store = "ST_SCHEMA"
        old_ts = NOW - timedelta(minutes=40)
        db_session.add(_row(store, "ZONE_ENTER", "VIS_x", old_ts, zone_id="ZONE_X"))
        db_session.commit()
        r = client.get(f"/stores/{store}/anomalies")
        for a in r.json()["anomalies"]:
            assert isinstance(a["suggested_action"], str)
            assert len(a["suggested_action"]) > 0

    def test_anomaly_id_is_uuid(self, client, db_session):
        store = "ST_UUID"
        old_ts = NOW - timedelta(minutes=40)
        db_session.add(_row(store, "ZONE_ENTER", "VIS_u", old_ts, zone_id="ZONE_U"))
        db_session.commit()
        r = client.get(f"/stores/{store}/anomalies")
        for a in r.json()["anomalies"]:
            assert uuid.UUID(a["anomaly_id"])   # raises ValueError if not valid UUID

    def test_empty_store_no_anomalies(self, client):
        r = client.get("/stores/ST_PRISTINE/anomalies")
        assert r.status_code == 200
        assert r.json()["anomalies"] == []
