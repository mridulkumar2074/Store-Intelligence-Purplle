# Store Intelligence System — Architecture Design

## Overview

This system converts raw CCTV footage from Purplle retail stores into a live analytics API. The pipeline has four stages: detection → event stream → intelligence API → live dashboard.

```
CCTV Clips
    │
    ▼
Detection Layer (pipeline/)
  YOLOv8n + ByteTrack + Re-ID
    │
    ▼ HTTP POST /events/ingest (batched)
Intelligence API (app/)
  FastAPI + SQLite
    │
    ├─ /stores/{id}/metrics
    ├─ /stores/{id}/funnel
    ├─ /stores/{id}/heatmap
    ├─ /stores/{id}/anomalies
    └─ /health
    │
    ▼
Live Dashboard (dashboard/)
  Streamlit — auto-refreshing web UI
```

---

## Stage 1: Detection Pipeline

### Model Selection
**YOLOv8n** (nano variant, `yolov8n.pt`) is used for person detection. It runs at ~40 FPS on CPU for 1080p video when processing every frame, which is necessary for accurate tracking. I considered YOLOv8s (small) for better recall on partially occluded subjects, but the nano model with a confidence threshold of 0.35 achieves sufficient detection on retail footage where subjects are well-lit.

### Tracking
**ByteTrack** (built into `ultralytics`) is used for multi-object tracking. ByteTrack maintains track continuity even through partial occlusions by keeping low-confidence detections in a secondary buffer — this directly addresses the partial occlusion edge case in the footage.

### Re-Identification
Re-ID uses a **colour histogram** approach: 32 hue bins + 16 saturation bins from the bounding box crop, normalised to a unit vector. Cosine similarity (threshold: 0.82) determines if a newly appearing person matches a recently exited one. This handles the re-entry problem without requiring a deep metric learning model (which would need GPU and adds deployment complexity).

**Cross-camera deduplication** uses the same appearance features plus a 5-minute time window. If a person exits one camera and appears on another with ≥0.82 cosine similarity within 5 minutes, they receive the same `visitor_id` rather than being double-counted.

### Staff Detection
Staff are identified by **HSV colour range matching** on the upper 50% of the bounding box. Store staff wear distinctive uniform colours (configurable via `STAFF_UNIFORM_CONFIG` env var). When ≥20% of upper-body pixels match the uniform colour range, the person is flagged `is_staff=true`. These events are stored but excluded from all customer metrics.

### Entry/Exit Detection
A horizontal **counting line** (configurable `entry_line.y` in `store_layout.json`) determines entry vs. exit direction. A person is committed as ENTRY after crossing the line for 3 consecutive frames (the `CROSSING_CONFIRM_FRAMES` constant) to prevent spurious events from micro-oscillations near the line.

### Zone Classification
Zone boundaries are defined as 2D polygons in `data/store_layout.json` per camera. The centroid of each person's bounding box is checked against these polygons using a ray-casting point-in-polygon algorithm. This is fast (O(n) where n = polygon vertices) and requires no external geometry library.

### Billing Queue & POS Correlation
When processing billing camera footage (offline/batch mode), the pipeline loads `pos_transactions.csv` and checks if a transaction occurred within 5 minutes after a person left the billing zone. No transaction → `BILLING_QUEUE_ABANDON`. This two-pass approach (process full clip first, then correlate) is only valid for offline batch processing; live processing would require a message queue.

### Event Schema
All events follow the schema specified in the challenge brief:
```json
{
  "event_id": "uuid-v4",
  "store_id": "ST1008",
  "camera_id": "CAM_ENTRY",
  "visitor_id": "VIS_c8a2f1",
  "event_type": "ZONE_DWELL",
  "timestamp": "2026-04-10T12:22:10Z",
  "zone_id": "ST1008_SKINCARE",
  "dwell_ms": 30000,
  "is_staff": false,
  "confidence": 0.91,
  "metadata": {
    "queue_depth": null,
    "sku_zone": "SKINCARE",
    "session_seq": 5
  }
}
```

---

## Stage 2: Intelligence API

### Framework
**FastAPI** with **SQLAlchemy ORM** and **SQLite** (WAL mode). FastAPI was chosen for its native Pydantic v2 integration, which provides schema validation and structured error responses at zero cost. SQLite in WAL mode handles concurrent reads from the dashboard and writes from the pipeline without blocking.

