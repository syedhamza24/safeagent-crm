import pytest

from app.db import build_context, get_conn, get_order, seed


@pytest.fixture()
def conn(tmp_path):
    c = get_conn(tmp_path / "test.db")
    seed(c)
    yield c
    c.close()


def test_seed_creates_orders(conn):
    order = get_order(conn, "ORD-1002")
    assert order is not None
    assert order.customer_id == "CUS-001"
    assert order.total_cents == 30_000


def test_missing_order_returns_none(conn):
    assert get_order(conn, "ORD-9999") is None


def test_partial_refund_balance(conn):
    order = get_order(conn, "ORD-1005")
    assert order.refundable_cents == 20_000


def test_build_context_has_trusted_facts(conn):
    ctx = build_context(conn, "CUS-001", "ORD-1001")
    assert ctx.customer_id == "CUS-001"
    assert ctx.order.order_id == "ORD-1001"
    assert ctx.refunded_today_cents == 0


def test_refunded_today_sums_todays_refunds(conn):
    conn.execute("INSERT INTO refunds (order_id, customer_id, amount_cents) VALUES ('ORD-1002','CUS-001',4000)")
    conn.execute("INSERT INTO refunds (order_id, customer_id, amount_cents) VALUES ('ORD-1002','CUS-001',1000)")
    conn.commit()
    assert build_context(conn, "CUS-001").refunded_today_cents == 5_000


def test_seed_can_be_reset(conn):
    conn.execute("UPDATE orders SET refunded_cents = 999 WHERE id = 'ORD-1001'")
    conn.commit()
    seed(conn)
    assert get_order(conn, "ORD-1001").refunded_cents == 0
