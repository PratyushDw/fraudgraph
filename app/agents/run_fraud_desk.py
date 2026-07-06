"""fraud_desk workflow runner + reusable helpers.

Drives: triage -> investigator -> case-file -> approval pause -> analyst decision
-> persist to BigQuery. Before anything is persisted, every txn_id / account_id
cited in the case file is checked against BigQuery; a case file citing an ID that
does not exist is rejected outright.

The Streamlit console imports generate_case_file / verify_citations / persist
from here rather than duplicating the logic.

CLI usage (Toolbox server must be running:
    toolbox --config app/agents/tools.yaml --enable-api):
  python app/agents/run_fraud_desk.py                       # batch: top ring
  python app/agents/run_fraud_desk.py --ring-id R-...-50    # interactive: one ring
  python app/agents/run_fraud_desk.py --decision approve --note "..."
  add --no-persist to skip the BigQuery writes (dry run)
"""

import argparse
import asyncio
import datetime
import os
import re
import sys

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT",
                      os.environ.get("FRAUDGRAPH_PROJECT_ID", "fraudgraph"))
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from google.adk import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow.utils import _workflow_hitl_utils as hitl_utils
from google.genai import types

from app.agents.fraud_desk.agent import MODEL, build_fraud_desk

PROJECT_ID = os.environ["GOOGLE_CLOUD_PROJECT"]
DATASET = "fraudgraph"
APP = "fraud_desk"

VALID_ACTIONS = ("FREEZE", "ENHANCED_MONITORING", "DISMISS")
VALID_PATTERNS = ("CYCLE", "MULE_CHAIN", "SMURF_FAN_IN", "DISPERSAL_FAN_OUT",
                  "DORMANT_BURST", "UNCLASSIFIED")


def verify_citations(casefile_md: str, project_id: str = PROJECT_ID):
    """Grounding gate: every txn_id / account_id cited must exist in BigQuery.
    Returns (txn_ids, missing_txns, missing_accounts)."""
    from google.cloud import bigquery
    client = bigquery.Client(project=project_id)
    txn_ids = sorted(set(re.findall(r"\bT\d{3,}\b", casefile_md)))
    acct_ids = sorted(set(re.findall(r"\bA\d{3,}\b", casefile_md)))
    missing_t, missing_a = [], []
    if txn_ids:
        found = {r.txn_id for r in client.query(
            f"SELECT txn_id FROM `{DATASET}.transactions` WHERE txn_id IN UNNEST(@ids)",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ArrayQueryParameter("ids", "STRING", txn_ids)])).result()}
        missing_t = [t for t in txn_ids if t not in found]
    if acct_ids:
        found = {r.account_id for r in client.query(
            f"SELECT account_id FROM `{DATASET}.accounts` WHERE account_id IN UNNEST(@ids)",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ArrayQueryParameter("ids", "STRING", acct_ids)])).result()}
        missing_a = [a for a in acct_ids if a not in found]
    return txn_ids, missing_t, missing_a


async def generate_case_file(ring_id=None):
    """Run triage -> investigator -> case-file live (Gemini + MCP Toolbox), returning
    a dict: ring_id, casefile_md, txn_ids, missing_txns, missing_accounts, tool_calls.
    The workflow pauses at the HITL node; the caller records the analyst decision."""
    wf = build_fraud_desk()
    ss = InMemorySessionService()
    runner = Runner(node=wf, app_name=APP, session_service=ss)
    session = await ss.create_session(app_name=APP, user_id="analyst")
    prompt = (f"Investigate ring {ring_id} and produce its case file." if ring_id else
              "Triage the queue and produce a case file for the single "
              "highest-priority ring.")
    tool_calls = []
    try:
        async for ev in runner.run_async(user_id="analyst", session_id=session.id,
                                         new_message=types.Content(
                                             role="user", parts=[types.Part(text=prompt)])):
            if ev.content and ev.content.parts:
                for p in ev.content.parts:
                    if p.function_call and p.function_call.name != "adk_request_input":
                        tool_calls.append(f"{ev.author}: {p.function_call.name}")
    finally:
        await runner.close()
    state = (await ss.get_session(app_name=APP, user_id="analyst",
                                  session_id=session.id)).state
    casefile_md = state.get("casefile_md", "")
    rid = ring_id
    if not rid:
        m = re.search(r"\bR-[\w.\-]+\b", casefile_md)
        rid = m.group(0).rstrip(".,)") if m else "UNKNOWN"
    txn_ids, missing_t, missing_a = (verify_citations(casefile_md) if casefile_md
                                     else ([], [], []))
    return {"ring_id": rid, "casefile_md": casefile_md, "txn_ids": txn_ids,
            "missing_txns": missing_t, "missing_accounts": missing_a,
            "tool_calls": tool_calls}


