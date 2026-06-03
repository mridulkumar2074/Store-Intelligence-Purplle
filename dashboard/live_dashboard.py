"""
Live Store Intelligence Dashboard — Streamlit

Run:
    streamlit run dashboard/live_dashboard.py

Connects to the Store Intelligence API and auto-refreshes every 10 seconds.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000")
REFRESH_INTERVAL = int(os.getenv("DASHBOARD_REFRESH_S", "10"))

st.set_page_config(
    page_title="Store Intelligence — Live Dashboard",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(path: str, params: Optional[dict] = None) -> Optional[Any]:
    try:
        resp = requests.get(f"{API_URL}{path}", params=params or {}, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        st.error(f"API error ({path}): {exc}")
        return None


def _metric_delta(val: Any, ref: Any, fmt: str = ".1%") -> str:
    if val is None or ref is None or ref == 0:
        return ""
    delta = (val - ref) / ref
    arrow = "↑" if delta >= 0 else "↓"
    return f"{arrow} {abs(delta):.1%}"


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.title("🛍️ Store Intelligence")
store_options = ["ST1008", "ST1076"]
selected_store = st.sidebar.selectbox("Store", store_options)
st.sidebar.markdown("---")
st.sidebar.markdown(f"**API:** `{API_URL}`")
st.sidebar.markdown(f"**Refresh:** every {REFRESH_INTERVAL}s")

auto_refresh = st.sidebar.checkbox("Auto-refresh", value=True)

# ---------------------------------------------------------------------------
# Health banner
# ---------------------------------------------------------------------------

health = _get("/health")
if health:
    store_healths = {s["store_id"]: s for s in health.get("stores", [])}
    sh = store_healths.get(selected_store, {})
    feed_status = sh.get("feed_status", "UNKNOWN")
    lag = sh.get("lag_seconds")
    if feed_status == "STALE_FEED":
        st.warning(f"⚠️ STALE_FEED — last event {lag:.0f}s ago")
    elif sh:
        st.success(f"✅ Feed live — last event {lag:.0f}s ago" if lag is not None else "✅ Feed live")

# ---------------------------------------------------------------------------
# Main metrics row
# ---------------------------------------------------------------------------

st.title(f"📊 {selected_store} — Live Metrics")
st.caption(f"As of {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

metrics = _get(f"/stores/{selected_store}/metrics")
if metrics:
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Unique Visitors", metrics.get("unique_visitors", 0))
    col2.metric(
        "Conversion Rate",
        f"{metrics.get('conversion_rate', 0):.1%}",
    )
    col3.metric("Queue Depth", metrics.get("current_queue_depth", 0))
    col4.metric(
        "Abandonment Rate",
        f"{metrics.get('abandonment_rate', 0):.1%}",
    )
    col5.metric(
        "Revenue (₹)",
        f"₹{metrics.get('total_revenue_inr', 0):,.0f}",
    )

st.markdown("---")

# ---------------------------------------------------------------------------
# Two-column layout: Funnel + Anomalies
# ---------------------------------------------------------------------------

col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("🔽 Conversion Funnel")
    funnel = _get(f"/stores/{selected_store}/funnel")
    if funnel and funnel.get("stages"):
        import pandas as pd  # type: ignore
        stages = funnel["stages"]
        df_funnel = pd.DataFrame(stages)
        df_funnel = df_funnel.rename(columns={
            "stage": "Stage", "count": "Visitors", "drop_off_pct": "Drop-off %"
        })
        st.dataframe(df_funnel.set_index("Stage"), use_container_width=True)

        try:
            import plotly.graph_objects as go  # type: ignore
            fig = go.Figure(go.Funnel(
                y=[s["stage"] for s in stages],
                x=[s["count"] for s in stages],
                textinfo="value+percent initial",
            ))
            fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), height=280)
            st.plotly_chart(fig, use_container_width=True)
        except ImportError:
            st.info("Install plotly for funnel chart visualisation.")

with col_right:
    st.subheader("🚨 Active Anomalies")
    anomalies_data = _get(f"/stores/{selected_store}/anomalies")
    if anomalies_data:
        anomaly_list = anomalies_data.get("anomalies", [])
        if not anomaly_list:
            st.success("No active anomalies.")
        else:
            for a in anomaly_list:
                sev = a.get("severity", "INFO")
                icon = {"CRITICAL": "🔴", "WARN": "🟡", "INFO": "🔵"}.get(sev, "⚪")
                with st.expander(f"{icon} [{sev}] {a.get('anomaly_type', '')}"):
                    st.write(a.get("description", ""))
                    st.info(f"💡 {a.get('suggested_action', '')}")

st.markdown("---")

# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------

st.subheader("🗺️ Zone Heatmap")
window_h = st.slider("Time window (hours)", 1, 48, 24)
heatmap = _get(f"/stores/{selected_store}/heatmap", {"window_hours": window_h})
if heatmap:
    zones = heatmap.get("zones", [])
    conf = heatmap.get("data_confidence", False)
    if not conf:
        st.warning("⚠️ Low data: fewer than 20 sessions in window — scores may not be reliable.")
    if zones:
        import pandas as pd
        df_hm = pd.DataFrame(zones)[["zone_name", "visit_frequency", "avg_dwell_ms", "normalised_score"]]
        df_hm = df_hm.rename(columns={
            "zone_name": "Zone",
            "visit_frequency": "Visits",
            "avg_dwell_ms": "Avg Dwell (ms)",
            "normalised_score": "Score (0-100)",
        })
        st.dataframe(
            df_hm.style.background_gradient(subset=["Score (0-100)"], cmap="YlOrRd"),
            use_container_width=True,
        )
    else:
        st.info("No zone data in this window.")

# ---------------------------------------------------------------------------
# Zone dwell breakdown
# ---------------------------------------------------------------------------

if metrics and metrics.get("avg_dwell_per_zone"):
    st.subheader("⏱️ Avg Dwell by Zone (today)")
    import pandas as pd
    dwell_data = metrics["avg_dwell_per_zone"]
    df_dwell = pd.DataFrame(dwell_data)
    df_dwell["avg_dwell_s"] = df_dwell["avg_dwell_ms"] / 1000
    df_dwell = df_dwell.rename(columns={"zone_name": "Zone", "visit_count": "Visits", "avg_dwell_s": "Avg Dwell (s)"})
    try:
        import plotly.express as px  # type: ignore
        fig2 = px.bar(df_dwell, x="Zone", y="Avg Dwell (s)", color="Avg Dwell (s)",
                      color_continuous_scale="Blues", height=300)
        fig2.update_layout(margin=dict(l=0, r=0, t=0, b=0))
        st.plotly_chart(fig2, use_container_width=True)
    except ImportError:
        st.dataframe(df_dwell[["Zone", "Visits", "Avg Dwell (s)"]], use_container_width=True)

# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------

if auto_refresh:
    time.sleep(REFRESH_INTERVAL)
    st.rerun()
