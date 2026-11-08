"""Evaluation of the surface-agnostic condition language (postconditions, checkpoints,
outcome detectors)."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .schema import (
    AllCondition,
    AnyCondition,
    Condition,
    DialogCondition,
    ElementCondition,
    LocationCondition,
    NotCondition,
    TextCondition,
)
from .surface.base import Resolved
from .surface.web import substitute


def glob_match(pattern: str, text: str) -> bool:
    """'*' is the only wildcard; everything else (including '?') is literal."""
    rx = "^" + ".*".join(re.escape(p) for p in pattern.split("*")) + "$"
    return re.match(rx, text) is not None


def path_query(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    return (u.path or "/") + (f"?{u.query}" if u.query else "")


def normalize_location(url: str, ignored: list[str]) -> str:
    if not ignored:
        return path_query(url)
    u = urllib.parse.urlsplit(url)
    # Keep semantic query values exact, while allowing an explicit transient token
    # to change or disappear after a recovery. Sorting also removes ordering drift.
    pairs = sorted((k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k not in ignored)
    query = "&".join(f"{k}={v}" for k, v in pairs)
    return (u.path or "/") + (f"?{query}" if query else "")


def evaluate(cond: Condition, surface: Any, params: dict[str, str], since: float = 0.0) -> bool:
    if isinstance(cond, TextCondition):
        return surface.text_present(substitute(cond.pattern, params), cond.regex, cond.frame)
    if isinstance(cond, ElementCondition):
        return isinstance(surface.resolve(cond.target, params), Resolved)
    if isinstance(cond, LocationCondition):
        url = surface.location(cond.frame)
        return url is not None and glob_match(normalize_location(substitute(cond.pattern, params), cond.ignore_query),
                                             normalize_location(url, cond.ignore_query))
    if isinstance(cond, DialogCondition):
        rx = re.compile(cond.pattern, re.I)
        return any(d["ts"] >= since and rx.search(d["message"]) for d in surface.dialogs)
    if isinstance(cond, AllCondition):
        return all(evaluate(c, surface, params, since) for c in cond.of)
    if isinstance(cond, AnyCondition):
        return any(evaluate(c, surface, params, since) for c in cond.of)
    if isinstance(cond, NotCondition):
        return not evaluate(cond.cond, surface, params, since)
    raise TypeError(f"unknown condition {cond!r}")


def describe(cond: Condition | None) -> str:
    if cond is None:
        return "nothing"
    if isinstance(cond, TextCondition):
        where = f" in frame {cond.frame}" if cond.frame else ""
        return f"text {'~' if cond.regex else ''}'{cond.pattern}'{where}"
    if isinstance(cond, ElementCondition):
        return f"element <{cond.target.description}>"
    if isinstance(cond, LocationCondition):
        return f"location {cond.frame or 'top'} ~ {cond.pattern}"
    if isinstance(cond, DialogCondition):
        return f"dialog ~ {cond.pattern}"
    if isinstance(cond, AllCondition):
        return "all(" + "; ".join(describe(c) for c in cond.of) + ")"
    if isinstance(cond, AnyCondition):
        return "any(" + "; ".join(describe(c) for c in cond.of) + ")"
    if isinstance(cond, NotCondition):
        return f"not {describe(cond.cond)}"
    return repr(cond)


def frames_of(cond: Condition | None) -> set[str]:
    """Frames a condition looks at (used to scope 'is the screen settled?')."""
    if cond is None:
        return set()
    if isinstance(cond, (TextCondition, LocationCondition)):
        return {cond.frame or "top"}
    if isinstance(cond, ElementCondition):
        return {cond.target.frame or "top"}
    if isinstance(cond, (AllCondition, AnyCondition)):
        return set().union(*(frames_of(c) for c in cond.of)) if cond.of else set()
    if isinstance(cond, NotCondition):
        return frames_of(cond.cond)
    return set()
