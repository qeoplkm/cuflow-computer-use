"""Human-in-the-loop: who controls the live session, and how control moves.

Control-transfer model
----------------------
A run's live session has exactly one controller at a time, tracked by ``SessionControl``:

    AUTOMATION ──escalate()──▶ AWAITING_HUMAN ──claim(op)──▶ HUMAN ──release(decision)──▶ AUTOMATION
                                     │
                                     └──────── timeout / no operator ──────▶ (run fails with the original error)

* Automation calls ``ensure_automation()`` before every action; while a human holds the
  session it *cannot* act (the call raises). That is the seam that makes "pause, cede
  control, resume" safe, rather than two drivers racing on one page.
* An ``InterventionRequest`` carries what an operator needs: goal/capability, step and
  intent, why we stopped, current locations, on-screen error text, a (masked) screenshot,
  the proposed action for approval requests, and the allowed resolutions.
* The operator gets a ``LiveSession`` on the *same* browser context (cookies, frames, the
  half-filled form), never a fresh one. Every input event the human produces while holding
  the session is captured through the same perception code the model uses, so human steps
  are logged as structured, reviewable steps (and can be compiled into the artifact).
* ``release`` records the decision: ``resume`` (I fixed it, continue), ``approve`` /
  ``deny`` (for irreversible steps), ``completed`` (I finished the task by hand),
  ``abort``.

Operator surfaces implemented here: ``TerminalOperator`` (a person at the keyboard drives
the headed browser window) and ``ScriptedOperator`` (a simulated human for reproducible
evidence and tests). A remote console would be another ``Operator`` on the same seam.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol

from .evidence import RunRecorder, now_iso
from .redaction import Redactor

RESOLUTIONS = ("resume", "approve", "deny", "completed", "abort")


class ControlState(str, Enum):
    AUTOMATION = "automation"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN = "human"


ALLOWED = {
    (ControlState.AUTOMATION, ControlState.AWAITING_HUMAN),
    (ControlState.AWAITING_HUMAN, ControlState.HUMAN),
    (ControlState.AWAITING_HUMAN, ControlState.AUTOMATION),  # declined / timed out without claim
    (ControlState.HUMAN, ControlState.AUTOMATION),
}


class ControlError(RuntimeError):
    pass


@dataclass
class InterventionRequest:
    id: str
    run_id: str
    kind: str  # stuck | needs_approval | unrecognized_state | agent_requested | target_not_found
    reason: str
    subject: str  # capability id@version or the discovery goal (redacted)
    step_id: str | None
    step_intent: str | None
    locations: dict[str, str]
    alerts: list[str]
    screenshot: str | None
    proposed_action: dict[str, Any] | None = None
    allowed: tuple[str, ...] = RESOLUTIONS
    created_at: str = field(default_factory=now_iso)


@dataclass
class Resolution:
    decision: str
    operator: str
    note: str | None = None
    human_events: list[dict[str, Any]] = field(default_factory=list)
    waited_ms: int = 0


class Operator(Protocol):
    name: str

    def handle(self, req: InterventionRequest, live: "LiveSession") -> Resolution: ...


class SessionControl:
    def __init__(self, recorder: RunRecorder) -> None:
        self.recorder = recorder
        self.state = ControlState.AUTOMATION
        self.holder = "automation"
        self._n = 0

    def _transition(self, to: ControlState, holder: str, **info: Any) -> None:
        if (self.state, to) not in ALLOWED:
            raise ControlError(f"illegal control transition {self.state.value} -> {to.value}")
        self.recorder.event("control_transfer", frm=self.state.value, to=to.value, holder=holder, **info)
        self.state, self.holder = to, holder

    def ensure_automation(self) -> None:
        if self.state is not ControlState.AUTOMATION:
            raise ControlError(f"automation may not act: session is held by {self.holder} ({self.state.value})")

    def next_id(self) -> str:
        self._n += 1
        return f"int-{self._n:02d}"

    def escalate(self, req: InterventionRequest, operator: Operator, surface: Any, redactor: Redactor) -> Resolution:
        self.recorder.write_json(f"interventions/{req.id}.json", asdict(req))
        self.recorder.event("intervention_requested", id=req.id, kind=req.kind, reason=req.reason, step=req.step_id)
        self._transition(ControlState.AWAITING_HUMAN, "nobody", intervention=req.id)
        live = LiveSession(self, surface, req)
        t0 = time.time()
        try:
            res = operator.handle(req, live)
        finally:
            if self.state is ControlState.HUMAN:  # operator crashed while holding control
                live.release("abort", "operator surface failed while holding control")
            elif self.state is ControlState.AWAITING_HUMAN:
                self._transition(ControlState.AUTOMATION, "automation", intervention=req.id, note="no operator claimed")
        res.waited_ms = int((time.time() - t0) * 1000)
        res.human_events = live.captured
        self.recorder.event(
            "intervention_resolved",
            id=req.id,
            decision=res.decision,
            operator=res.operator,
            note=res.note,
            human_actions=[summarize_human_event(e, redactor) for e in live.captured],
        )
        return res


class LiveSession:
    """What an operator gets: the same live session, gated by the control state."""

    def __init__(self, control: SessionControl, surface: Any, req: InterventionRequest) -> None:
        self.control = control
        self.surface = surface
        self.req = req
        self.captured: list[dict[str, Any]] = []
        self.operator: str | None = None

    @property
    def page(self) -> Any:
        return self.surface.page

    def claim(self, operator: str) -> None:
        self.surface.drain_human_events()  # ignore anything before the human held control
        self.control._transition(ControlState.HUMAN, operator, intervention=self.req.id)
        self.operator = operator

    def pump(self, ms: int = 200) -> None:
        self.surface.pump(ms)
        if self.control.state is ControlState.HUMAN:
            for ev in self.surface.drain_human_events():
                ev["operator"] = self.operator
                self.captured.append(ev)
                self.control.recorder.event(
                    "human_action", operator=self.operator, **summarize_human_event(ev, self.control.recorder.redactor)
                )

    def release(self, decision: str, note: str | None = None) -> Resolution:
        if decision not in self.req.allowed:
            raise ControlError(f"resolution {decision!r} not allowed here; allowed: {self.req.allowed}")
        self.pump(250)  # flush trailing input events
        self.control._transition(
            ControlState.AUTOMATION, "automation", intervention=self.req.id, decision=decision, by=self.operator
        )
        return Resolution(decision=decision, operator=self.operator or "unknown", note=note)


def summarize_human_event(ev: dict[str, Any], redactor: Redactor) -> dict[str, Any]:
    info = ev.get("info") or {}
    out = {
        "type": ev.get("type"),
        "target": f'{info.get("role")} "{info.get("name")}"',
        "frame": ev.get("frame") or "top",
    }
    if ev.get("type") == "change" and info.get("value") is not None:
        redactor.add(str(info["value"]), "operator_input")
        out["value"] = redactor.text(str(info.get("value")))
    return out


# ======================================================================================
# Operator surfaces
# ======================================================================================


class NoOperator:
    """Unattended: nobody to escalate to. The run fails with the original error."""

    name = "none"

    def handle(self, req: InterventionRequest, live: LiveSession) -> Resolution:
        return Resolution(decision="abort", operator=self.name, note="no operator attached (unattended run)")


class TerminalOperator:
    """A person at this terminal drives the headed browser window (the same live session)."""

    name = "terminal"

    def __init__(self, operator_id: str = "operator@terminal", timeout_s: int = 900) -> None:
        self.operator_id = operator_id
        self.timeout_s = timeout_s

    def _lines(self) -> "queue.Queue[str]":
        q: "queue.Queue[str]" = queue.Queue()

        def reader() -> None:
            for line in sys.stdin:
                q.put(line.strip())

        threading.Thread(target=reader, daemon=True).start()
        return q

    def handle(self, req: InterventionRequest, live: LiveSession) -> Resolution:
        bar = "=" * 78
        print(f"\n{bar}\nINTERVENTION REQUIRED  [{req.id}]  kind={req.kind}", file=sys.stderr)
        print(f"  subject : {req.subject}\n  step    : {req.step_id} — {req.step_intent}", file=sys.stderr)
        print(f"  reason  : {req.reason}", file=sys.stderr)
        if req.proposed_action:
            print(f"  proposed: {req.proposed_action}", file=sys.stderr)
        for k, v in req.locations.items():
            print(f"  frame {k:<7}: {v}", file=sys.stderr)
        for a in req.alerts:
            print(f"  on screen: {a}", file=sys.stderr)
        print(f"  screenshot: {req.screenshot}\n{bar}", file=sys.stderr)
        print("Type 'claim' to take control of the browser window (or 'abort').", file=sys.stderr, flush=True)
        q = self._lines()
        deadline = time.time() + self.timeout_s
        claimed = False
        while time.time() < deadline:
            live.pump(200)
            try:
                line = q.get_nowait()
            except queue.Empty:
                continue
            cmd, _, note = line.partition(" ")
            if not claimed and cmd == "claim":
                live.claim(self.operator_id)
                claimed = True
                print(
                    f"You control the session. Act in the browser window, then type one of: "
                    f"{' | '.join(req.allowed)} [note]",
                    file=sys.stderr,
                    flush=True,
                )
            elif cmd == "abort" and not claimed:
                return Resolution("abort", self.operator_id, note or None)
            elif claimed and cmd in req.allowed:
                return live.release(cmd, note or None)
            else:
                print(f"? expected {'claim/abort' if not claimed else '/'.join(req.allowed)}", file=sys.stderr)
        if claimed:
            return live.release("abort", "operator timed out")
        return Resolution("abort", self.operator_id, "no operator claimed before timeout")


class ScriptedOperator:
    """A *simulated* human operator, for reproducible evidence and tests.

    It claims the live session, performs scripted UI actions through the real page (so they
    are captured like any human input), then releases with a scripted decision.
    """

    name = "scripted"

    def __init__(self, operator_id: str, actions: list[dict[str, Any]], decision: str, note: str | None = None) -> None:
        self.operator_id = f"simulated:{operator_id}"
        self.actions = actions
        self.decision = decision
        self.note = note

    def handle(self, req: InterventionRequest, live: LiveSession) -> Resolution:
        live.pump(300)  # "reading the request"
        live.claim(self.operator_id)
        if self.decision not in req.allowed:
            return live.release("abort", "Configured simulated resolution is not allowed for this intervention.")
        for a in self.actions:
            frame = next((f for f in live.page.frames if f.name == a.get("frame")), live.page.main_frame)
            loc = frame.locator(a["css"]) if "css" in a else frame.get_by_role(a["role"], name=a["name"], exact=True)
            if a["action"] == "click":
                loc.first.click()
            elif a["action"] == "fill":
                loc.first.fill(a["value"])
            elif a["action"] == "select":
                loc.first.select_option(label=a["value"])
            live.pump(400)
        return live.release(self.decision, self.note)
