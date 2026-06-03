from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: Optional[int] = None
    group_id: Optional[str] = None
    group_size: Optional[int] = None
    reentry_gap_seconds: Optional[int] = None

    model_config = {"extra": "allow"}


class StoreEvent(BaseModel):
    event_id: UUID
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: datetime
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    model_config = {"json_encoders": {datetime: lambda dt: dt.isoformat()}}


class IngestRequest(BaseModel):
    events: list[StoreEvent] = Field(max_length=500)


class IngestResult(BaseModel):
    accepted: int
    rejected: int
    duplicate: int
    errors: list[dict[str, Any]] = Field(default_factory=list)


class ZoneDwellStat(BaseModel):
    zone_id: str
    zone_name: str
    avg_dwell_ms: float
    visit_count: int


class StoreMetrics(BaseModel):
    store_id: str
    date: str
    unique_visitors: int
    converted_visitors: int
    conversion_rate: float
    avg_dwell_per_zone: list[ZoneDwellStat]
    current_queue_depth: int
    abandonment_rate: float
    total_transactions: int
    total_revenue_inr: float


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class StoreFunnel(BaseModel):
    store_id: str
    window_start: str
    window_end: str
    stages: list[FunnelStage]
    total_sessions: int


class HeatmapZone(BaseModel):
    zone_id: str
    zone_name: str
    zone_type: str
    visit_frequency: int
    avg_dwell_ms: float
    normalised_score: float


class StoreHeatmap(BaseModel):
    store_id: str
    window_hours: int
    data_confidence: bool
    zones: list[HeatmapZone]


class AnomalySeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class AnomalyType(str, Enum):
    BILLING_QUEUE_SPIKE = "BILLING_QUEUE_SPIKE"
    CONVERSION_DROP = "CONVERSION_DROP"
    DEAD_ZONE = "DEAD_ZONE"
    STALE_FEED = "STALE_FEED"


class Anomaly(BaseModel):
    anomaly_id: str
    anomaly_type: AnomalyType
    severity: AnomalySeverity
    description: str
    suggested_action: str
    detected_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class StoreAnomalies(BaseModel):
    store_id: str
    checked_at: datetime
    anomalies: list[Anomaly]


class StoreHealth(BaseModel):
    store_id: str
    status: str
    last_event_ts: Optional[datetime]
    lag_seconds: Optional[float]
    feed_status: str


class HealthResponse(BaseModel):
    service: str
    version: str
    checked_at: datetime
    stores: list[StoreHealth]
    db_status: str
