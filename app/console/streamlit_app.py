"""FraudGraph Analyst Console — Streamlit (design doc §1.7).

Four tabs: Overview KPIs · Ring queue · Ring detail (case file + approve/reject) ·
Benchmark. Phase 0 scaffold: full tab structure, BigQuery-backed when configured,
graceful placeholders when not — so a hello-world revision can deploy to Cloud Run
early (risk-register mitigation) and Phase 3 fills in the wiring.
"""

import os

import streamlit as st

PROJECT_ID = os.environ.get("FRAUDGRAPH_PROJECT_ID", "")
DATASET = "fraudgraph"

st.set_page_config(page_title="FraudGraph — Analyst Console", page_icon="🕸️",
                   layout="wide")
st.title("🕸️ FraudGraph — Fraud-Ring Investigation Desk")
st.caption("GPU-accelerated ring detection (cuDF/cuGraph) · ADK 2.x agent mesh · "
           "human-in-the-loop approval · synthetic PaySim-seeded data")


@st.cache_resource
def bq_client():
    from google.cloud import bigquery
    return bigquery.Client(project=PROJECT_ID)


@st.cache_data(ttl=300)
def query(sql: str):
    return bq_client().query(sql).to_dataframe()


configured = bool(PROJECT_ID)
tab_overview, tab_queue, tab_detail, tab_bench = st.tabs(
    ["Overview", "Ring queue", "Ring detail", "Benchmark"])

with tab_overview:
    if not configured:
        st.info("Set FRAUDGRAPH_PROJECT_ID to connect BigQuery. "
                "(Phase 0 scaffold — deploy smoke test mode.)")
    else:
        try:
            kpi = query(f"""
                SELECT COUNT(*) AS rings,
                       ROUND(SUM(total_amount), 0) AS exposure,
                       COUNTIF(risk_score >= 70) AS high_risk
                FROM `{DATASET}.ring_features`""")
            c1, c2, c3 = st.columns(3)
            c1.metric("Rings detected", f"{int(kpi.rings[0]):,}")
            c2.metric("Exposure at risk", f"₹{float(kpi.exposure[0]):,.0f}")
            c3.metric("High-risk rings (≥70)", f"{int(kpi.high_risk[0]):,}")
        except Exception as e:  # tables may not exist yet during Phase 0/1
            st.warning(f"BigQuery not ready: {e}")
    st.markdown("**Benchmark headline** — filled from the `benchmarks` table in "
                "Phase 1 (GPU seconds vs CPU minutes/DNF at 1M/10M/50M edges).")

with tab_queue:
    if configured:
        try:
            st.dataframe(query(f"""
                SELECT ring_id, pattern_label, risk_score, n_accounts, n_txns,
                       ROUND(total_amount, 0) AS total_amount
                FROM `{DATASET}.ring_features`
                ORDER BY risk_score * total_amount DESC LIMIT 50"""),
                width="stretch")
        except Exception as e:
            st.warning(f"BigQuery not ready: {e}")
    else:
        st.info("Ring queue appears here once detection outputs land in BigQuery "
                "(Phase 1).")

with tab_detail:
    st.info("Phase 2/3: pick a ring → subgraph visualization (pyvis) → "
            "'Generate case file' runs the ADK fraud_desk workflow → analyst "
            "Approve / Reject / Escalate resumes the HITL node and writes "
            "`analyst_decisions`.")

with tab_bench:
    st.info("Phase 1: CPU-vs-GPU chart rendered from the `benchmarks` table, with "
            "the full methodology note (same VM, cold+warm, DNFs recorded).")
