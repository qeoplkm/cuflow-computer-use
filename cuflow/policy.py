"""Allowlist enforcement and action risk classification.

The policy is enforced at three independent points, so a confused model, a bad artifact
or a surprising redirect cannot get around it:

1. **Before acting** (`PolicyEngine.check`) — action type allowed, the target's frame is on
   an allowed origin/path, navigation targets are allowed, risk gate for irreversible
   actions.
2. **At the network layer** (`allows_url`, wired into the browser's request routing) —
   document requests to anything outside the allowlist are aborted, whoever initiated them.
3. **After acting** — the surface's locations are re-checked; leaving the allowlist stops
   the run with POLICY_VIOLATION.

Risk classification is conservative: the strictest of (a) the action type's base risk,
(b) keyword rules on the control's visible name, (c) for Enter key presses, the name of
the form's submit button (pressing Enter *is* clicking submit), and (d) the risk the
model or artifact declares. Irreversible actions are never executed without an explicit
approval — in discovery that approval comes from a human via the escalation channel; in
replay it must be passed by the caller.
"""

from __future__ import annotations

import fnmatch
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .schema import RISK_ORDER, Policy, Risk

BASE_RISK = {
    "navigate": Risk.safe,
    "extract": Risk.safe,
    "click": Risk.safe,
    "fill": Risk.safe,  # local, uncommitted form state; the commit is the submit click
    "select": Risk.safe,
    "press": Risk.safe,  # but Enter inherits the risk of the form's submit button (below)
}


def max_risk(*risks: Risk | None) -> Risk:
    present = [r for r in risks if r is not None]
    return max(present, key=lambda r: RISK_ORDER[r]) if present else Risk.safe


@dataclass
class Decision:
    allowed: bool
    risk: Risk
    reason: str = ""
    needs_approval: bool = False


class PolicyEngine:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._kw = [re.compile(rf"\b{re.escape(k)}\b", re.I) for k in policy.irreversible_keywords]
        self._rev = [re.compile(rf"\b{re.escape(k)}\b", re.I) for k in policy.reversible_keywords]

    # ---- locations -------------------------------------------------------------------

    def allows_url(self, url: str) -> tuple[bool, str]:
        if url in ("about:blank", "") or url.startswith("data:"):
            return True, ""
        u = urllib.parse.urlsplit(url)
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in self.policy.allowed_origins:
            return False, f"origin {origin} not in allowlist"
        path = u.path or "/"
        if any(fnmatch.fnmatchcase(path, p) for p in self.policy.denied_paths):
            return False, f"path {path} is explicitly denied"
        if not any(fnmatch.fnmatchcase(path, p) for p in self.policy.allowed_paths):
            return False, f"path {path} not in allowlist"
        return True, ""

    # ---- risk ------------------------------------------------------------------------

    def classify(self, action_type: str, element: dict[str, Any] | None = None, key: str | None = None) -> Risk:
        risk = BASE_RISK.get(action_type, Risk.irreversible)  # unknown action types: assume worst
        names: list[str] = []
        if action_type == "press":
            normalized = (key or "").lower()
            navigation = {"tab", "shift+tab", "escape", "arrowleft", "arrowright", "arrowup", "arrowdown", "home", "end", "backspace"}
            # Unknown shortcuts can invoke app commands. Do not assume they are navigation.
            if normalized not in navigation | {"enter", "space", " "}:
                risk = Risk.irreversible
        if element:
            if action_type == "click":
                names.append(element.get("name") or "")
                names.append((element.get("attrs") or {}).get("value") or "")
            if action_type == "press" and (key or "").lower() in {"enter", "space", " "}:
                names.append(element.get("name") or "")
                names.append((element.get("attrs") or {}).get("value") or "")
                names.append(element.get("form_submit_name") or "")
        if any(k.search(n) for n in names if n for k in self._kw):
            risk = Risk.irreversible
        elif any(k.search(n) for n in names if n for k in self._rev):
            risk = max_risk(risk, Risk.reversible)
        return risk

    # ---- the gate --------------------------------------------------------------------

    def check(
        self,
        action_type: str,
        *,
        element: dict[str, Any] | None = None,
        frame_url: str | None = None,
        nav_url: str | None = None,
        key: str | None = None,
        declared_risk: Risk | None = None,
        approved: bool = False,
    ) -> Decision:
        if action_type not in self.policy.allowed_actions:
            return Decision(False, Risk.irreversible, f"action type '{action_type}' is not allowed")
        if action_type == "press" and element is None:
            return Decision(False, Risk.irreversible, "keyboard actions require an explicit target; implicit focus is not trusted")
        for url in (frame_url, nav_url):
            if url is not None:
                ok, why = self.allows_url(url)
                if not ok:
                    return Decision(False, Risk.irreversible, why)
        risk = max_risk(self.classify(action_type, element, key), declared_risk)
        if risk is Risk.irreversible:
            if self.policy.on_irreversible == "block":
                return Decision(False, risk, "irreversible actions are blocked by policy")
            if not approved:
                return Decision(False, risk, "irreversible action requires approval", needs_approval=True)
        return Decision(True, risk)
