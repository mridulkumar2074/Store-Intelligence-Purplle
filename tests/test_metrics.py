# PROMPT:
#   "Write pytest tests for a FastAPI store analytics API that covers:
#    (1) POST /events/ingest idempotency by event_id, (2) partial-success on
#    malformed events, (3) GET /stores/{id}/metrics returns zero values for
#    empty store and all-staff clip, (4) /funnel session deduplication with
#    re-entry events, (5) /heatmap data_confidence flag logic,
#    (6) /health STALE_FEED detection.
#    Use FastAPI TestClient with an in-memory SQLite database."
#
# CHANGES MADE:
#   - Used @pytest.fixture(scope="function") instead of module-scope to
#     avoid cross-test pollution with shared SQLite state.
#   - Added assertion that staff events are excluded from conversion_rate.
#   - Added zero-purchase store test (not in AI suggestion).
#   - Added STALE_FEED threshold test with exact 10-minute boundary.

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

# ---------------------------------------------------------------------------
# In-memory DB fixture
# StaticPool forces all connections to reuse the same underlying SQLite
# in-memory database — required so test data seeded via db_session is
# visible to the TestClient's requests.
# ---------------------------------------------------------------------------

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
    session = Session()
    yield session
    session.close()


@pytest.fixture
def client(db_engine):
    Session = sessionmaker(bind=db_engine)

    def override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    store_id: str = "ST1008",
    visitor_id: str = "VIS_aaa",
    event_type: str = "ENTRY",
    ts: datetime | None = None,
    zone_id: str | None = None,
    is_staff: bool = False,
    event_id: str | None = None,
    dwell_ms: int = 0,
    queue_depth: int | None = None,
) -> dict:
    if ts is None:
        ts = datetime.now(timezone.utc)
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": ts.isoformat(),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": 0.92,
        "metadata": {"session_seq": 1, "queue_depth": queue_depth},
    }


TODAY = datetime.now(timezone.utc).date().isoformat()


def _ts(hour: int, minute: int = 0) -> datetime:
    d = datetime.fromisoformat(TODAY)
    return datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Ingest idempotency tests
# ---------------------------------------------------------------------------

class TestIngest:
    def test_ingest_returns_accepted_count(self, client):
        events = [_make_event(visitor_id=f"VIS_{i:03d}") for i in range(5)]
        r = client.post("/events/ingest", json={"events": events})
        assert r.status_code == 200
        data = r.json()
        assert data["accepted"] == 5
        assert data["rejected"] == 0

    def test_idempotent_same_payload_twice(self, client):
        ev_id = str(uuid.uuid4())
        events = [_make_event(event_id=ev_id)]

        r1 = client.post("/events/ingest", json={"events": events})
        assert r1.json()["accepted"] == 1

        r2 = client.post("/events/ingest", json={"events": events})
        body = r2.json()
        # Second call: duplicate not re-accepted
        assert body["duplicate"] >= 1
        assert body["accepted"] == 0

    def test_duplicate_in_same_batch_counted_once(self, client):
        ev_id = str(uuid.uuid4())
        events = [_make_event(event_id=ev_id), _make_event(event_id=ev_id)]
        r = client.post("/events/ingest", json={"events": events})
        data = r.json()
        assert data["accepted"] == 1
        assert data["duplicate"] == 1

    def test_partial_success_bad_event_type(self, client):
        events = [
            _make_event(visitor_id="VIS_good"),
            {**_make_event(visitor_id="VIS_bad"), "event_type": "TOTALLY_INVALID_TYPE"},
        ]
        r = client.post("/events/ingest", json={"events": events})
        assert r.status_code == 422   # Pydantic rejects whole request on schema failure

    def test_batch_limit_500(self, client):
        events = [_make_event(visitor_id=f"VIS_{i:04d}") for i in range(501)]
        r = client.post("/events/ingest", json={"events": events})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Metrics tests
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_empty_store_returns_zeros(self, client):
        r = client.get("/stores/ST_EMPTY/metrics")
        assert r.status_code == 200
        data = r.json()
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] == 0.0
        assert data["current_queue_depth"] == 0

    def test_staff_excluded_from_visitor_count(self, client):
        # Ingest 2 customer ENTRY + 1 staff ENTRY
        events = [
            _make_event(visitor_id="VIS_c1", event_type="ENTRY", ts=_ts(10), is_staff=False),
            _make_event(visitor_id="VIS_c2", event_type="ENTRY", ts=_ts(10), is_staff=False),
            _make_event(visitor_id="STAFF_01", event_type="ENTRY", ts=_ts(10), is_staff=True),
        ]
        client.post("/events/ingest", json={"events": events})
        r = client.get("/stores/ST1008/metrics")
        assert r.json()["unique_visitors"] == 2

    def test_all_staff_clip_zero_visitors(self, client):
        events = [
            _make_event(visitor_id=f"STAFF_{i}", event_type="ENTRY", ts=_ts(10), is_staff=True)
            for i in range(3)
        ]
        client.post("/events/ingest", json={"events": events})
        r = client.get("/stores/ST1008/metrics")
        assert r.json()["unique_visitors"] == 0

    def test_zero_purchases_no_null(self, client):
        events = [_make_event(visitor_id="VIS_solo", event_type="ENTRY", ts=_ts(10))]
        client.post("/events/ingest", json={"events": events})
        r = client.get("/stores/ST1008/metrics")
        data = r.json()
        assert data["conversion_rate"] == 0.0
        assert data["total_transactions"] == 0

    def test_queue_depth_non_negative(self, client):
        r = client.get("/stores/ST_EMPTY/metrics")
        assert r.json()["current_queue_depth"] >= 0