def persist(casefile_md, ring_id, decision, note, txn_ids, project_id=PROJECT_ID):
    """Write case_files + analyst_decisions via free-tier load jobs."""
    from google.cloud import bigquery
    client = bigquery.Client(project=project_id)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    a = re.search(r"Recommended Action.*?\b(" + "|".join(VALID_ACTIONS) + r")\b",
                  casefile_md, re.S | re.I)
    action = a.group(1).upper() if a else "UNSPECIFIED"
    p = re.search(r"\b(" + "|".join(VALID_PATTERNS) + r")\b", casefile_md)
    pattern = p.group(1) if p else ""
    status = {"approve": "approved", "reject": "rejected",
              "escalate": "escalated"}.get(decision, "draft")

    cf_schema = [bigquery.SchemaField("ring_id", "STRING"),
                 bigquery.SchemaField("created_at", "TIMESTAMP"),
                 bigquery.SchemaField("pattern", "STRING"),
                 bigquery.SchemaField("narrative_md", "STRING"),
                 bigquery.SchemaField("evidence_txn_ids", "STRING", mode="REPEATED"),
                 bigquery.SchemaField("recommended_action", "STRING"),
                 bigquery.SchemaField("model", "STRING"),
                 bigquery.SchemaField("status", "STRING")]
    client.load_table_from_json(
        [{"ring_id": ring_id, "created_at": now, "pattern": pattern,
          "narrative_md": casefile_md, "evidence_txn_ids": txn_ids,
          "recommended_action": action, "model": MODEL, "status": status}],
        f"{project_id}.{DATASET}.case_files",
        job_config=bigquery.LoadJobConfig(schema=cf_schema)).result()

    ad_schema = [bigquery.SchemaField("ring_id", "STRING"),
                 bigquery.SchemaField("decision", "STRING"),
                 bigquery.SchemaField("note", "STRING"),
                 bigquery.SchemaField("decided_at", "TIMESTAMP")]
    client.load_table_from_json(
        [{"ring_id": ring_id, "decision": decision, "note": note, "decided_at": now}],
        f"{project_id}.{DATASET}.analyst_decisions",
        job_config=bigquery.LoadJobConfig(schema=ad_schema)).result()
    return {"status": status, "action": action, "pattern": pattern}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ring-id", default=None)
    ap.add_argument("--decision", default=None, choices=["approve", "reject", "escalate"])
    ap.add_argument("--note", default="")
    ap.add_argument("--no-persist", action="store_true")
    args = ap.parse_args()

    print(f"[run] target={args.ring_id or 'top ring'} model={MODEL} project={PROJECT_ID}")
    res = await generate_case_file(args.ring_id)
    for tc in res["tool_calls"]:
        print(f"  tool -> {tc}")
    print("\n===== CASE FILE =====\n" + res["casefile_md"])

    if not res["casefile_md"]:
        print("FATAL: no case file produced"); sys.exit(2)
    print(f"\n[verify] cited txn_ids: {len(res['txn_ids'])} | "
          f"missing txns: {res['missing_txns'] or 'none'} | "
          f"missing accounts: {res['missing_accounts'] or 'none'}")
    if res["missing_txns"] or res["missing_accounts"]:
        print("GROUNDING VIOLATION — IDs not in BigQuery. Not persisting."); sys.exit(3)

    decision = args.decision or (input(
        "\nAnalyst decision [approve/reject/escalate]: ").strip() or "escalate")
    note = args.note or f"decided via CLI runner ({decision})"
    if not args.no_persist:
        info = persist(res["casefile_md"], res["ring_id"], decision, note, res["txn_ids"])
        print(f"[persist] {res['ring_id']} -> status={info['status']} action={info['action']}")
    print("\n[done] workflow complete — case file verified"
          + ("" if args.no_persist else " and persisted"))


if __name__ == "__main__":
    asyncio.run(main())
