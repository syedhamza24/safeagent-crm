"""Tiny SQLite database: fake customers + orders for the demo.

Uses Python's built-in sqlite3, so there is nothing extra to install.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .schemas import OrderSnapshot, SessionContext

DB_PATH = Path(__file__).resolve().parent.parent / "safeagent.db"


def get_conn(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS customers;
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS refunds;
        DROP TABLE IF EXISTS audit_log;

        CREATE TABLE customers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            shipping_address TEXT NOT NULL,
            plan_tier TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE orders (
            id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            total_cents INTEGER NOT NULL,
            refunded_cents INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL
        );

        CREATE TABLE refunds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            idempotency_key TEXT UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            customer_id TEXT NOT NULL,
            user_message TEXT NOT NULL,
            tool TEXT NOT NULL,
            args_json TEXT NOT NULL,
            risk TEXT NOT NULL,
            score INTEGER NOT NULL,
            decision TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            idempotency_key TEXT,
            resolved_by TEXT,
            resolved_at TEXT
        );
        """
    )
    conn.commit()


def seed(conn: sqlite3.Connection) -> None:
    """Reset the demo data. Safe to call any time."""
    init_db(conn)
    conn.executemany(
        "INSERT INTO customers (id, name, email, phone, shipping_address, plan_tier) VALUES (?,?,?,?,?,?)",
        [
            ("CUS-001", "Alice Johnson", "alice@example.com", "555-0101", "12 Oak St, Austin TX", "pro"),
            ("CUS-002", "Bob Smith", "bob@example.com", "555-0102", "88 Pine Ave, Denver CO", "basic"),
            ("CUS-003", "Carla Mendes", "carla@example.com", "555-0103", "5 Harbor Rd, Miami FL", "enterprise"),
        ],
    )
    conn.executemany(
        "INSERT INTO orders (id, customer_id, total_cents, refunded_cents, status) VALUES (?,?,?,?,?)",
        [
            ("ORD-1001", "CUS-001", 4_500, 0, "shipped"),
            ("ORD-1002", "CUS-001", 30_000, 0, "delivered"),
            ("ORD-1003", "CUS-002", 12_000, 0, "processing"),
            ("ORD-1004", "CUS-002", 80_000, 0, "delivered"),
            ("ORD-1005", "CUS-003", 25_000, 5_000, "delivered"),
        ],
    )
    conn.commit()


def get_order(conn: sqlite3.Connection, order_id: str) -> OrderSnapshot | None:
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        return None
    return OrderSnapshot(
        order_id=row["id"],
        customer_id=row["customer_id"],
        total_cents=row["total_cents"],
        refunded_cents=row["refunded_cents"],
    )


def refunded_today_cents(conn: sqlite3.Connection, customer_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM refunds "
        "WHERE customer_id = ? AND date(created_at) = date('now')",
        (customer_id,),
    ).fetchone()
    return int(row["total"])


def build_context(
    conn: sqlite3.Connection,
    customer_id: str,
    order_id: str | None = None,
    injection_suspected: bool = False,
) -> SessionContext:
    """Collect the TRUSTED facts the policy engine needs. Never comes from the LLM."""
    return SessionContext(
        customer_id=customer_id,
        order=get_order(conn, order_id) if order_id else None,
        refunded_today_cents=refunded_today_cents(conn, customer_id),
        injection_suspected=injection_suspected,
    )


# ---------------------------------------------------------------- audit log

def create_audit(
    conn: sqlite3.Connection,
    *,
    customer_id: str,
    user_message: str,
    tool: str,
    args: dict,
    risk: str,
    score: int,
    decision: str,
    reasons: list[str],
    status: str,
    result: dict | None = None,
    idempotency_key: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO audit_log (customer_id, user_message, tool, args_json, risk, score, decision, "
        "reasons_json, status, result_json, idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            customer_id,
            user_message,
            tool,
            json.dumps(args),
            risk,
            score,
            decision,
            json.dumps(reasons),
            status,
            json.dumps(result) if result is not None else None,
            idempotency_key,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_audit(
    conn: sqlite3.Connection,
    audit_id: int,
    *,
    status: str,
    result: dict | None = None,
    resolved_by: str | None = None,
) -> None:
    conn.execute(
        "UPDATE audit_log SET status = ?, result_json = COALESCE(?, result_json), "
        "resolved_by = COALESCE(?, resolved_by), "
        "resolved_at = CASE WHEN ? IS NULL THEN resolved_at ELSE datetime('now') END WHERE id = ?",
        (status, json.dumps(result) if result is not None else None, resolved_by, resolved_by, audit_id),
    )
    conn.commit()


def _audit_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["args"] = json.loads(d.pop("args_json"))
    d["reasons"] = json.loads(d.pop("reasons_json"))
    rj = d.pop("result_json")
    d["result"] = json.loads(rj) if rj else None
    return d


def get_audit(conn: sqlite3.Connection, audit_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM audit_log WHERE id = ?", (audit_id,)).fetchone()
    return _audit_row_to_dict(row) if row else None


def list_audit(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_audit_row_to_dict(r) for r in rows]
