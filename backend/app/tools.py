"""The actions that actually change things.

IMPORTANT: Only the backend calls these, and only AFTER the policy engine said OK
(or a human approved). The LLM never gets to call them directly.
"""
from __future__ import annotations

import sqlite3

from .schemas import (
    DeleteRecordArgs,
    ProcessRefundArgs,
    QueryOrderStatusArgs,
    Strict,
    ToolName,
    UpdateCustomerRecordArgs,
)

ALLOWED_FIELDS = {"shipping_address", "phone", "email", "plan_tier"}


class ToolError(Exception):
    """Raised when an action cannot be performed (bad data, not found, etc)."""


def query_order_status(conn: sqlite3.Connection, args: QueryOrderStatusArgs) -> dict:
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (args.order_id,)).fetchone()
    if row is None:
        raise ToolError("Order not found")
    return {
        "order_id": row["id"],
        "status": row["status"],
        "total_cents": row["total_cents"],
        "refunded_cents": row["refunded_cents"],
    }


def update_customer_record(conn: sqlite3.Connection, args: UpdateCustomerRecordArgs) -> dict:
    if args.field not in ALLOWED_FIELDS:  # defense in depth (schema already limits this)
        raise ToolError("Field not allowed")
    row = conn.execute(
        "SELECT * FROM customers WHERE id = ? AND deleted = 0", (args.customer_id,)
    ).fetchone()
    if row is None:
        raise ToolError("Customer not found")
    before = row[args.field]
    conn.execute(f"UPDATE customers SET {args.field} = ? WHERE id = ?", (args.new_value, args.customer_id))
    conn.commit()
    # before/after is the "payload diff" shown in the admin dashboard
    return {"customer_id": args.customer_id, "field": args.field, "before": before, "after": args.new_value}


def process_refund(conn: sqlite3.Connection, args: ProcessRefundArgs, idempotency_key: str | None = None) -> dict:
    # Same key twice = same refund, never a second one (protects against double-clicks).
    if idempotency_key:
        existing = conn.execute(
            "SELECT * FROM refunds WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            return {
                "order_id": existing["order_id"],
                "amount_cents": existing["amount_cents"],
                "duplicate": True,
            }

    order = conn.execute("SELECT * FROM orders WHERE id = ?", (args.order_id,)).fetchone()
    if order is None:
        raise ToolError("Order not found")
    refundable = order["total_cents"] - order["refunded_cents"]
    if args.amount_cents > refundable:  # re-check at execution time, state may have changed
        raise ToolError("Refund exceeds refundable balance")

    conn.execute(
        "INSERT INTO refunds (order_id, customer_id, amount_cents, idempotency_key) VALUES (?,?,?,?)",
        (args.order_id, order["customer_id"], args.amount_cents, idempotency_key),
    )
    conn.execute(
        "UPDATE orders SET refunded_cents = refunded_cents + ? WHERE id = ?",
        (args.amount_cents, args.order_id),
    )
    conn.commit()
    return {
        "order_id": args.order_id,
        "amount_cents": args.amount_cents,
        "refunded_total_cents": order["refunded_cents"] + args.amount_cents,
        "duplicate": False,
    }


def delete_record(conn: sqlite3.Connection, args: DeleteRecordArgs) -> dict:
    row = conn.execute(
        "SELECT * FROM customers WHERE id = ? AND deleted = 0", (args.customer_id,)
    ).fetchone()
    if row is None:
        raise ToolError("Customer not found")
    conn.execute("UPDATE customers SET deleted = 1 WHERE id = ?", (args.customer_id,))  # soft delete
    conn.commit()
    return {"customer_id": args.customer_id, "deleted": True}


def execute(conn: sqlite3.Connection, tool: ToolName, args: Strict, idempotency_key: str | None = None) -> dict:
    if tool == ToolName.QUERY_ORDER_STATUS:
        return query_order_status(conn, args)  # type: ignore[arg-type]
    if tool == ToolName.UPDATE_CUSTOMER_RECORD:
        return update_customer_record(conn, args)  # type: ignore[arg-type]
    if tool == ToolName.PROCESS_REFUND:
        return process_refund(conn, args, idempotency_key)  # type: ignore[arg-type]
    if tool == ToolName.DELETE_RECORD:
        return delete_record(conn, args)  # type: ignore[arg-type]
    raise ToolError(f"Unknown tool {tool}")