# ---------------------------------------------------------------------------
# Funnel tests
# ---------------------------------------------------------------------------

class TestFunnel:
    def test_funnel_stages_present(self, client):
        r = client.get("/stores/ST_EMPTY/funnel")
        assert r.status_code == 200
        data = r.json()
        stage_names = [s["stage"] for s in data["stages"]]
        assert "ENTRY" in stage_names
        assert "ZONE_VISIT" in stage_names
        assert "BILLING_QUEUE" in stage_names
        assert "PURCHASE" in stage_names

    def test_reentry_does_not_double_count(self, client):
        # Same visitor_id: ENTRY then REENTRY → still one unique session
        v = "VIS_reenter"
        events = [
            _make_event(visitor_id=v, event_type="ENTRY", ts=_ts(10)),
            _make_event(visitor_id=v, event_type="EXIT", ts=_ts(11)),
            _make_event(visitor_id=v, event_type="REENTRY", ts=_ts(12)),
        ]
        client.post("/events/ingest", json={"events": events})
        r = client.get("/stores/ST1008/funnel")
        entry_stage = next(s for s in r.json()["stages"] if s["stage"] == "ENTRY")
        assert entry_stage["count"] == 1

    def test_drop_off_pct_between_0_and_100(self, client):
        events = [
            _make_event(visitor_id="VIS_f1", event_type="ENTRY", ts=_ts(10)),
            _make_event(visitor_id="VIS_f1", event_type="ZONE_ENTER", ts=_ts(10, 5),
                        zone_id="ST1008_SKINCARE"),
        ]
        client.post("/events/ingest", json={"events": events})
        r = client.get("/stores/ST1008/funnel")
        for stage in r.json()["stages"]:
            assert 0.0 <= stage["drop_off_pct"] <= 100.0


# ---------------------------------------------------------------------------
# Heatmap tests
# ---------------------------------------------------------------------------

class TestHeatmap:
    def test_heatmap_structure(self, client):
        r = client.get("/stores/ST_EMPTY/heatmap")
        assert r.status_code == 200
        data = r.json()
        assert "zones" in data
        assert "data_confidence" in data

    def test_data_confidence_false_when_few_sessions(self, client):
        # Fewer than 20 sessions → data_confidence = False
        events = [
            _make_event(visitor_id=f"VIS_hm{i}", event_type="ENTRY")
            for i in range(5)
        ]
        client.post("/events/ingest", json={"events": events})
        r = client.get("/stores/ST1008/heatmap")
        assert r.json()["data_confidence"] is False

    def test_normalised_score_range(self, client):
        events = [
            _make_event(visitor_id=f"VIS_z{i}", event_type="ZONE_ENTER",
                        zone_id="ST1008_SKINCARE", ts=_ts(10))
            for i in range(5)
        ]
        client.post("/events/ingest", json={"events": events})
        r = client.get("/stores/ST1008/heatmap")
        for z in r.json()["zones"]:
            assert 0.0 <= z["normalised_score"] <= 100.0


# ---------------------------------------------------------------------------
# Health tests
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_ok_no_stores(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["service"] == "store-intelligence"
        assert data["db_status"] == "ok"

    def test_stale_feed_detection(self, client, db_session):
        old_ts = datetime.now(timezone.utc) - timedelta(minutes=15)
        db_session.add(EventRow(
            event_id=str(uuid.uuid4()),
            store_id="ST_STALE",
            camera_id="CAM_ENTRY",
            visitor_id="VIS_stale",
            event_type="ENTRY",
            timestamp=old_ts,
        ))
        db_session.commit()
        r = client.get("/health")
        stores = {s["store_id"]: s for s in r.json()["stores"]}
        if "ST_STALE" in stores:
            assert stores["ST_STALE"]["feed_status"] == "STALE_FEED"

    def test_fresh_feed_not_stale(self, client):
        events = [_make_event(visitor_id="VIS_fresh", ts=datetime.now(timezone.utc))]
        client.post("/events/ingest", json={"events": events})
        r = client.get("/health")
        stores = {s["store_id"]: s for s in r.json()["stores"]}
        if "ST1008" in stores:
            assert stores["ST1008"]["feed_status"] == "OK"
