"""FraudGraph `fraud_desk` — ADK 2.x Workflow graph (design doc §1.6).

Triage -> Investigator -> Case-File -> HITL approval, Gemini 2.5 Flash on Vertex AI,
grounded in BigQuery through MCP Toolbox tools (../tools.yaml, toolset `fraud_desk`).

Phase 0 scaffold: agents, instructions, and graph shape, verified against
google-adk==2.3.0 (`google.adk.workflow`: Workflow / Node / FunctionNode / Edge /
START; HITL pause via `google.adk.events.request_input.RequestInput`).
Phase 2 wires: live Toolbox tool loading, HITL resume from the console,
batch (top-5 rings) and interactive (single ring) run modes.
"""

import os

MODEL = "gemini-2.5-flash"
TOOLBOX_URL = os.environ.get("TOOLBOX_URL", "http://127.0.0.1:5000")
TOOLSET = "fraud_desk"

# Non-negotiable grounding rule, embedded in every agent instruction (design doc
# §1.6): claims must trace to tool results; no invented numbers, ever.
GROUNDING_RULE = """
GROUNDING RULES (non-negotiable):
1. Every factual claim you make MUST trace to a tool result from this conversation.
   Cite the real txn_id / account_id / ring_id values the tools returned.
2. Never invent, estimate, round, or extrapolate any number, ID, date, or amount.
3. If a tool returns no rows or an error, state exactly that and stop — do not fill
   the gap with plausible-sounding content.
4. Whenever you reference a transaction, include its txn_id so a human analyst can
   verify it directly in BigQuery.
"""

TRIAGE_INSTRUCTION = f"""You are the Triage Agent on a bank's fraud investigation desk.
Call `list_top_rings` (max_rings <= 10) and produce the investigation queue:
rank rings by (risk_score x total_amount), and for each ring give one line —
ring_id, pattern_label, n_accounts, total_amount, and why it ranks where it does.
Output the queue as a short markdown table, highest priority first.
{GROUNDING_RULE}"""

INVESTIGATOR_INSTRUCTION = f"""You are the Investigator Agent for one target fraud ring.
Given a ring_id: call `get_ring_summary`, then `get_ring_transactions`
(max_txns <= 200), then `get_account_profile` for the 3-5 most central member
accounts (highest in/out volume in the transactions you retrieved).
Assemble and report, with citations:
1. TIMELINE — first/last activity and burst windows (from real ts values).
2. FLOW PATHS — where money enters, how it moves between members, where it exits;
   name accounts and cite txn_ids for each hop you describe.
3. MEMBER PROFILES — account age, geo, observed volumes for the key members.
4. STRUCTURE — what the density / cyclicity / fan-in metrics say about the shape.
Report only what the tools returned. Flag anything you could NOT verify.
{GROUNDING_RULE}"""

CASEFILE_INSTRUCTION = f"""You are the Case-File Agent. Turn the investigator's findings
into a SAR-style case file in markdown with EXACTLY these sections:
# Case File: <ring_id>
## Pattern Classification   — the detected pattern and the metric evidence for it
## Money-Flow Narrative     — entry -> layering -> exit story, each step citing txn_ids
## Evidence Table           — markdown table: txn_id | ts | src | dst | amount | channel
## Financial Exposure       — total amount at risk (sum only over cited transactions)
## Recommended Action       — one of: FREEZE | ENHANCED_MONITORING | DISMISS, with a
                              two-sentence justification
Base every row and number on tool results already gathered in this workflow. If
evidence is insufficient for a section, write "INSUFFICIENT EVIDENCE" rather than
inventing content. The file will be reviewed by a human analyst who can reject it.
{GROUNDING_RULE}"""


def load_tools():
    """MCP Toolbox toolset (parameterized SQL against BigQuery). Requires the
    toolbox server to be running with ../tools.yaml. Phase 2 wires auth."""
    from toolbox_adk import ToolboxToolset
    return ToolboxToolset(TOOLBOX_URL, toolset_name=TOOLSET)


def request_approval(context):
    """HITL node body — pauses the workflow for the analyst's decision.

    Phase 2: emit a RequestInput (google.adk.events.request_input.RequestInput) so
    the run interrupts; the Streamlit console renders the case file and resumes the
    workflow with approve / reject / escalate, which is then persisted to
    `fraudgraph.analyst_decisions`.
    """
    raise NotImplementedError("Phase 2: HITL wiring (RequestInput emit + resume)")


def build_fraud_desk(tools=None):
    """Assemble the ADK 2.x Workflow graph: triage -> investigate -> casefile -> HITL.

    `edges` uses the chain-tuple form (EdgeItem = Edge | tuple[ChainElement, ...]);
    agents and functions are auto-wrapped into nodes by the Workflow runtime.
    """
    from google.adk.agents import LlmAgent
    from google.adk.workflow import START, Workflow, node

    tools = tools if tools is not None else [load_tools()]
    triage = LlmAgent(name="triage", model=MODEL,
                      instruction=TRIAGE_INSTRUCTION, tools=tools)
    investigator = LlmAgent(name="investigator", model=MODEL,
                            instruction=INVESTIGATOR_INSTRUCTION, tools=tools)
    casefile = LlmAgent(name="casefile", model=MODEL,
                        instruction=CASEFILE_INSTRUCTION, tools=tools)
    hitl = node(request_approval, name="hitl_approval", rerun_on_resume=False)

    return Workflow(
        name="fraud_desk",
        edges=[(START, triage, investigator, casefile, hitl)],
    )
