import pytest

from app.db import get_conn, get_order, seed
from app.schemas import (
    DeleteRecordArgs,
    ProcessRefundArgs,
    QueryOrderStatusArgs,
    ToolName,
    UpdateCustomerRecordArgs,
)
from app.tools import ToolError, execute


@pytest.fixture()
def conn(tmp_path):
    c = get_conn(tmp_path / "test.db")
    seed(c)
    yield c
    c.close()


def test_query_order_status(conn):
    r = execute(conn, ToolName.QUERY_ORDER_STATUS, QueryOrderStatusArgs(order_id="ORD-1001"))
    assert r["status"] == "shipped"


def test_query_missing_order(conn):
    with pytest.raises(ToolError):
        execute(conn, ToolName.QUERY_ORDER_STATUS, QueryOrderStatusArgs(order_id="ORD-9999"))


def test_update_returns_before_and_after(conn):
    args = UpdateCustomerRecordArgs(customer_id="CUS-001", field="shipping_address", new_value="1 New St")
    r = execute(conn, ToolName.UPDATE_CUSTOMER_RECORD, args)
    assert r["before"] == "12 Oak St, Austin TX"
    assert r["after"] == "1 New St"


def test_refund_updates_order(conn):
    args = ProcessRefundArgs(order_id="ORD-1002", amount_cents=10_000, reason="damaged item")
    r = execute(conn, ToolName.PROCESS_REFUND, args, idempotency_key="k1")
    assert r["duplicate"] is False
    assert get_order(conn, "ORD-1002").refunded_cents == 10_000


def test_same_idempotency_key_refunds_only_once(conn):
    args = ProcessRefundArgs(order_id="ORD-1002", amount_cents=10_000, reason="damaged item")
    execute(conn, ToolName.PROCESS_REFUND, args, idempotency_key="same")
    r2 = execute(conn, ToolName.PROCESS_REFUND, args, idempotency_key="same")
    assert r2["duplicate"] is True
    assert get_order(conn, "ORD-1002").refunded_cents == 10_000  # not 20,000


def test_refund_over_balance_is_rejected_at_execution(conn):
    args = ProcessRefundArgs(order_id="ORD-1001", amount_cents=999_999, reason="too much")
    with pytest.raises(ToolError):
        execute(conn, ToolName.PROCESS_REFUND, args)


def test_delete_is_soft_delete(conn):
    r = execute(conn, ToolName.DELETE_RECORD, DeleteRecordArgs(customer_id="CUS-002", reason="gdpr request"))
    assert r["deleted"] is True
    row = conn.execute("SELECT deleted FROM customers WHERE id='CUS-002'").fetchone()
    assert row["deleted"] == 1
    with pytest.raises(ToolError):  # can't delete twice
        execute(conn, ToolName.DELETE_RECORD, DeleteRecordArgs(customer_id="CUS-002", reason="gdpr request"))
