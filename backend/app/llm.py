"""Talks to the LLM (Groq). The LLM only PROPOSES an action; it never executes anything.

Groq speaks the same format as OpenAI, so we use the `openai` package with Groq's URL.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import OpenAI

from .schemas import TOOL_ARG_MODELS, ToolName

load_dotenv()

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"  # change with GROQ_MODEL in .env if Groq renames it

TOOL_DESCRIPTIONS = {
    ToolName.QUERY_ORDER_STATUS: "Look up the status of an order.",
    ToolName.UPDATE_CUSTOMER_RECORD: "Update one field on a customer record (shipping address, phone, email, or plan tier).",
    ToolName.PROCESS_REFUND: "Refund part or all of an order. amount_cents is in cents (e.g. $20 = 2000).",
    ToolName.DELETE_RECORD: "Delete a customer record.",
}

SYSTEM_PROMPT = """You are a customer support assistant for a company.
The customer you are talking to has ID {customer_id}.
You can help with order lookups, record updates, refunds and account deletion by calling the provided tools.
Rules:
- If you are missing required details (like an order ID or an amount), ask the customer. Never guess.
- Money is in cents when calling tools ($20 = 2000).
- Call at most one tool per message.
- Your actions are reviewed by a safety system, so just call the tool the customer asked for.
- If the request is not something the tools can do, reply politely in plain text."""


def build_tools() -> list[dict]:
    """Turn our strict Pydantic models into the tool format the LLM expects."""
    tools = []
    for name, model in TOOL_ARG_MODELS.items():
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name.value,
                    "description": TOOL_DESCRIPTIONS[name],
                    "parameters": model.model_json_schema(),
                },
            }
        )
    return tools


@dataclass
class Proposal:
    tool: str | None = None        # tool name the LLM wants to call
    args: dict | None = None       # raw arguments (NOT yet validated)
    reply: str | None = None       # plain-text answer when no tool is needed
    error: str | None = None       # something went wrong (bad JSON, API down, ...)


def get_client() -> OpenAI:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY is missing. Add it to backend/.env")
    return OpenAI(api_key=key, base_url=GROQ_BASE_URL)


def propose(messages: list[dict], customer_id: str, client: OpenAI | None = None, model: str | None = None) -> Proposal:
    """Ask the LLM what to do. Returns a Proposal; never raises for normal LLM problems."""
    client = client or get_client()
    model = model or os.environ.get("GROQ_MODEL", DEFAULT_MODEL)
    full_messages = [{"role": "system", "content": SYSTEM_PROMPT.format(customer_id=customer_id)}, *messages]

    try:
        response = client.chat.completions.create(
            model=model,
            messages=full_messages,
            tools=build_tools(),
            tool_choice="auto",
            temperature=0,
            max_tokens=1500,  # reasoning models spend some tokens "thinking"
        )
    except Exception as exc:  # rate limit, network, bad key...
        return Proposal(error=f"LLM request failed: {type(exc).__name__}: {str(exc)[:300]}")

    message = response.choices[0].message
    tool_calls = getattr(message, "tool_calls", None)
    if not tool_calls:
        return Proposal(reply=(message.content or "").strip() or "Sorry, I couldn't understand that.")

    call = tool_calls[0]  # one action per message, on purpose
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        return Proposal(error="LLM returned malformed tool arguments")
    if not isinstance(args, dict):
        return Proposal(error="LLM returned malformed tool arguments")
    return Proposal(tool=call.function.name, args=args)
