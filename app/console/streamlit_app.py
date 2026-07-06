"""FraudGraph analyst console - Streamlit app served on Cloud Run.

Four tabs: overview KPIs, ring queue, ring detail (subgraph + case file +
approve/reject/escalate) and the benchmark. Every panel reads the BigQuery
warehouse that the GPU pipeline and the agent workflow populate; analyst
decisions are written back to `analyst_decisions` and `case_files`. The live
"generate case file" button runs the agents (Gemini + MCP Toolbox) when that
stack is available and falls back to the persisted case file when it isn't, so
the console works even if the agent runtime is down.
"""

import os

import pandas as pd
import streamlit as st

PROJECT_ID = (os.environ.get("FRAUDGRAPH_PROJECT_ID")
              or os.environ.get("GOOGLE_CLOUD_PROJECT") or "fraudgraph")
DATASET = "fraudgraph"
RISK_THRESHOLD = 60

st.set_page_config(page_title="FraudGraph — Analyst Console", page_icon="🕸️",
                   layout="wide")


@st.cache_resource
def bq():
    from google.cloud import bigquery
    return bigquery.Client(project=PROJECT_ID)


@st.cache_data(ttl=120, show_spinner=False)
def q(sql: str) -> pd.DataFrame:
    return bq().query(sql).to_dataframe()


def table(name: str) -> str:
    return f"`{PROJECT_ID}.{DATASET}.{name}`"


st.title("🕸️ FraudGraph — Fraud-Ring Investigation Desk")
st.caption("GPU-accelerated ring detection (cuDF / cuGraph) · ADK 2.x agent mesh "
           "(Gemini 2.5 Flash) · human-in-the-loop approval · synthetic, "
           "PaySim-seeded data")

try:
    bq()
    connected = True
except Exception as e:  # noqa: BLE001
    connected = False
    st.error(f"BigQuery not reachable ({e}). Set FRAUDGRAPH_PROJECT_ID / credentials.")

tab_overview, tab_queue, tab_detail, tab_bench = st.tabs(
    ["📊 Overview", "📋 Ring queue", "🔬 Ring detail", "⚡ Benchmark"])


