from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from typing import Any, Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("trace_id", "store_id", "endpoint", "latency_ms", "event_count", "status_code"):
            if hasattr(record, key):
                log[key] = getattr(record, key)
        if record.exc_info:
            log["exc"] = self.formatException(record.exc_info)
        return json.dumps(log)


logger = logging.getLogger("store_intelligence")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        trace_id = str(uuid.uuid4())
        request.state.trace_id = trace_id
        t0 = time.perf_counter()
        response = await call_next(request)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        extra: dict[str, Any] = {
            "trace_id": trace_id,
            "endpoint": request.url.path,
            "latency_ms": latency_ms,
            "status_code": response.status_code,
        }
        store_id = request.path_params.get("store_id")
        if store_id:
            extra["store_id"] = store_id

        logger.info("request", extra=extra)
        response.headers["X-Trace-Id"] = trace_id
        return response


def log_ingest(trace_id: str, store_id: Optional[str], event_count: int, status_code: int) -> None:
    logger.info(
        "ingest",
        extra={
            "trace_id": trace_id,
            "store_id": store_id or "mixed",
            "endpoint": "/events/ingest",
            "event_count": event_count,
            "status_code": status_code,
        },
    )
