from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./store_intelligence.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    echo=False,
)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class EventRow(Base):
    __tablename__ = "events"

    event_id = Column(String(36), primary_key=True)
    store_id = Column(String(32), nullable=False, index=True)
    camera_id = Column(String(64), nullable=False)
    visitor_id = Column(String(64), nullable=False, index=True)
    event_type = Column(String(32), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    zone_id = Column(String(64), nullable=True)
    dwell_ms = Column(Integer, default=0)
    is_staff = Column(Boolean, default=False)
    confidence = Column(Float, default=1.0)
    queue_depth = Column(Integer, nullable=True)
    sku_zone = Column(String(64), nullable=True)
    session_seq = Column(Integer, nullable=True)
    group_id = Column(String(64), nullable=True)
    group_size = Column(Integer, nullable=True)
    reentry_gap_seconds = Column(Integer, nullable=True)
    ingested_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class POSTransaction(Base):
    __tablename__ = "pos_transactions"

    row_id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(32), nullable=False, index=True)
    store_id = Column(String(32), nullable=False, index=True)
    transaction_ts = Column(DateTime, nullable=False, index=True)
    product_id = Column(String(32), nullable=True)
    brand_name = Column(String(128), nullable=True)
    total_amount = Column(Float, default=0.0)


def create_tables() -> None:
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
