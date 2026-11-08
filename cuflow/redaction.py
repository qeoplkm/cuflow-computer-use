"""Redaction of regulated data before anything is persisted (logs, artifacts, evidence).

Three layers, applied in order:

1. **Known values** — exact values we know are sensitive in this run: secrets, inputs and
   outputs whose spec says pii/financial/secret, and values the surface found next to PII
   labels ("Name:", "Tax ID:", "Phone:"). This is the most reliable layer; it is also the
   only one that catches names, which no regex can.
2. **Patterns** — SSNs, card numbers (Luhn-checked), emails, phone numbers, long account
   numbers, and money amounts.
3. **Structure** — callers never log raw observations; they log redacted text, and
   password fields are masked by the surface before Python ever sees them.

Limits are documented in REPORT.md (free-text PII in unlabeled positions is best effort).
"""

from __future__ import annotations

import re
import html
from typing import Any


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone", re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}|\b\d{3}[-.]\d{3}[-.]\d{4}\b")),
    ("money", re.compile(r"(?<![\w.])-?\$?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\b")),
]
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
LONG_NUMBER = re.compile(r"\b\d{9,}\b")
URL_TOKEN = re.compile(r"([?&](?:amp;)?(?:t|k|token|csrf|nonce|session(?:id)?|access_token)=)[^&#\s\"'<>]+", re.I)


class Redactor:
    def __init__(self) -> None:
        self._known: dict[str, str] = {}

    def add(self, value: str | None, label: str) -> None:
        if value is None:
            return
        v = str(value).strip()
        if len(v) >= 3:  # never redact trivially short strings (would shred normal text)
            self._known[v] = label
            self._known[html.escape(v, quote=True)] = label

    @property
    def known_values(self) -> list[str]:
        return list(self._known)

    def add_many(self, values: list[str], label: str) -> None:
        for v in values:
            self.add(v, label)

    def text(self, s: str) -> str:
        if not s:
            return s
        for value in sorted(self._known, key=len, reverse=True):
            if value in s:
                s = s.replace(value, f"‹{self._known[value]}›")
        for label, pat in PATTERNS:
            s = pat.sub(f"‹{label}›", s)
        s = CARD.sub(lambda m: "‹card›" if _luhn_ok(re.sub(r"\D", "", m.group())) else m.group(), s)
        s = LONG_NUMBER.sub("‹number›", s)
        s = URL_TOKEN.sub(r"\1‹token›", s)
        return s

    def obj(self, o: Any) -> Any:
        """Recursively redact strings in a JSON-like structure."""
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o


def mask_value(value: str, keep: int = 2) -> str:
    """Partial mask for display to a human operator: 10042 -> •••42."""
    v = str(value)
    return "•" * max(len(v) - keep, 1) + v[-keep:] if len(v) > keep else "•" * len(v)