# --------------------------------------------------------------- Overview
with tab_overview:
    if connected:
        try:
            k = q(f"""
                SELECT COUNT(*) AS rings,
                       COALESCE(SUM(total_amount), 0) AS exposure,
                       COUNTIF(risk_score >= {RISK_THRESHOLD}) AS high_risk
                FROM {table('ring_features')}""").iloc[0]
            cf = q(f"SELECT COUNT(*) AS n FROM {table('case_files')}").iloc[0]["n"]
            dec = q(f"SELECT COUNT(*) AS n FROM {table('analyst_decisions')}").iloc[0]["n"]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Rings detected", f"{int(k['rings']):,}")
            c2.metric("Exposure at risk", f"₹{float(k['exposure']):,.0f}")
            c3.metric("High-risk rings", f"{int(k['high_risk']):,}", help=f"risk ≥ {RISK_THRESHOLD}")
            c4.metric("Case files · decisions", f"{int(cf)} · {int(dec)}")
        except Exception as e:  # noqa: BLE001
            st.warning(f"KPIs unavailable: {e}")

        st.divider()
        left, right = st.columns(2)
        with left:
            st.subheader("⚡ Acceleration headline")
            try:
                b = q(f"""
                    SELECT n_edges,
                       ROUND(SUM(IF(engine='gpu' AND NOT warm, wall_seconds, 0)), 1) AS gpu_s,
                       ROUND(SUM(IF(engine='cpu' AND NOT warm, wall_seconds, 0)), 1) AS cpu_s
                    FROM {table('benchmarks')}
                    WHERE operation IN ('load_parquet','etl_features','graph_build',
                                        'wcc','louvain','pagerank')
                    GROUP BY n_edges ORDER BY n_edges""")
                big = b[b.cpu_s > 0].sort_values("n_edges").tail(1)
                if not big.empty:
                    r = big.iloc[0]
                    st.metric(f"End-to-end at {int(r['n_edges']/1e6)}M edges",
                              f"GPU {r['gpu_s']}s  vs  CPU {r['cpu_s']}s",
                              f"{r['cpu_s']/max(r['gpu_s'],0.1):.0f}× faster")
                st.caption("Same Colab T4 VM, cold runs; full table in the Benchmark tab.")
            except Exception as e:  # noqa: BLE001
                st.info(f"Benchmark headline pending: {e}")
        with right:
            st.subheader("🎯 Detection quality vs ground truth")
            try:
                pr = q(f"""
                    WITH flagged AS (
                      SELECT DISTINCT ra.account_id
                      FROM {table('ring_assignments')} ra
                      JOIN {table('ring_features')} rf USING (ring_id)
                      WHERE ra.method='leiden' AND rf.risk_score >= {RISK_THRESHOLD})
                    SELECT
                      COUNTIF(a.is_mule_gt AND f.account_id IS NOT NULL) AS tp,
                      COUNTIF(NOT a.is_mule_gt AND f.account_id IS NOT NULL) AS fp,
                      COUNTIF(a.is_mule_gt AND f.account_id IS NULL) AS fn
                    FROM {table('accounts')} a
                    LEFT JOIN flagged f ON a.account_id = f.account_id""").iloc[0]
                tp, fp, fn = int(pr.tp), int(pr.fp), int(pr.fn)
                prec = tp / (tp + fp) if tp + fp else 0.0
                rec = tp / (tp + fn) if tp + fn else 0.0
                m1, m2 = st.columns(2)
                m1.metric("Precision", f"{prec:.1%}")
                m2.metric("Recall", f"{rec:.1%}")
                st.caption(f"Accounts flagged in a ring (risk ≥ {RISK_THRESHOLD}) scored "
                           "against generator ground truth - possible because the data "
                           "is synthetic and labelled.")
            except Exception as e:  # noqa: BLE001
                st.info(f"Precision/recall pending: {e}")

        st.divider()
        st.subheader("Recent analyst decisions")
        try:
            st.dataframe(q(f"""SELECT ring_id, decision, note, decided_at
                              FROM {table('analyst_decisions')}
                              ORDER BY decided_at DESC LIMIT 10"""),
                         width="stretch", hide_index=True)
        except Exception as e:  # noqa: BLE001
            st.info(f"No decisions yet: {e}")
    else:
        st.info("Connect BigQuery to populate the overview.")


# --------------------------------------------------------------- Ring queue
with tab_queue:
    st.subheader("Investigation queue — ranked by exposure × risk")
    if connected:
        try:
            queue = q(f"""
                SELECT ring_id, pattern_label, risk_score, n_accounts, n_txns,
                       ROUND(total_amount, 0) AS total_amount,
                       ROUND(cyclicity, 3) AS cyclicity, ROUND(fanin_ratio, 3) AS fanin_ratio
                FROM {table('ring_features')}
                ORDER BY risk_score * total_amount DESC LIMIT 100""")
            st.dataframe(queue, width="stretch", hide_index=True,
                         column_config={"total_amount": st.column_config.NumberColumn(
                             "total_amount (₹)", format="%.0f")})
            st.caption(f"{len(queue)} rings · click the Ring detail tab to investigate one.")
        except Exception as e:  # noqa: BLE001
            st.warning(f"Queue unavailable: {e}")
    else:
        st.info("Connect BigQuery to load the ring queue.")


# --------------------------------------------------------------- Ring detail
def draw_subgraph(members, edges_df):
    import matplotlib.pyplot as plt
    import networkx as nx
    g = nx.from_pandas_edgelist(edges_df, "src_account", "dst_account",
                                edge_attr="amount", create_using=nx.DiGraph())
    fig, ax = plt.subplots(figsize=(7, 5))
    if g.number_of_nodes():
        pos = nx.spring_layout(g, seed=42, k=0.9)
        nx.draw_networkx_nodes(g, pos, node_color="#76B900", node_size=520,
                               edgecolors="#3E6606", ax=ax)
        nx.draw_networkx_edges(g, pos, edge_color="#5F6368", width=1.4,
                               arrows=True, arrowsize=12, ax=ax,
                               connectionstyle="arc3,rad=0.08")
        nx.draw_networkx_labels(g, pos, font_size=7, ax=ax)
    ax.set_title(f"Ring subgraph — {g.number_of_nodes()} members, "
                 f"{g.number_of_edges()} internal flows")
    ax.axis("off")
    return fig


