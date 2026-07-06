"""Custom ADK function tools that supplement the MCP Toolbox layer.

The Toolbox tools in tools.yaml cover the parameterized SQL. This module adds a
small extra tool for precomputed graph statistics that don't map cleanly onto a
single query. Ground-truth columns are never exposed to agents (they exist for
offline evaluation only, see tools.yaml).
"""

import os

PROJECT_ID = os.environ.get("FRAUDGRAPH_PROJECT_ID", "")
DATASET = "fraudgraph"


def get_ring_graph_stats(ring_id: str) -> dict:
    """Precomputed graph statistics for one detected ring.

    Returns the cuGraph-derived structural metrics (density, cyclicity, fan-in
    ratio, burstiness) together with detection provenance (method, run_id) so the
    investigator can reason about ring shape without re-deriving graph algorithms.

    Args:
        ring_id: Detected ring identifier, e.g. from list_top_rings.
    """
    from google.cloud import bigquery

    client = bigquery.Client(project=PROJECT_ID)
    job = client.query(
        f"""
        SELECT rf.ring_id, rf.n_accounts, rf.n_txns, rf.total_amount, rf.density,
               rf.cyclicity, rf.fanin_ratio, rf.burstiness, rf.pattern_label,
               rf.risk_score,
               ARRAY(SELECT ra.account_id
                     FROM `{DATASET}.ring_assignments` ra
                     WHERE ra.ring_id = rf.ring_id
                       AND ra.method != 'ground_truth'
                     ORDER BY ra.account_id LIMIT 50) AS member_accounts
        FROM `{DATASET}.ring_features` rf
        WHERE rf.ring_id = @ring_id
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("ring_id", "STRING", ring_id)]
        ),
    )
    rows = [dict(r) for r in job.result()]
    if not rows:
        return {"error": f"no ring_features row for ring_id={ring_id}"}
    return rows[0]
