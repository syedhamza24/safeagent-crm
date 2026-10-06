import pytest
from pydantic import ValidationError

from app.policy import PolicyConfig, evaluate
from app.schemas import (
    DeleteRecordArgs,
    Decision,
    OrderSnapshot,
    ProcessRefundArgs,
    QueryOrderStatusArgs,
    RiskLevel,
    SessionContext,
    ToolName,
    UpdateCustomerRecordArgs,
)


def ctx(total=30_000, refunded=0, today=0, injection=False, owner="CUS-001"):
    return SessionContext(
        customer_id="CUS-001",
        order=OrderSnapshot(order_id="ORD-1234", customer_id=owner, total_cents=total, refunded_cents=refunded),
        refunded_today_cents=today,
        injection_suspected=injection,
    )


def refund(cents):
    return ProcessRefundArgs(order_id="ORD-1234", amount_cents=cents, reason="customer request")


# ---- schema validation ----

def test_rejects_extra_fields():
    with pytest.raises(ValidationError):
        ProcessRefundArgs(order_id="ORD-1234", amount_cents=100, reason="ok ok", override=True)


def test_rejects_float_money_and_bad_ids():
    with pytest.raises(ValidationError):
        ProcessRefundArgs(order_id="ORD-1234", amount_cents=50.5, reason="ok ok")
    with pytest.raises(ValidationError):
        QueryOrderStatusArgs(order_id="1234; DROP TABLE orders")


def test_rejects_non_positive_refund():
    with pytest.raises(ValidationError):
        refund(0)
    with pytest.raises(ValidationError):
        refund(-500)


# ---- reads ----

def test_read_is_auto_executed():
    r = evaluate(ToolName.QUERY_ORDER_STATUS, QueryOrderStatusArgs(order_id="ORD-1234"), ctx())
    assert (r.risk, r.decision) == (RiskLevel.LOW, Decision.AUTO_EXECUTE)


def test_read_of_someone_elses_order_is_blocked():
    r = evaluate(ToolName.QUERY_ORDER_STATUS, QueryOrderStatusArgs(order_id="ORD-1234"), ctx(owner="CUS-999"))
    assert r.decision == Decision.BLOCK


# ---- refunds ----

@pytest.mark.parametrize(
    "cents,risk,decision",
    [
        (5_000, RiskLevel.LOW, Decision.AUTO_EXECUTE),         # exactly $50
        (5_001, RiskLevel.MEDIUM, Decision.REQUIRE_APPROVAL),
        (20_000, RiskLevel.MEDIUM, Decision.REQUIRE_APPROVAL),  # exactly $200
        (20_001, RiskLevel.CRITICAL, Decision.REQUIRE_APPROVAL),
    ],
)
def test_refund_tiers(cents, risk, decision):
    r = evaluate(ToolName.PROCESS_REFUND, refund(cents), ctx(total=50_000))
    assert (r.risk, r.decision) == (risk, decision)


def test_refund_over_refundable_balance_is_blocked():
    r = evaluate(ToolName.PROCESS_REFUND, refund(25_000), ctx(total=30_000, refunded=10_000))
    assert r.decision == Decision.BLOCK


def test_refund_on_foreign_order_is_blocked():
    r = evaluate(ToolName.PROCESS_REFUND, refund(1_000), ctx(owner="CUS-999"))
    assert r.decision == Decision.BLOCK


def test_small_refunds_cannot_bypass_daily_cap():
    # $40 would normally auto-execute, but $980 already refunded today.
    r = evaluate(ToolName.PROCESS_REFUND, refund(4_000), ctx(today=98_000))
    assert (r.risk, r.decision) == (RiskLevel.CRITICAL, Decision.REQUIRE_APPROVAL)


def test_thresholds_are_configurable():
    cfg = PolicyConfig(auto_refund_limit_cents=1_000)
    r = evaluate(ToolName.PROCESS_REFUND, refund(2_000), ctx(), cfg)
    assert r.decision == Decision.REQUIRE_APPROVAL


# ---- record updates / deletes ----

def test_low_risk_field_auto_executes_sensitive_needs_approval():
    low = UpdateCustomerRecordArgs(customer_id="CUS-001", field="shipping_address", new_value="1 Main St")
    high = UpdateCustomerRecordArgs(customer_id="CUS-001", field="plan_tier", new_value="enterprise")
    assert evaluate(ToolName.UPDATE_CUSTOMER_RECORD, low, ctx()).decision == Decision.AUTO_EXECUTE
    assert evaluate(ToolName.UPDATE_CUSTOMER_RECORD, high, ctx()).decision == Decision.REQUIRE_APPROVAL


def test_cannot_update_other_customer():
    a = UpdateCustomerRecordArgs(customer_id="CUS-777", field="phone", new_value="123")
    assert evaluate(ToolName.UPDATE_CUSTOMER_RECORD, a, ctx()).decision == Decision.BLOCK


def test_delete_always_needs_approval_and_is_critical():
    a = DeleteRecordArgs(customer_id="CUS-001", reason="gdpr request")
    r = evaluate(ToolName.DELETE_RECORD, a, ctx())
    assert (r.risk, r.decision) == (RiskLevel.CRITICAL, Decision.REQUIRE_APPROVAL)


# ---- injection escalation ----

def test_injection_flag_downgrades_auto_execute_to_approval():
    r = evaluate(ToolName.PROCESS_REFUND, refund(1_000), ctx(injection=True))
    assert r.decision == Decision.REQUIRE_APPROVAL
    assert r.risk == RiskLevel.MEDIUM
    assert any("injection" in x.lower() for x in r.reasons)


def test_injection_never_unblocks():
    r = evaluate(ToolName.PROCESS_REFUND, refund(1_000), ctx(owner="CUS-999", injection=True))
    assert r.decision == Decision.BLOCK