with tab_detail:
    if connected:
        try:
            rings = q(f"""SELECT ring_id, pattern_label, risk_score, total_amount
                         FROM {table('ring_features')}
                         ORDER BY risk_score * total_amount DESC LIMIT 100""")
            labels = {f"{r.ring_id}  ·  {r.pattern_label}  ·  risk {r.risk_score}": r.ring_id
                      for r in rings.itertuples()}
            pick = st.selectbox("Select a ring to investigate", list(labels))
            ring_id = labels[pick]

            summ = q(f"SELECT * FROM {table('ring_features')} "
                     f"WHERE ring_id = '{ring_id}'").iloc[0]
            m = st.columns(5)
            m[0].metric("Pattern", summ.pattern_label)
            m[1].metric("Risk", f"{summ.risk_score}")
            m[2].metric("Accounts", f"{int(summ.n_accounts)}")
            m[3].metric("Exposure", f"₹{summ.total_amount:,.0f}")
            m[4].metric("Cyclicity", f"{summ.cyclicity:.2f}")

            gcol, ccol = st.columns([1, 1])
            with gcol:
                st.subheader("Money-flow subgraph")
                try:
                    members = q(f"""SELECT account_id FROM {table('ring_assignments')}
                                   WHERE ring_id='{ring_id}' AND method='leiden'""")["account_id"].tolist()
                    if members:
                        ids = ",".join(f"'{a}'" for a in members)
                        edges_df = q(f"""SELECT src_account, dst_account, SUM(amount) AS amount
                                        FROM {table('transactions')}
                                        WHERE src_account IN ({ids}) AND dst_account IN ({ids})
                                        GROUP BY src_account, dst_account""")
                        st.pyplot(draw_subgraph(members, edges_df))
                    else:
                        st.info("No detected members for this ring.")
                except Exception as e:  # noqa: BLE001
                    st.warning(f"Subgraph unavailable: {e}")

            with ccol:
                st.subheader("Case file")
                persisted = q(f"""SELECT narrative_md, recommended_action, status, model,
                                        created_at
                                 FROM {table('case_files')} WHERE ring_id='{ring_id}'
                                 ORDER BY created_at DESC LIMIT 1""")
                gen = st.button("🤖 Generate fresh case file (live agent)",
                                help="Runs the ADK workflow: triage → investigate → "
                                     "case-file, grounded in BigQuery.")
                casefile_md, txn_ids = None, []
                if gen:
                    with st.spinner("Running triage → investigator → case-file agents…"):
                        try:
                            import asyncio
                            from app.agents.run_fraud_desk import generate_case_file
                            res = asyncio.run(generate_case_file(ring_id))
                            casefile_md, txn_ids = res["casefile_md"], res["txn_ids"]
                            if res["missing_txns"] or res["missing_accounts"]:
                                st.error("Grounding violation — cited IDs not in BigQuery.")
                                casefile_md = None
                            else:
                                st.session_state[f"cf_{ring_id}"] = (casefile_md, txn_ids)
                                st.success(f"Generated · {len(txn_ids)} cited txns "
                                           "verified in BigQuery.")
                        except Exception as e:  # noqa: BLE001
                            st.warning(f"Live agent unavailable here ({e}). "
                                       "Showing the persisted case file.")
                if not casefile_md and f"cf_{ring_id}" in st.session_state:
                    casefile_md, txn_ids = st.session_state[f"cf_{ring_id}"]
                if not casefile_md and not persisted.empty:
                    casefile_md = persisted.iloc[0]["narrative_md"]
                    st.caption(f"Persisted case file · {persisted.iloc[0]['model']} · "
                               f"status: {persisted.iloc[0]['status']}")

                if casefile_md:
                    with st.container(height=380, border=True):
                        st.markdown(casefile_md)
                    st.markdown("**Analyst decision**")
                    note = st.text_input("Note", key=f"note_{ring_id}",
                                         placeholder="rationale recorded with the decision")
                    d1, d2, d3 = st.columns(3)
                    decision = None
                    if d1.button("✅ Approve (freeze)", key=f"ap_{ring_id}"):
                        decision = "approve"
                    if d2.button("⤴️ Escalate", key=f"es_{ring_id}"):
                        decision = "escalate"
                    if d3.button("❌ Reject", key=f"rj_{ring_id}"):
                        decision = "reject"
                    if decision:
                        try:
                            from app.agents.run_fraud_desk import persist
                            persist(casefile_md, ring_id, decision,
                                    note or f"decided via console ({decision})", txn_ids)
                            q.clear()
                            st.success(f"Decision '{decision}' recorded to "
                                       "analyst_decisions + case_files.")
                        except Exception as e:  # noqa: BLE001
                            st.error(f"Could not record decision: {e}")
                else:
                    st.info("No case file yet — click *Generate* above.")
        except Exception as e:  # noqa: BLE001
            st.warning(f"Ring detail unavailable: {e}")
    else:
        st.info("Connect BigQuery to investigate rings.")