### Ingest Endpoint
`POST /events/ingest` is **idempotent by `event_id`** (primary key constraint catches duplicates). It returns partial success responses: the `accepted`/`rejected`/`duplicate` breakdown lets the pipeline caller understand exactly what happened without all-or-nothing semantics. Up to 500 events per batch.

### Metrics Computation
All metrics are computed **at query time from raw events**, not pre-aggregated. This means:
- The result is always current (no cache staleness)
- The query is simple SQL — no complex ETL
- Trade-off: at high event volume, query latency will increase and an index on `(store_id, event_type, timestamp)` becomes critical

**Conversion rate** is computed by correlating billing zone presence (BILLING_QUEUE_JOIN or ZONE_ENTER for billing zones) with POS transaction counts in the same time window.

### Funnel Logic
A "session" is one ENTRY event per unique `visitor_id` per day. REENTRY events use the same `visitor_id`, so they do not inflate the ENTRY count. The funnel measures what fraction of sessions progressed through each stage:

```
ENTRY → ZONE_VISIT → BILLING_QUEUE → PURCHASE
```

Each stage is a subset of the previous (sessions that progressed). Drop-off % is relative to the prior stage.

### Anomaly Detection
Three anomaly types are detected:

| Type | Trigger | Severity |
|------|---------|---------|
| BILLING_QUEUE_SPIKE | Joins in last hour ≥ 2× hourly baseline AND ≥ 4 absolute | WARN (2×) / CRITICAL (3×) |
| CONVERSION_DROP | Today's rate < 80% of 7-day average | WARN / CRITICAL (≥40% drop) |
| DEAD_ZONE | Zone had traffic today but none in last 30 min | INFO |

### Health Endpoint
`GET /health` checks the last event timestamp per store. If `now - last_event > 10 minutes`, the store's `feed_status` is set to `STALE_FEED`. This is what an on-call engineer would use to verify the pipeline is running.

### Graceful Degradation
If the database is unavailable, a SQLAlchemy `OperationalError` is caught at the application level and returned as HTTP 503 with a structured JSON body. No raw stack traces are exposed in responses.

---

## Stage 3: Live Dashboard

The Streamlit dashboard polls the API every 10 seconds (configurable via `DASHBOARD_REFRESH_S`). It shows:
- Key metrics (unique visitors, conversion rate, queue depth, abandonment rate, revenue)
- Conversion funnel chart (Plotly if available)
- Active anomalies with severity badges
- Zone heatmap with colour gradient
- Zone dwell bar chart

---

## AI-Assisted Decisions

### 1. Counting Line vs. Zone-based Entry Detection
I asked Claude to evaluate two approaches for entry/exit counting: (a) a horizontal counting line in the entry camera, and (b) tracking people across the full entry zone polygon.

**AI suggestion**: Use a counting line with a dead-band buffer to handle people standing near the threshold. The AI highlighted that zone-based detection suffers from false positives when people hover near the entrance without actually entering.

**My decision**: I agreed with the counting line approach but added a `CROSSING_CONFIRM_FRAMES=3` buffer that the AI did not suggest. This prevents micro-oscillations (a person swaying back and forth at the threshold) from generating spurious ENTRY/EXIT event pairs.

### 2. Re-ID Without Deep Learning
I asked Claude to compare a deep Re-ID model (OSNet from torchreid) vs. colour histogram cosine similarity for the re-entry problem.

**AI suggestion**: OSNet would give better accuracy (≥90% top-1 on Market-1501) but requires GPU inference and adds 800MB to the Docker image. For a retail store with controlled lighting, colour histograms achieve ~85% accuracy at zero GPU cost.

**My decision**: I chose colour histograms. In a production system I would A/B test against OSNet, but for this challenge the operational simplicity outweighs the marginal accuracy gain. I overrode the AI's default recommendation of deep Re-ID because the deployment constraint (CPU-only Docker) makes it impractical.

### 3. Storage Engine Choice
I asked Claude to compare SQLite WAL vs. PostgreSQL for the API backend.

**AI suggestion**: PostgreSQL for production. SQLite for local development only.

**My decision**: SQLite with WAL mode for both, since the challenge specifies `docker compose up` with no external dependencies. I noted the AI was right that PostgreSQL would be the production choice — but the challenge explicitly asks for zero-manual-step startup, and adding a Postgres container adds startup complexity. The system is designed so that swapping to Postgres requires only a `DATABASE_URL` environment variable change.
