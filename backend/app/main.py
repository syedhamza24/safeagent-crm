"""Web API for SafeAgent CRM.

Endpoints
  GET  /health
  GET  /customers          demo customers for the picker
  POST /chat               send a customer message through the safe pipeline
  GET  /audit              live audit log for the admin dashboard
  POST /approve/{id}       approve a paused action
  POST /reject/{id}        reject a paused action
  POST /reset              reset the demo data

Protections for a public free-tier demo:
  - per-IP rate limit on /chat
  - global daily cap on AI calls (so free quota is never exhausted by one visitor)
  - strict input validation (length limits, history can only be user/assistant)
  - optional admin token for approve/reject (set ADMIN_TOKEN to enable)
"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Callable, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import db
from .agent import ApprovalError, build_graph, handle_message, resolve_approval
from .llm import propose
from .schemas import CUSTOMER_ID

load_dotenv()


# ---------------------------------------------------------------- small helpers

class RateLimiter:
    """Sliding-window limiter: at most `limit` hits per `window` seconds per key."""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


class DailyBudget:
    """Global cap on AI calls per UTC day. Protects the free quota."""

    def __init__(self, limit: int):
        self.limit = limit
        self._day = ""
        self._used = 0
        self._lock = Lock()

    def take(self) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            if today != self._day:
                self._day, self._used = today, 0
            if self._used >= self.limit:
                return False
            self._used += 1
            return True


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")  # set by hosts like Render/Railway
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------- request models

class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]  # no "system": visitors can't inject system messages
    content: str = Field(max_length=500)


class ChatRequest(BaseModel):
    customer_id: str = Field(pattern=CUSTOMER_ID)
    message: str = Field(min_length=1, max_length=500)
    history: list[HistoryItem] = Field(default_factory=list, max_length=20)


# ---------------------------------------------------------------- app factory

def create_app(
    db_path: Path | str | None = None,
    propose_fn: Callable | None = None,
    admin_token: str | None = None,
    chat_rate_per_min: int | None = None,
    daily_budget: int | None = None,
) -> FastAPI:
    admin_token = admin_token if admin_token is not None else os.environ.get("ADMIN_TOKEN", "")
    chat_limiter = RateLimiter(chat_rate_per_min or int(os.environ.get("CHAT_RATE_PER_MIN", "10")))
    admin_limiter = RateLimiter(60)
    budget = DailyBudget(daily_budget or int(os.environ.get("DAILY_LLM_BUDGET", "300")))
    propose_impl = propose_fn or propose

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        conn = db.get_conn(db_path)
        db.seed(conn)  # fresh demo data every start
        conn.close()
        yield

    app = FastAPI(title="SafeAgent CRM", version="1.0.0", lifespan=lifespan)

    origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    def get_db():
        conn = db.get_conn(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        if admin_token and x_admin_token != admin_token:
            raise HTTPException(status_code=401, detail="Invalid or missing admin token.")

    def check_admin_rate(request: Request) -> None:
        if not admin_limiter.allow(client_ip(request)):
            raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")

    # ------------------------------------------------------------ routes

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/customers")
    def customers(conn=Depends(get_db)) -> list[dict]:
        rows = conn.execute("SELECT id, name, plan_tier, deleted FROM customers ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    @app.post("/chat")
    def chat(req: ChatRequest, request: Request, conn=Depends(get_db)) -> dict:
        row = conn.execute("SELECT deleted FROM customers WHERE id = ?", (req.customer_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Customer not found.")
        if row["deleted"]:
            raise HTTPException(status_code=403, detail="This demo account was deleted. Use Reset to restore it.")
        if not chat_limiter.allow(client_ip(request)):
            raise HTTPException(status_code=429, detail="Too many messages. Please wait a minute and try again.")
        if not budget.take():
            raise HTTPException(
                status_code=429,
                detail="The demo has reached its daily AI limit. Please try again tomorrow.",
            )
        graph = build_graph(conn, propose_impl)
        history = [h.model_dump() for h in req.history[-6:]]
        return handle_message(graph, req.customer_id, req.message, history)

    @app.get("/audit")
    def audit(limit: int = 50, conn=Depends(get_db)) -> list[dict]:
        return db.list_audit(conn, limit=max(1, min(limit, 200)))

    def _resolve(audit_id: int, approve: bool, conn) -> dict:
        try:
            return resolve_approval(conn, audit_id, approve=approve, admin="dashboard")
        except ApprovalError as exc:
            code = 404 if "not found" in str(exc).lower() else 409
            raise HTTPException(status_code=code, detail=str(exc))

    @app.post("/approve/{audit_id}", dependencies=[Depends(require_admin), Depends(check_admin_rate)])
    def approve(audit_id: int, conn=Depends(get_db)) -> dict:
        return _resolve(audit_id, True, conn)

    @app.post("/reject/{audit_id}", dependencies=[Depends(require_admin), Depends(check_admin_rate)])
    def reject(audit_id: int, conn=Depends(get_db)) -> dict:
        return _resolve(audit_id, False, conn)

    @app.post("/reset", dependencies=[Depends(check_admin_rate)])
    def reset(conn=Depends(get_db)) -> dict:
        db.seed(conn)
        return {"status": "reset"}

    return app


app = create_app()
