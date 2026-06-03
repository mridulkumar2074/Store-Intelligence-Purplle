from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from pipeline.zone_classifier import Zone

logger = logging.getLogger("pipeline.emit")

BATCH_SIZE = 50
FLUSH_INTERVAL_SECONDS = 5.0
API_TIMEOUT = 10


class EventEmitter:
    def __init__(self, api_url: str, store_id: str) -> None:
        self._api_url = api_url.rstrip("/")
        self._store_id = store_id
        self._buffer: list[dict[str, Any]] = []
        self._last_flush = time.time()

    def emit(self, event: dict[str, Any]) -> None:
        self._buffer.append(event)
        if len(self._buffer) >= BATCH_SIZE or (time.time() - self._last_flush) >= FLUSH_INTERVAL_SECONDS:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        batch = self._buffer[:]
        self._buffer.clear()
        self._last_flush = time.time()
        try:
            resp = requests.post(
                f"{self._api_url}/events/ingest",
                json={"events": batch},
                timeout=API_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json()
            logger.info(
                "flushed %d events → accepted=%d rejected=%d duplicate=%d",
                len(batch),
                result.get("accepted", 0),
                result.get("rejected", 0),
                result.get("duplicate", 0),
            )
        except Exception as exc:
            logger.error("flush failed: %s — %d events dropped", exc, len(batch))

    # ------------------------------------------------------------------ #
    #  Factory helpers                                                     #
    # ------------------------------------------------------------------ #

    def make_entry(
        self,
        visitor_id: str,
        camera_id: str,
        timestamp: datetime,
        confidence: float,
        is_staff: bool,
        session_seq: int,
        group_id: Optional[str] = None,
        group_size: Optional[int] = None,
    ) -> dict[str, Any]:
        return self._base(
            visitor_id=visitor_id,
            camera_id=camera_id,
            event_type="ENTRY",
            timestamp=timestamp,
            confidence=confidence,
            is_staff=is_staff,
            metadata={"session_seq": session_seq, "group_id": group_id, "group_size": group_size},
        )

    def make_exit(
        self,
        visitor_id: str,
        camera_id: str,
        timestamp: datetime,
        confidence: float,
        is_staff: bool,
        session_seq: int,
    ) -> dict[str, Any]:
        return self._base(
            visitor_id=visitor_id,
            camera_id=camera_id,
            event_type="EXIT",
            timestamp=timestamp,
            confidence=confidence,
            is_staff=is_staff,
            metadata={"session_seq": session_seq},
        )

    def make_reentry(
        self,
        visitor_id: str,
        camera_id: str,
        timestamp: datetime,
        confidence: float,
        gap_seconds: int,
        session_seq: int,
    ) -> dict[str, Any]:
        return self._base(
            visitor_id=visitor_id,
            camera_id=camera_id,
            event_type="REENTRY",
            timestamp=timestamp,
            confidence=confidence,
            is_staff=False,
            metadata={"session_seq": session_seq, "reentry_gap_seconds": gap_seconds},
        )

    def make_zone_enter(
        self,
        visitor_id: str,
        camera_id: str,
        timestamp: datetime,
        zone: Zone,
        confidence: float,
        is_staff: bool,
        session_seq: int,
    ) -> dict[str, Any]:
        return self._base(
            visitor_id=visitor_id,
            camera_id=camera_id,
            event_type="ZONE_ENTER",
            timestamp=timestamp,
            zone_id=zone.zone_id,
            confidence=confidence,
            is_staff=is_staff,
            metadata={"session_seq": session_seq, "sku_zone": zone.sku_zone},
        )

    def make_zone_exit(
        self,
        visitor_id: str,
        camera_id: str,
        timestamp: datetime,
        zone: Zone,
        dwell_ms: int,
        confidence: float,
        is_staff: bool,
        session_seq: int,
    ) -> dict[str, Any]:
        return self._base(
            visitor_id=visitor_id,
            camera_id=camera_id,
            event_type="ZONE_EXIT",
            timestamp=timestamp,
            zone_id=zone.zone_id,
            dwell_ms=dwell_ms,
            confidence=confidence,
            is_staff=is_staff,
            metadata={"session_seq": session_seq, "sku_zone": zone.sku_zone},
        )

    def make_zone_dwell(
        self,
        visitor_id: str,
        camera_id: str,
        timestamp: datetime,
        zone: Zone,
        dwell_ms: int,
        confidence: float,
        is_staff: bool,
        session_seq: int,
    ) -> dict[str, Any]:
        return self._base(
            visitor_id=visitor_id,
            camera_id=camera_id,
            event_type="ZONE_DWELL",
            timestamp=timestamp,
            zone_id=zone.zone_id,
            dwell_ms=dwell_ms,
            confidence=confidence,
            is_staff=is_staff,
            metadata={"session_seq": session_seq, "sku_zone": zone.sku_zone},
        )

    def make_billing_queue_join(
        self,
        visitor_id: str,
        camera_id: str,
        timestamp: datetime,
        zone: Zone,
        queue_depth: int,
        confidence: float,
        session_seq: int,
    ) -> dict[str, Any]:
        return self._base(
            visitor_id=visitor_id,
            camera_id=camera_id,
            event_type="BILLING_QUEUE_JOIN",
            timestamp=timestamp,
            zone_id=zone.zone_id,
            confidence=confidence,
            is_staff=False,
            metadata={"session_seq": session_seq, "queue_depth": queue_depth, "sku_zone": zone.sku_zone},
        )

    def make_billing_queue_abandon(
        self,
        visitor_id: str,
        camera_id: str,
        timestamp: datetime,
        zone: Zone,
        wait_ms: int,
        confidence: float,
        session_seq: int,
    ) -> dict[str, Any]:
        return self._base(
            visitor_id=visitor_id,
            camera_id=camera_id,
            event_type="BILLING_QUEUE_ABANDON",
            timestamp=timestamp,
            zone_id=zone.zone_id,
            dwell_ms=wait_ms,
            confidence=confidence,
            is_staff=False,
            metadata={"session_seq": session_seq},
        )

    def _base(
        self,
        visitor_id: str,
        camera_id: str,
        event_type: str,
        timestamp: datetime,
        confidence: float = 1.0,
        is_staff: bool = False,
        zone_id: Optional[str] = None,
        dwell_ms: int = 0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "event_id": str(uuid.uuid4()),
            "store_id": self._store_id,
            "camera_id": camera_id,
            "visitor_id": visitor_id,
            "event_type": event_type,
            "timestamp": timestamp.isoformat(),
            "zone_id": zone_id,
            "dwell_ms": dwell_ms,
            "is_staff": is_staff,
            "confidence": round(max(0.0, min(1.0, confidence)), 3),
            "metadata": metadata or {},
        }
