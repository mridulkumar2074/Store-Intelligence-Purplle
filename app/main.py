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