# --------------------------------------------------------------- Benchmark
with tab_bench:
    st.subheader("CPU vs GPU — same VM, cold runs")
    if connected:
        try:
            b = q(f"""SELECT n_edges, engine, operation, wall_seconds, status
                     FROM {table('benchmarks')} WHERE NOT warm""")
            stages = ["load_parquet", "etl_features", "graph_build", "wcc",
                      "louvain", "pagerank"]
            b = b[b.operation.isin(stages)]
            sizes = sorted(b.n_edges.unique())
            rows = []
            for n in sizes:
                row = {"edges": f"{int(n/1e6)}M"}
                for eng in ("gpu", "cpu"):
                    sub = b[(b.n_edges == n) & (b.engine == eng)]
                    if sub.empty:
                        row[eng.upper()] = "—"
                    elif sub.status.astype(str).str.startswith("DNF").any():
                        row[eng.upper()] = "DNF"
                    else:
                        row[eng.upper()] = f"{sub.wall_seconds.sum():.1f}s"
                rows.append(row)
            summary = pd.DataFrame(rows)

            import matplotlib.pyplot as plt
            import numpy as np
            fig, ax = plt.subplots(figsize=(9, 4.5))
            x = np.arange(len(sizes))
            for off, (eng, color) in enumerate([("cpu", "#5F6368"), ("gpu", "#76B900")]):
                vals, hatch = [], []
                for n in sizes:
                    sub = b[(b.n_edges == n) & (b.engine == eng)]
                    if sub.empty:
                        vals.append(0); hatch.append(False)
                    elif sub.status.astype(str).str.startswith("DNF").any():
                        vals.append(1800); hatch.append(True)
                    else:
                        vals.append(sub.wall_seconds.sum()); hatch.append(False)
                bars = ax.bar(x + (off - 0.5) * 0.4, vals, width=0.38, color=color,
                              label=eng.upper())
                for i, (v, h) in enumerate(zip(vals, hatch)):
                    if v > 0:
                        bars[i].set_hatch("//" if h else None)
                        ax.text(x[i] + (off - 0.5) * 0.4, v,
                                "DNF" if h else f"{v:.0f}s", ha="center",
                                va="bottom", fontsize=8)
            ax.set_yscale("log"); ax.set_xticks(x, [f"{int(n/1e6)}M" for n in sizes])
            ax.set_ylabel("end-to-end wall seconds (log)"); ax.set_xlabel("transaction edges")
            ax.set_title("FraudGraph pipeline — GPU (cuDF+cuGraph) vs CPU (pandas+NetworkX)")
            ax.legend()
            cc1, cc2 = st.columns([2, 1])
            cc1.pyplot(fig)
            cc2.dataframe(summary, width="stretch", hide_index=True)
            st.caption("Methodology: identical logical operations both engines; same "
                       "Colab T4 VM (its CPU vs its GPU); cold timings; CPU omitted above "
                       "10M (exceeds VM RAM). Full provenance in the `benchmarks` table.")
        except Exception as e:  # noqa: BLE001
            st.warning(f"Benchmark unavailable: {e}")
    else:
        st.info("Connect BigQuery to load the benchmark.")
