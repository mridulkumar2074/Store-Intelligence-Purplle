from __future__ import annotations

import csv
import io
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.anomalies import get_anomalies
from app.database import POSTransaction, create_tables, get_db
from app.funnel import get_funnel
from app.health import get_health
from app.heatmap import get_heatmap
from app.ingestion import ingest_events
from app.metrics import get_store_metrics
from app.models import IngestRequest, IngestResult
from app.telemetry import RequestLoggingMiddleware, configure_logging, log_ingest


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    create_tables()
    _load_pos_data()
    _load_sample_events()
    yield


def _load_pos_data() -> None:
    pos_path = os.getenv("POS_CSV", "data/pos_transactions.csv")
    if not os.path.exists(pos_path):
        return
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        existing = db.query(POSTransaction).count()
        if existing > 0:
            return
        with open(pos_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    date_str = row["order_date"].strip()
                    time_str = row["order_time"].strip()
                    ts = datetime.strptime(f"{date_str} {time_str}", "%d-%m-%Y %H:%M:%S")
                    db.add(
                        POSTransaction(
                            order_id=str(row["order_id"]).strip(),
                            store_id=row["store_id"].strip(),
                            transaction_ts=ts,
                            product_id=str(row.get("product_id", "")).strip() or None,
                            brand_name=row.get("brand_name", "").strip() or None,
                            total_amount=float(row.get("total_amount", 0) or 0),
                        )
                    )
                except Exception:
                    continue
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _load_sample_events() -> None:
    events_path = os.getenv("SAMPLE_EVENTS", "sample_events/events.jsonl")
    if not os.path.exists(events_path):
        return
    import json as _json
    import uuid as _uuid
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from app.database import SessionLocal
    from app.models import IngestRequest, StoreEvent
    db = SessionLocal()
    try:
        from app.database import EventRow
        if db.query(EventRow).count() > 0:
            return
        raw = []
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        raw.append(_json.loads(line))
                    except Exception:
                        pass
        if not raw:
            return

        # Shift all event dates so the latest event falls on today (UTC).
        # This keeps the deployed demo "live" no matter when it (re)starts —
        # Render's free tier has ephemeral storage and reloads on every cold start.
        def _parse(ts: str) -> _dt:
            return _dt.fromisoformat(ts.replace("Z", "+00:00"))

        max_ts = max(_parse(e["timestamp"]) for e in raw)
        today_utc = _dt.now(_tz.utc).date()
        day_offset = (today_utc - max_ts.date()).days

        events = []
        for e in raw:
            try:
                shifted = _parse(e["timestamp"]) + _td(days=day_offset)
                e["timestamp"] = shifted.isoformat()
                e["event_id"] = str(_uuid.uuid4())  # fresh ids to avoid stale collisions
                events.append(StoreEvent.model_validate(e))
            except Exception:
                pass

        if events:
            from app.ingestion import ingest_events
            for i in range(0, len(events), 500):
                ingest_events(db, IngestRequest(events=events[i:i + 500]))
    except Exception:
        pass
    finally:
        db.close()


app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0",
    description="Real-time retail store analytics from CCTV event streams",
    lifespan=lifespan,
)
app.add_middleware(RequestLoggingMiddleware)


@app.exception_handler(OperationalError)
async def db_unavailable_handler(request: Request, exc: OperationalError):
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"error": "database_unavailable", "detail": "Database is temporarily unavailable."},
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_error", "detail": "An unexpected error occurred."},
    )


# ---------------------------------------------------------------------------
# POST /events/ingest
# ---------------------------------------------------------------------------

@app.post("/events/ingest", response_model=IngestResult, status_code=status.HTTP_200_OK)
def events_ingest(payload: IngestRequest, request: Request, db: Session = Depends(get_db)):
    trace_id = getattr(request.state, "trace_id", "unknown")
    result = ingest_events(db, payload)
    stores = {e.store_id for e in payload.events}
    store_id = next(iter(stores)) if len(stores) == 1 else None
    log_ingest(trace_id, store_id, len(payload.events), 200)
    return result


# ---------------------------------------------------------------------------
# GET /stores/{store_id}/metrics
# ---------------------------------------------------------------------------

@app.get("/stores/{store_id}/metrics")
def store_metrics(
    store_id: str,
    date: Optional[str] = Query(None, description="ISO date YYYY-MM-DD; defaults to today"),
    db: Session = Depends(get_db),
):
    return get_store_metrics(db, store_id, date)


# ---------------------------------------------------------------------------
# GET /stores/{store_id}/funnel
# ---------------------------------------------------------------------------

@app.get("/stores/{store_id}/funnel")
def store_funnel(
    store_id: str,
    date: Optional[str] = Query(None, description="ISO date YYYY-MM-DD; defaults to today"),
    db: Session = Depends(get_db),
):
    return get_funnel(db, store_id, date)


# ---------------------------------------------------------------------------
# GET /stores/{store_id}/heatmap
# ---------------------------------------------------------------------------

@app.get("/stores/{store_id}/heatmap")
def store_heatmap(
    store_id: str,
    window_hours: int = Query(24, ge=1, le=168),
    db: Session = Depends(get_db),
):
    return get_heatmap(db, store_id, window_hours)


# ---------------------------------------------------------------------------
# GET /stores/{store_id}/anomalies
# ---------------------------------------------------------------------------

@app.get("/stores/{store_id}/anomalies")
def store_anomalies(store_id: str, db: Session = Depends(get_db)):
    return get_anomalies(db, store_id)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get("/health")
def health(db: Session = Depends(get_db)):
    return get_health(db)
