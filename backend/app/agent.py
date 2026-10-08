"""The SafeAgent pipeline (LangGraph).

    message -> detect_injection -> propose (LLM) -> validate -> load_context -> policy_gate
                                                                                   |
                         +-------------------------+---------------------------+
                         |                         |                           |
                      execute                 queue_approval                  block

The LLM only proposes. Everything after `propose` is deterministic code.
Replies to the customer are fixed templates, never LLM text, so the LLM cannot
promise things (like a refund) that the system did not actually do.

Approvals live in the database (audit_log), not in memory, so a paused request
survives restarts and works on hosts with temporary disks.
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from . import db
from .injection import detect_injection
from .llm import Proposal, propose
from .policy import evaluate
from .schemas import TOOL_ARG_MODELS, Decision, PolicyResult, Strict, ToolName
from .tools import ToolError, execute

ProposeFn = Callable[[list[dict], str], Proposal]


class AgentState(TypedDict, total=False):
    customer_id: str
    message: str
    history: list[dict]
    injection: bool
    injection_matches: list[str]
    proposal: Proposal | None
    tool: ToolName | None
    args: Strict | None
    policy: PolicyResult | None
    audit_id: int | None
    outcome: str      # executed | pending_approval | blocked | clarify | chat | error
    reply: str


# ---------------------------------------------------------------- reply templates

def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _success_reply(tool: ToolName, result: dict) -> str:
    if tool == ToolName.QUERY_ORDER_STATUS:
        return (
            f"Order {result['order_id']} is currently '{result['status']}'. "
            f"Total: {_money(result['total_cents'])}, refunded so far: {_money(result['refunded_cents'])}."
        )
    if tool == ToolName.UPDATE_CUSTOMER_RECORD:
        return f"Done! Your {result['field'].replace('_', ' ')} has been updated."
    if tool == ToolName.PROCESS_REFUND:
        return f"Your refund of {_money(result['amount_cents'])} for order {result['order_id']} has been processed."
    if tool == ToolName.DELETE_RECORD:
        return "Your account has been deleted."
    return "Done."


# ---------------------------------------------------------------- the graph

def build_graph(conn: sqlite3.Connection, propose_fn: ProposeFn = propose):
    def n_detect(state: AgentState) -> dict:
        res = detect_injection(state["message"])
        return {"injection": res.suspected, "injection_matches": res.matches}

    def n_propose(state: AgentState) -> dict:
        messages = [*state.get("history", []), {"role": "user", "content": state["message"]}]
        proposal = propose_fn(messages, state["customer_id"])
        if proposal.error:
            return {"proposal": proposal, "outcome": "error",
                    "reply": "Sorry, I'm having trouble right now. Please try again in a moment."}
        if proposal.tool is None:
            return {"proposal": proposal, "outcome": "chat", "reply": proposal.reply or "How can I help?"}
        return {"proposal": proposal}

    def n_validate(state: AgentState) -> dict:
        proposal = state["proposal"]
        try:
            tool = ToolName(proposal.tool)
        except ValueError:
            # The LLM invented a tool. Log it as blocked: it is a notable event.
            audit_id = db.create_audit(
                conn, customer_id=state["customer_id"], user_message=state["message"],
                tool=str(proposal.tool), args=proposal.args or {}, risk="critical", score=90,
                decision="block", reasons=["Unknown tool requested"], status="blocked",
            )
            return {"outcome": "blocked", "audit_id": audit_id,
                    "reply": "I'm not able to help with that request."}
        try:
            args = TOOL_ARG_MODELS[tool].model_validate(proposal.args or {})
        except ValidationError:
            return {"outcome": "clarify",
                    "reply": "I need a bit more detail to do that. Could you give me the exact order number "
                             "(like ORD-1001) and any amount or value involved?"}
        return {"tool": tool, "args": args}

    def n_load_context(state: AgentState) -> dict:
        order_id = getattr(state["args"], "order_id", None)
        ctx = db.build_context(conn, state["customer_id"], order_id, state.get("injection", False))
        return {"policy": evaluate(state["tool"], state["args"], ctx)}

    def _audit(state: AgentState, status: str, result: dict | None = None, key: str | None = None) -> int:
        policy: PolicyResult = state["policy"]
        return db.create_audit(
            conn, customer_id=state["customer_id"], user_message=state["message"],
            tool=state["tool"].value, args=state["args"].model_dump(),
            risk=policy.risk.value, score=policy.score, decision=policy.decision.value,
            reasons=policy.reasons, status=status, result=result, idempotency_key=key,
        )

    def n_execute(state: AgentState) -> dict:
        key = str(uuid.uuid4())
        try:
            result = execute(conn, state["tool"], state["args"], key)
        except ToolError as exc:
            audit_id = _audit(state, "failed", {"error": str(exc)}, key)
            return {"outcome": "error", "audit_id": audit_id,
                    "reply": "Sorry, I couldn't complete that. Please check the details and try again."}
        audit_id = _audit(state, "auto_executed", result, key)
        return {"outcome": "executed", "audit_id": audit_id, "reply": _success_reply(state["tool"], result)}

    def n_queue(state: AgentState) -> dict:
        audit_id = _audit(state, "pending_approval", key=str(uuid.uuid4()))
        return {"outcome": "pending_approval", "audit_id": audit_id,
                "reply": f"This request needs a quick review by our team before it can go through. "
                         f"I've sent it for approval (reference #{audit_id})."}

    def n_block(state: AgentState) -> dict:
        audit_id = _audit(state, "blocked")
        return {"outcome": "blocked", "audit_id": audit_id,
                "reply": "I'm sorry, I'm not able to complete that request."}

    def after_propose(state: AgentState) -> Literal["validate", "end"]:
        return "end" if state.get("outcome") in {"error", "chat"} else "validate"

    def after_validate(state: AgentState) -> Literal["load_context", "end"]:
        return "end" if state.get("outcome") in {"blocked", "clarify"} else "load_context"

    def route(state: AgentState) -> Literal["execute", "queue", "block"]:
        decision = state["policy"].decision
        if decision == Decision.AUTO_EXECUTE:
            return "execute"
        if decision == Decision.REQUIRE_APPROVAL:
            return "queue"
        return "block"

    g = StateGraph(AgentState)
    g.add_node("detect_injection", n_detect)
    g.add_node("propose", n_propose)
    g.add_node("validate", n_validate)
    g.add_node("policy_gate", n_load_context)
    g.add_node("execute", n_execute)
    g.add_node("queue", n_queue)
    g.add_node("block", n_block)

    g.add_edge(START, "detect_injection")
    g.add_edge("detect_injection", "propose")
    g.add_conditional_edges("propose", after_propose, {"validate": "validate", "end": END})
    g.add_conditional_edges("validate", after_validate, {"load_context": "policy_gate", "end": END})
    g.add_conditional_edges("policy_gate", route, {"execute": "execute", "queue": "queue", "block": "block"})
    for node in ("execute", "queue", "block"):
        g.add_edge(node, END)
    return g.compile()


def handle_message(graph: Any, customer_id: str, message: str, history: list[dict] | None = None) -> dict:
    """Run one customer message through the pipeline."""
    final = graph.invoke({"customer_id": customer_id, "message": message, "history": history or []})
    policy = final.get("policy")
    return {
        "outcome": final["outcome"],
        "reply": final["reply"],
        "audit_id": final.get("audit_id"),
        "risk": policy.risk.value if policy else None,
        "decision": policy.decision.value if policy else None,
    }


# ---------------------------------------------------------------- human approval

class ApprovalError(Exception):
    pass


def resolve_approval(conn: sqlite3.Connection, audit_id: int, approve: bool, admin: str = "admin") -> dict:
    """Approve or reject a paused action. Approving RE-CHECKS everything against fresh data."""
    row = db.get_audit(conn, audit_id)
    if row is None:
        raise ApprovalError("Audit entry not found")
    if row["status"] != "pending_approval":
        raise ApprovalError(f"Entry is already '{row['status']}'")

    if not approve:
        db.update_audit(conn, audit_id, status="rejected", resolved_by=admin)
        return {"status": "rejected"}

    tool = ToolName(row["tool"])
    args = TOOL_ARG_MODELS[tool].model_validate(row["args"])

    # Data may have changed since the request was paused: check again.
    order_id = getattr(args, "order_id", None)
    ctx = db.build_context(conn, row["customer_id"], order_id)
    policy = evaluate(tool, args, ctx)
    if policy.decision == Decision.BLOCK:
        db.update_audit(conn, audit_id, status="blocked",
                        result={"error": "Re-check failed", "reasons": policy.reasons}, resolved_by=admin)
        return {"status": "blocked", "reasons": policy.reasons}

    try:
        result = execute(conn, tool, args, row["idempotency_key"])
    except ToolError as exc:
        db.update_audit(conn, audit_id, status="failed", result={"error": str(exc)}, resolved_by=admin)
        return {"status": "failed", "error": str(exc)}

    db.update_audit(conn, audit_id, status="approved_executed", result=result, resolved_by=admin)
    return {"status": "approved_executed", "result": result}
