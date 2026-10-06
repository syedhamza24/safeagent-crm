"""Strict schemas for every tool the agent may *propose*.

The LLM never executes anything. It emits a tool name + JSON args, and those args
must survive these models (extra fields forbidden, types strict) before the policy
engine even looks at them. Money is always integer cents: no float rounding bugs.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ToolName(str, Enum):
    QUERY_ORDER_STATUS = "query_order_status"
    UPDATE_CUSTOMER_RECORD = "update_customer_record"
    PROCESS_REFUND = "process_refund"
    DELETE_RECORD = "delete_record"


ORDER_ID = r"^ORD-\d{4,8}$"
CUSTOMER_ID = r"^CUS-\d{3,8}$"


class QueryOrderStatusArgs(Strict):
    order_id: str = Field(pattern=ORDER_ID)


class UpdateCustomerRecordArgs(Strict):
    customer_id: str = Field(pattern=CUSTOMER_ID)
    field: Literal["shipping_address", "phone", "email", "plan_tier"]
    new_value: str = Field(min_length=1, max_length=200)


class ProcessRefundArgs(Strict):
    order_id: str = Field(pattern=ORDER_ID)
    amount_cents: int = Field(gt=0, le=10_000_000)
    reason: str = Field(min_length=3, max_length=300)


class DeleteRecordArgs(Strict):
    customer_id: str = Field(pattern=CUSTOMER_ID)
    reason: str = Field(min_length=3, max_length=300)


TOOL_ARG_MODELS: dict[ToolName, type[Strict]] = {
    ToolName.QUERY_ORDER_STATUS: QueryOrderStatusArgs,
    ToolName.UPDATE_CUSTOMER_RECORD: UpdateCustomerRecordArgs,
    ToolName.PROCESS_REFUND: ProcessRefundArgs,
    ToolName.DELETE_RECORD: DeleteRecordArgs,
}


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    CRITICAL = "critical"


class Decision(str, Enum):
    AUTO_EXECUTE = "auto_execute"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


class PolicyResult(BaseModel):
    risk: RiskLevel
    score: int = Field(ge=0, le=100)
    decision: Decision
    reasons: list[str]


class OrderSnapshot(BaseModel):
    """What the executor knows about an order at decision time (fetched by backend, never by the LLM)."""
    order_id: str
    customer_id: str
    total_cents: int
    refunded_cents: int = 0

    @property
    def refundable_cents(self) -> int:
        return max(self.total_cents - self.refunded_cents, 0)


class SessionContext(BaseModel):
    """Trusted facts about who is chatting. Comes from auth/session, NOT from the LLM."""
    customer_id: str
    order: OrderSnapshot | None = None
    refunded_today_cents: int = 0
    injection_suspected: bool = False
