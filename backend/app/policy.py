"""Deterministic policy engine: the 'secret sauce'.

Pure function, no LLM, no I/O. Same input -> same decision, always. That is what
makes it auditable and unit-testable. Thresholds live in one config object so a
client can tune them without touching logic.
"""
from __future__ import annotations

from dataclasses import dataclass


from .schemas import (
    DeleteRecordArgs,
    Decision,
    PolicyResult,
    ProcessRefundArgs,
    QueryOrderStatusArgs,
    RiskLevel,
    SessionContext,
    Strict,
    ToolName,
    UpdateCustomerRecordArgs,
)


@dataclass(frozen=True)
class PolicyConfig:
    auto_refund_limit_cents: int = 5_000        # <= $50 auto-executes
    critical_refund_cents: int = 20_000         # > $200 is Critical
    daily_refund_cap_cents: int = 100_000       # $1,000 per customer per day
    sensitive_fields: frozenset[str] = frozenset({"email", "plan_tier"})


DEFAULT_CONFIG = PolicyConfig()

_SCORES = {RiskLevel.LOW: 10, RiskLevel.MEDIUM: 50, RiskLevel.CRITICAL: 90}
_ESCALATE = {RiskLevel.LOW: RiskLevel.MEDIUM, RiskLevel.MEDIUM: RiskLevel.CRITICAL, RiskLevel.CRITICAL: RiskLevel.CRITICAL}


def _result(risk: RiskLevel, decision: Decision, reasons: list[str]) -> PolicyResult:
    return PolicyResult(risk=risk, score=_SCORES[risk], decision=decision, reasons=reasons)


def _block(reason: str) -> PolicyResult:
    return _result(RiskLevel.CRITICAL, Decision.BLOCK, [reason])


def evaluate(
    tool: ToolName,
    args: Strict,
    ctx: SessionContext,
    cfg: PolicyConfig = DEFAULT_CONFIG,
) -> PolicyResult:
    """Return the decision for a *validated* tool call."""
    result = _evaluate_inner(tool, args, ctx, cfg)

    # A prompt-injection signal never lowers risk and never allows auto-execution.
    if ctx.injection_suspected and result.decision != Decision.BLOCK:
        risk = _ESCALATE[result.risk]
        return _result(risk, Decision.REQUIRE_APPROVAL, result.reasons + ["Possible prompt injection detected in user input"])
    return result


def _evaluate_inner(tool: ToolName, args: Strict, ctx: SessionContext, cfg: PolicyConfig) -> PolicyResult:
    if tool == ToolName.QUERY_ORDER_STATUS:
        assert isinstance(args, QueryOrderStatusArgs)
        if ctx.order is None or ctx.order.customer_id != ctx.customer_id:
            return _block("Order does not belong to this customer")
        return _result(RiskLevel.LOW, Decision.AUTO_EXECUTE, ["Read-only action"])

    if tool == ToolName.UPDATE_CUSTOMER_RECORD:
        assert isinstance(args, UpdateCustomerRecordArgs)
        if args.customer_id != ctx.customer_id:
            return _block("Cannot modify another customer's record")
        if args.field in cfg.sensitive_fields:
            return _result(RiskLevel.MEDIUM, Decision.REQUIRE_APPROVAL, [f"Sensitive field '{args.field}' change"])
        return _result(RiskLevel.LOW, Decision.AUTO_EXECUTE, [f"Low-risk field '{args.field}' change"])

    if tool == ToolName.PROCESS_REFUND:
        assert isinstance(args, ProcessRefundArgs)
        return _refund(args, ctx, cfg)

    if tool == ToolName.DELETE_RECORD:
        assert isinstance(args, DeleteRecordArgs)
        if args.customer_id != ctx.customer_id:
            return _block("Cannot delete another customer's record")
        return _result(RiskLevel.CRITICAL, Decision.REQUIRE_APPROVAL, ["Destructive and irreversible action"])

    return _block(f"Unknown tool '{tool}'")


def _refund(args: ProcessRefundArgs, ctx: SessionContext, cfg: PolicyConfig) -> PolicyResult:
    order = ctx.order
    if order is None or order.order_id != args.order_id:
        return _block("Order not found")
    if order.customer_id != ctx.customer_id:
        return _block("Order does not belong to this customer")
    if args.amount_cents > order.refundable_cents:
        return _block(
            f"Refund {args.amount_cents}c exceeds refundable balance {order.refundable_cents}c"
        )

    reasons: list[str] = []
    if args.amount_cents <= cfg.auto_refund_limit_cents:
        risk = RiskLevel.LOW
        reasons.append("Refund within auto-approval limit")
    elif args.amount_cents <= cfg.critical_refund_cents:
        risk = RiskLevel.MEDIUM
        reasons.append("Refund above auto-approval limit")
    else:
        risk = RiskLevel.CRITICAL
        reasons.append("Large refund")

    if ctx.refunded_today_cents + args.amount_cents > cfg.daily_refund_cap_cents:
        risk = RiskLevel.CRITICAL
        reasons.append("Would exceed daily refund cap for this customer")

    decision = Decision.AUTO_EXECUTE if risk == RiskLevel.LOW else Decision.REQUIRE_APPROVAL
    return _result(risk, decision, reasons)
