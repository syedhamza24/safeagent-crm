# SafeAgent CRM

An AI support agent that can act on a CRM, but cannot act unsafely.
The LLM only *proposes* tool calls. A deterministic policy engine decides what actually runs.

```
Customer msg -> [LangGraph agent] -> proposed tool call
                                       |
                              Pydantic strict validation
                                       |
                        Deterministic policy engine (no LLM)
                    /               |                    \
             AUTO_EXECUTE     REQUIRE_APPROVAL          BLOCK
             (run now)        (graph interrupt()        (refuse, log,
                               + Slack ping)             tell customer)
                                       |
                           Admin approves / rejects
                                       |
                    Re-validate + re-run policy, then execute
                    (idempotency key prevents double refunds)
```

## What makes it different
1. **Policy is code, not a prompt.** Same input, same decision. 100% unit-testable.
2. **Trusted context vs. untrusted input.** Customer ID and order data come from the session/DB, never from the LLM. Cross-account access is blocked even if the model is fooled.
3. **Cumulative limits.** Ten "small" $40 refunds can't bypass the daily cap.
4. **Injection signal escalates risk** and can never lower it or unblock an action.
5. **Approval re-checks at execution time**, with idempotency keys.
6. **Adversarial eval suite** with a published pass rate.

## Repo layout
```
safeagent-crm/
  backend/
    app/
      schemas.py        # DONE  strict tool args + decision types
      policy.py         # DONE  deterministic engine
      injection.py      # TODO  cheap regex/heuristic detector -> ctx.injection_suspected
      db.py             # TODO  SQLite: customers, orders, audit_log, idempotency_keys
      tools.py          # TODO  real executors (mock CRM; Stripe test-mode refund)
      graph.py          # TODO  LangGraph state machine (below)
      llm.py            # TODO  provider-agnostic wrapper (env: LLM_PROVIDER, LLM_MODEL)
      notify.py         # TODO  Slack incoming webhook
      main.py           # TODO  FastAPI: /chat, /audit, /approve/{id}, /reject/{id}, /events (SSE)
    tests/
      test_policy.py    # DONE  18 passing
      test_evals.py     # TODO  adversarial prompt suite (runs graph, asserts no unsafe execution)
  frontend/             # TODO  Next.js: chat left, live audit log right
```

## LangGraph design

State:
```python
class AgentState(TypedDict):
    messages: list            # conversation
    session: SessionContext   # trusted, injected by backend
    proposal: dict | None     # {tool, args} from the LLM
    policy: PolicyResult | None
    audit_id: int | None
    outcome: str | None       # executed | pending | blocked | clarify
```

Nodes: `detect_injection` -> `propose` (LLM w/ tool calling) -> `validate` (Pydantic; on failure ask customer to clarify)
-> `load_context` (fetch order, refunded_today) -> `policy_gate` -> conditional edge:
- AUTO_EXECUTE -> `execute` -> `respond`
- REQUIRE_APPROVAL -> `request_approval` (write audit row, Slack, `interrupt()`) -> on resume `revalidate` -> `execute` or `respond` (rejected)
- BLOCK -> `respond` (polite refusal, audit row)

Use a SQLite/Postgres checkpointer so paused runs survive restarts.

## Audit log row
`id, ts, customer_id, tool, args_json, before_json, after_json (payload diff), risk, score, decision, reasons, status, approved_by, idempotency_key`

## Build order (7 to 10 days)
1. Day 1-2: schemas + policy (done), db, tools, injection detector
2. Day 3-4: LangGraph + llm wrapper, test with CLI
3. Day 5: FastAPI endpoints, Slack, SSE
4. Day 6-7: Next.js UI (chat, audit table with diff view, Approve/Reject, attack-preset buttons)
5. Day 8-9: eval suite (target 25+ cases), rate limit + daily LLM budget cap, deploy (Next.js on Vercel, API on Render/Railway/Fly)
6. Day 10: README results table, architecture diagram, 90-sec Loom

## Run tests
```
cd backend && pip install pydantic pytest && pytest -q
```
