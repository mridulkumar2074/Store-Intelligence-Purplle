# Architecture Decision Record — Store Intelligence System

---

## Decision 1: Detection Model — YOLOv8n + ByteTrack

### Options Considered

| Option | Accuracy | Speed (CPU 1080p) | Complexity |
|--------|----------|-------------------|------------|
| YOLOv8n + ByteTrack | Good (person class mAP ~0.52) | ~40 FPS | Low |
| YOLOv8s + ByteTrack | Better (~0.58) | ~18 FPS | Low |
| RT-DETR-L | Best (~0.63) | ~6 FPS | Medium |
| MediaPipe Pose | OK (body landmarks) | Fast | Medium |

### What AI Suggested

I asked Claude to recommend a detection stack for retail CCTV analytics. The response was:

> "YOLOv8 is the most pragmatic choice for retail CCTV. The nano variant processes 1080p footage at 40+ FPS on CPU, which is critical if you're running inference without GPU. YOLOv8n's person detection is adequate for uncrowded retail scenes. If you're seeing missed detections in crowded billing areas, YOLOv8s is the next step — better recall, acceptable latency. ByteTrack is the right tracker because it keeps low-confidence detections in a secondary buffer, which handles partial occlusion well. DeepSORT requires a Re-ID model at every update step, making it slower and more complex to deploy."

I also asked about using a VLM (GPT-4V / Claude Vision) for zone classification. The response was:

> "A VLM can classify which zone a person is standing in from a single frame, but the latency (300–800ms per API call) makes it unsuitable for processing 15 FPS video. A better approach is to use the VLM offline to validate your zone polygon definitions — paste in a frame screenshot and ask it to verify the polygons match the visible zone boundaries."

### What I Chose and Why

**YOLOv8n + ByteTrack**. I agreed with the AI's analysis. The key reasoning:
1. CPU-only Docker requirement rules out heavier models.
2. ByteTrack's low-confidence buffer is specifically valuable for the partial occlusion edge case.
3. I chose YOLOv8n over YOLOv8s because the challenge emphasises robustness (edge case handling) over raw accuracy, and the nano model leaves CPU headroom for the tracking and Re-ID logic.

**For VLM zone classification**: I did test Claude Vision on a single frame from the Store 1 zone camera. The prompt was:
> "This is a CCTV frame from a retail beauty store. I've defined these zones as pixel polygons: [polygon list]. Please verify that the polygons correctly correspond to the shelf sections visible in the frame, and flag any obvious misalignment."

The VLM response correctly identified that my initial Skincare polygon was too large and overlapped with the Center Display area. I adjusted the polygon boundaries accordingly. So I used a VLM for polygon *validation* (offline, one-time) but not for real-time classification — a sensible split.

---

## Decision 2: Event Schema Design

### Options Considered

**Option A — Flat schema** (everything at top level):
```json
{
  "event_id": "...", "store_id": "...", "visitor_id": "...",
  "event_type": "ZONE_DWELL", "zone_id": "...", "dwell_ms": 8400,
  "queue_depth": null, "sku_zone": "SKINCARE", "session_seq": 5, ...
}
```

**Option B — Nested metadata** (as specified in challenge brief):
```json
{
  "event_id": "...", "store_id": "...", "visitor_id": "...",
  "event_type": "ZONE_DWELL", "zone_id": "...", "dwell_ms": 8400,
  "metadata": {"queue_depth": null, "sku_zone": "SKINCARE", "session_seq": 5}
}
```

**Option C — Per-event-type typed schemas** (union type):
Different Pydantic models for ENTRY, ZONE_DWELL, BILLING_QUEUE_JOIN, etc., discriminated by `event_type`.

### What AI Suggested

I asked Claude to evaluate the three options. The key insight from the AI:

> "Option B (nested metadata) is the right choice for an event ingestion system because: (1) the core fields are stable and indexable; (2) the metadata fields vary by event type and would force NULL columns in a flat schema; (3) a single Pydantic model handles all event types without union type complexity. The trade-off is that querying `metadata.queue_depth` requires JSON extraction in SQL, but since queue depth is also stored as a top-level column in the database, this is mitigated."

On Option C, the AI noted:

> "Per-event-type schemas give you the best type safety but require a discriminated union, which complicates both the ingest validation and the API documentation. For a system where the consumer is a detection pipeline you control, the added complexity isn't worth it."

### What I Chose and Why

**Option B (nested metadata)**, matching the challenge brief's specified schema. I agreed with the AI. The additional reasoning I applied:

The challenge specifies that `event_id` must be globally unique UUID-v4, `visitor_id` is a per-session Re-ID token, and `confidence` must be present but not suppressed for low-confidence detections. These constraints map cleanly to a flat top-level with stable semantics. The metadata block holds the event-type-specific context.

One deviation from the AI suggestion: I also store `queue_depth`, `sku_zone`, and `session_seq` as flat columns in the database (not just in JSON metadata). This makes SQL aggregation queries for metrics computation simple and index-friendly, without changing the API-facing schema.

---

## Decision 3: API Storage — SQLite WAL vs PostgreSQL

### Options Considered

| Option | Cold Start | Query Performance | Ops Complexity |
|--------|------------|-------------------|----------------|
| SQLite (WAL mode) | Instant | Good (<100K events) | Zero |
| PostgreSQL | ~3s Docker pull | Excellent (any scale) | Requires service |
| DynamoDB / Cassandra | Cloud-only | Excellent | Very high |

### What AI Suggested

I asked Claude directly: "For a retail store analytics API serving 40 stores with up to 200 events/minute each, what database would you use?"

> "PostgreSQL. At 40 stores × 200 events/min = 8,000 events/min = ~11.5M events/day, SQLite will see contention on its single write lock and query latency will increase. You'd need TimescaleDB (time-series extension) or at minimum a PostgreSQL instance with a GIN index on the metadata column and a BRIN index on the timestamp column."

The AI also noted:

> "For the challenge submission, SQLite is fine — the goal is to demonstrate the architecture, not run 40 stores. But your CHOICES.md should acknowledge this limitation explicitly."

### What I Chose and Why

**SQLite with WAL mode** for the Docker submission, with an explicit note that the `DATABASE_URL` environment variable makes switching to Postgres a one-line change.

I disagreed with the AI's implicit assumption that the submission would be judged at 40-store production scale. The challenge acceptance gate requires `docker compose up` with no manual steps — adding a Postgres service container complicates the startup and introduces the risk of container startup ordering issues. For the submission, SQLite WAL handles the test workload (2 stores, <1000 events) perfectly.

The AI's production recommendation is correct, and I've noted it in both DESIGN.md and the README's "Scaling to Production" section. This documents the trade-off clearly without over-engineering the submission.

**Where I overrode AI**: The AI suggested adding a Redis cache layer for the metrics endpoint. I rejected this because (1) the metrics endpoint is already real-time by querying the events table directly, (2) adding Redis would violate the "no manual steps beyond git clone" requirement, and (3) for the test data volume there is no latency problem to solve.
