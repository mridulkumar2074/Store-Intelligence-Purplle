# Store Intelligence — Purplle Tech Challenge 2026

A complete retail store analytics system: CCTV → detection pipeline → live API → dashboard.

**North Star Metric**: Offline Store Conversion Rate  
`Conversion Rate = Visitors who completed a purchase ÷ Total unique visitors`

---

## Quick Start (5 commands)

```bash
git clone <repo-url> && cd store-intelligence

# 1. Start the API + dashboard
docker compose up -d api dashboard

# 2. Verify the API is healthy
curl http://localhost:8000/health

# 3. Load sample events (no video required)
python sample_events/generate_sample.py --store ST1008 --visitors 30 --out sample_events/events.jsonl
curl -X POST http://localhost:8000/events/ingest \
     -H "Content-Type: application/json" \
     -d "{\"events\": $(python -c "import json; lines=[json.loads(l) for l in open('sample_events/events.jsonl')]; print(json.dumps(lines[:50]))")}"

# 4. Check metrics
curl http://localhost:8000/stores/ST1008/metrics | python -m json.tool

# 5. Open the live dashboard
open http://localhost:8501
```

---

## Running the Detection Pipeline Against the CCTV Clips

### Prerequisites

```bash
pip install -r requirements-pipeline.txt
# YOLOv8n weights are downloaded automatically on first run (~6MB)
```

### Process a Single Camera

```bash
python -m pipeline.detect \
  --store-id ST1008 \
  --camera-id CAM_ENTRY \
  --video "Store 1/CAM 3 - entry.mp4" \
  --camera-type entry \
  --layout data/store_layout.json \
  --api-url http://localhost:8000 \
  --pos-csv data/pos_transactions.csv \
  --start-ts "2026-04-10T10:00:00Z"
```

### Process All Cameras (Both Stores)

```bash
# Make sure the API is running first
docker compose up -d api

# Then run the pipeline
API_URL=http://localhost:8000 bash pipeline/run.sh

# Or for a specific store only:
bash pipeline/run.sh --store ST1008 --api-url http://localhost:8000
```

The pipeline will:
1. Process each camera clip in order (entry → zone → billing)
2. Emit events via `POST /events/ingest` in batches of 50
3. Correlate billing zone exits with POS transactions for abandon detection
4. Log progress and flush counts per camera

### Via Docker Compose (Pipeline Container)

```bash
docker compose --profile pipeline up pipeline
```

---

## API Reference

| Endpoint | Description |
|----------|-------------|
| `POST /events/ingest` | Ingest up to 500 events. Idempotent by `event_id`. |
| `GET /stores/{id}/metrics` | Unique visitors, conversion rate, queue depth, abandonment |
| `GET /stores/{id}/funnel` | Entry → Zone Visit → Billing → Purchase funnel |
| `GET /stores/{id}/heatmap` | Zone visit frequency + avg dwell, normalised 0-100 |
| `GET /stores/{id}/anomalies` | Active anomalies with severity and suggested action |
| `GET /health` | Service health + per-store feed lag |

All endpoints support JSON. Swagger UI: http://localhost:8000/docs

---

## Running Tests

```bash
pip install pytest pytest-cov httpx
pytest --cov=app --cov=pipeline --cov-report=term-missing
```

Expected coverage: >70%

---

## Live Dashboard

Open **http://localhost:8501** after `docker compose up`.

- Auto-refreshes every 10 seconds
- Shows real-time metrics, funnel, heatmap, and anomalies
- Store selector in sidebar
- Feed staleness banner at top

---

## Project Structure

```
├── app/                    # FastAPI intelligence API
│   ├── main.py             # FastAPI entrypoint + POS CSV loader
│   ├── models.py           # Pydantic event + response schemas
│   ├── database.py         # SQLAlchemy ORM + SQLite setup
│   ├── ingestion.py        # POST /events/ingest logic
│   ├── metrics.py          # Real-time metric computation
│   ├── funnel.py           # Funnel + session deduplication
│   ├── heatmap.py          # Zone heatmap normalisation
│   ├── anomalies.py        # Anomaly detection (3 types)
│   ├── health.py           # Health + STALE_FEED detection
│   └── telemetry.py        # Structured JSON request logging
├── pipeline/               # Detection pipeline
│   ├── detect.py           # YOLOv8n detection + ByteTrack
│   ├── tracker.py          # Re-ID + cross-camera dedup
│   ├── zone_classifier.py  # Point-in-polygon zone assignment
│   ├── staff_detector.py   # HSV uniform colour detection
│   ├── emit.py             # Event schema + HTTP emission
│   └── run.sh              # One-command clip processor
├── dashboard/
│   └── live_dashboard.py   # Streamlit live dashboard
├── data/
│   ├── store_layout.json   # Zone definitions for both stores
│   └── pos_transactions.csv
├── sample_events/
│   ├── events.jsonl        # 596 synthetic schema-valid events
│   ├── generate_sample.py  # Synthetic event generator
│   └── convert_pos.py      # POS CSV → normalised JSON
├── tests/
│   ├── test_pipeline.py    # Zone classifier, Re-ID, emitter tests
│   ├── test_metrics.py     # API endpoint tests (idempotency, edge cases)
│   └── test_anomalies.py   # Anomaly detection tests
├── docs/
│   ├── DESIGN.md           # Architecture + AI-assisted decisions
│   └── CHOICES.md          # 3 key decisions with full reasoning
├── Dockerfile
├── Dockerfile.pipeline
└── docker-compose.yml
```

---

## Event Schema

```json
{
  "event_id": "uuid-v4",
  "store_id": "ST1008",
  "camera_id": "CAM_ENTRY",
  "visitor_id": "VIS_c8a2f1",
  "event_type": "ZONE_DWELL",
  "timestamp": "2026-04-10T14:22:10Z",
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

**Event types**: `ENTRY`, `EXIT`, `ZONE_ENTER`, `ZONE_EXIT`, `ZONE_DWELL`, `BILLING_QUEUE_JOIN`, `BILLING_QUEUE_ABANDON`, `REENTRY`

---

## Store Layout

Zone definitions live in `data/store_layout.json`. Each store has:
- Camera metadata (type, file, FPS, resolution)
- Counting line for entry/exit (y-coordinate + direction)
- Zone polygons per camera (pixel coordinates for 1920×1080)

---

## Scaling to Production

| Concern | Current | Production Path |
|---------|---------|-----------------|
| Database | SQLite WAL | PostgreSQL + TimescaleDB (change `DATABASE_URL`) |
| Event throughput | Batch HTTP | Kafka/Kinesis stream consumer |
| Re-ID accuracy | Colour histogram | OSNet deep Re-ID model (GPU required) |
| Dashboard | Streamlit polling | WebSocket push + React frontend |
| Multi-store | Sequential processing | Kubernetes jobs per store |
