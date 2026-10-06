"""Cheap heuristic detector for prompt-injection style messages.

This is NOT the main defense (the policy engine is). It is an early-warning signal:
if it fires, the policy engine escalates the action to human approval.
It will never catch everything, and that is fine, because a missed injection still
has to pass the deterministic policy checks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_PATTERNS: dict[str, str] = {
    "ignore_instructions": r"\b(ignore|disregard|forget|override)\b.{0,30}\b(instructions?|rules?|polic(?:y|ies)|guidelines?|limits?|safety)\b",
    "role_hijack": r"\b(you are now|act as|pretend (to be|you are)|from now on you)\b",
    "reveal_prompt": r"\b(system prompt|developer message|your instructions|reveal your (prompt|rules))\b",
    "jailbreak": r"\b(jailbreak|developer mode|dan mode|no restrictions)\b",
    "skip_approval": r"\b(skip|bypass|without|no need for|don'?t (need|require))\b.{0,20}\b(approval|review|check|verification|human|confirmation)\b",
    "fake_authority": r"\b(admin|manager|ceo|supervisor|owner|support team)\b.{0,25}\b(approved|authori[sz]ed|said|told|confirmed|pre-?approved)\b",
    "mass_action": r"\b(all|every)\b.{0,15}\b(customers?|records?|orders?|accounts?|users?)\b",
}

_COMPILED = {name: re.compile(rx, re.IGNORECASE | re.DOTALL) for name, rx in _PATTERNS.items()}


@dataclass
class InjectionResult:
    suspected: bool
    matches: list[str] = field(default_factory=list)


def detect_injection(text: str) -> InjectionResult:
    normalized = " ".join(text.split())  # collapse whitespace/newlines
    matches = [name for name, rx in _COMPILED.items() if rx.search(normalized)]
    return InjectionResult(suspected=bool(matches), matches=matches)
