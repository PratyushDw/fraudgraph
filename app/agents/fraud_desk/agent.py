"""fraud_desk: the ADK 2.x workflow behind FraudGraph's investigation desk.

Triage -> Investigator -> Case-File -> analyst approval -> decision record,
running Gemini 2.5 Flash on Vertex AI. All data access goes through the MCP
Toolbox parameterized-SQL tools defined in ../tools.yaml (toolset `fraud_desk`),
so every fact an agent states can be traced back to a BigQuery row.

The approval pause uses ADK's native workflow interrupt: the `hitl_approval`
FunctionNode returns a `RequestInput`, which suspends the run until the analyst
responds (from the CLI runner or the Streamlit console). The response payload
becomes the node output and the workflow resumes to record the decision.

Run modes (see run_fraud_desk.py):
  batch        triage the queue and handle the top ring
  interactive  the user message names a specific ring_id
"""

import os
import uuid

MODEL = os.environ.get("FRAUDGRAPH_MODEL", "gemini-2.5-flash")
TOOLBOX_URL = os.environ.get("TOOLBOX_URL", "http://127.0.0.1:5000")
TOOLSET = "fraud_desk"

# Shared grounding rules, appended to every agent instruction. The runner also
# re-checks cited IDs against BigQuery, so violations get caught either way.
GROUNDING_RULE = """
Grounding rules - follow these strictly:
1. Every factual claim you make MUST trace to a tool result from this conversation.
   Cite the real txn_id / account_id / ring_id values the tools returned.
2. Never invent, estimate, round, or extrapolate any number, ID, date, or amount.
3. If a tool returns no rows or an error, state exactly that and stop. Do not fill
   the gap with plausible-sounding content.
4. Whenever you reference a transaction, include its txn_id so a human analyst can
   verify it directly in BigQuery.
"""

TRIAGE_INSTRUCTION = f"""You are the Triage Agent on a bank's fraud investigation desk.
If the user's message names a specific ring_id, call `get_ring_summary` for it and
declare it the target. Otherwise call `list_top_rings` (max_rings <= 10) and rank the
queue by (risk_score x total_amount).
Output: a short markdown table of the queue (or the single named ring), then one line:
`TARGET RING: <ring_id>` — the single highest-priority ring the desk will investigate.
{GROUNDING_RULE}"""

INVESTIGATOR_INSTRUCTION = f"""You are the Investigator Agent for one target fraud ring.
The target ring_id is the `TARGET RING` named by the triage step earlier in this
conversation. Call `get_ring_summary`, then `get_ring_transactions` (max_txns <= 200),
then `get_account_profile` for the 3-5 most central member accounts (highest in/out
volume in the transactions you retrieved).
Assemble and report, with citations:
1. TIMELINE — first/last activity and burst windows (from real ts values).
2. FLOW PATHS — where money enters, how it moves between members, where it exits;
   name accounts and cite txn_ids for each hop you describe.
3. MEMBER PROFILES — account age, geo, observed volumes for the key members.
4. STRUCTURE — what the density / cyclicity / fan-in metrics say about the shape.
Report only what the tools returned. Flag anything you could NOT verify.
{GROUNDING_RULE}"""

CASEFILE_INSTRUCTION = f"""You are the Case-File Agent. Turn the investigator's findings
from this conversation into a SAR-style case file.
FIRST: call `get_ring_transactions` for the target ring (max_txns <= 200) so you have
the exact ts / src / dst / amount / channel for every transaction you cite — never
leave a field blank that the tool returns.
Then write markdown (no code fences around the output) with EXACTLY these sections:
# Case File: <ring_id>
## Pattern Classification   — the detected pattern and the metric evidence for it
## Money-Flow Narrative     — entry -> layering -> exit story, each step citing txn_ids
## Evidence Table           — markdown table: txn_id | ts | src | dst | amount | channel,
                              the 15-25 most probative transactions, every field filled
                              from tool results
## Financial Exposure       — two lines: the ring's total_amount from `get_ring_summary`,
                              and the sum over the Evidence Table rows
## Recommended Action       — exactly one of: FREEZE | ENHANCED_MONITORING | DISMISS,
                              with a two-sentence justification
Base every row and number on tool results. Only write "INSUFFICIENT EVIDENCE" for a
fact no tool can provide (e.g. an exit hop that does not exist in the data) — never
for fields the tools do return. A human analyst reviews and can reject this file.
{GROUNDING_RULE}"""


def load_tools():
    """MCP Toolbox toolset (parameterized SQL against BigQuery).

    Requires the Toolbox server:  toolbox --config app/agents/tools.yaml --enable-api
    """
    from toolbox_adk import ToolboxToolset
    return ToolboxToolset(TOOLBOX_URL, toolset_name=TOOLSET)


def request_approval(ctx, casefile_md: str):
    """HITL node — pauses the workflow for the analyst's decision.

    Returning a RequestInput interrupts the run; the analyst's response payload
    (decision / note) becomes this node's output (rerun_on_resume=False).
    `casefile_md` binds from workflow state (Case-File agent's output_key).
    """
    from google.adk.events.request_input import RequestInput
    return RequestInput(
        interrupt_id=f"hitl-{uuid.uuid4().hex[:12]}",
        message="Analyst decision required: approve | reject | escalate",
        payload={"case_file_preview": casefile_md[:400]},
    )


def record_decision(ctx, node_input=None):
    """Post-HITL node: stash the analyst decision in workflow state; the runner
    persists it to `fraudgraph.analyst_decisions` (BigQuery load job)."""
    ctx.state["analyst_decision"] = node_input
    return f"Analyst decision recorded: {node_input!r}"


def build_fraud_desk(tools=None):
    """Assemble the ADK 2.x Workflow graph:
    triage -> investigator -> casefile -> HITL pause -> decision record."""
    from google.adk.agents import LlmAgent
    from google.adk.workflow import START, Workflow, node

    tools = tools if tools is not None else [load_tools()]
    triage = LlmAgent(name="triage", model=MODEL,
                      instruction=TRIAGE_INSTRUCTION, tools=tools,
                      output_key="triage_queue")
    investigator = LlmAgent(name="investigator", model=MODEL,
                            instruction=INVESTIGATOR_INSTRUCTION, tools=tools,
                            output_key="investigation")
    casefile = LlmAgent(name="casefile", model=MODEL,
                        instruction=CASEFILE_INSTRUCTION, tools=tools,
                        output_key="casefile_md")

    return Workflow(
        name="fraud_desk",
        edges=[(START, triage, investigator, casefile,
                node(request_approval, name="hitl_approval", rerun_on_resume=False),
                node(record_decision, name="record_decision"))],
    )
