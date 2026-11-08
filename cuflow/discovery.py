"""LLM-driven discovery: observe -> decide -> act until the goal is met, recording a trace.

Design choices (see REPORT.md):
* The session is established by the app profile's deterministic login. The model never
  sees or types credentials.
* Known interstitials (from the app profile) are handled by the same recovery code replay
  uses, *before* the model looks at the screen, so they never end up as recorded steps
  (replay would break whenever they are absent).
* Every action passes the policy gate. Irreversible actions are escalated to a human for
  approval; the model cannot approve itself.
* The model must state intent and risk for every action, declare which typed values are
  parameters, and declare outputs when it reads data. The trace, not the transcript, is
  compiled into the artifact.
* Stuck detection: unchanged screen after acting, repeated identical calls, or repeated
  invalid calls -> intervention request to a human (or a clean failure if none attached).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from .compiler import DoneInfo, ParamDecl, RecordedStep, compile_capability
from .config import RUNS, capability_path, dump_yaml, load_profile, load_tenant
from .control import Operator
from .evidence import RunRecorder, now_iso
from .llm import LLMClient, ToolSpec
from .policy import PolicyEngine
from .redaction import Redactor
from .replay import Replayer, ReplayOptions, _Restart, _Stop, parse_output
from .results import ErrorCode, ErrorDetail, RunResult
from .schema import (
    AppProfile,
    AppRef,
    Capability,
    OutcomeKind,
    OutputSpec,
    Provenance,
    Risk,
    Sensitivity,
    TenantConfig,
    TextCondition,
)
from .surface.web import WebSurface
from .targets import build_target

SYSTEM_PROMPT = """\
You operate a legacy back-office banking application through a computer-use interface, on behalf \
of a bank operations team. Accomplish the GOAL by interacting with the UI. Your actions are being \
recorded and compiled into a reusable, deterministic automation, so act like a careful, \
methodical human operator.

Each turn you get the goal, what you did so far, and the CURRENT SCREEN: every frame of the \
window rendered as text, top to bottom. Interactive controls and table cells carry references \
like [work:12]. Only use references that appear on the CURRENT screen. A screenshot may be attached.

Rules:
- Call exactly one tool per turn.
- Use the application's own menus, forms, buttons and links. Never invent data that is neither \
on screen nor in the goal.
- When you type or select a value that comes from the goal and would change from one run to the \
next (an id, a name, an amount), declare it as a parameter with a short snake_case name.
- When the goal asks you to read data, call `extract` on the exact cell/field holding it, with a \
snake_case output name and a type. For a table cell, pass row_key: the text that identifies the \
row (e.g. the account description), never the value itself.
- Classify every action's risk: "safe" (navigate, open, read, type into a form that is not saved yet), \
"reversible" (saves something that can be undone), "irreversible" (commits something: submit, post, \
confirm, transfer, delete). Irreversible actions \
need a human's approval; the system will ask for it, do not try to work around it.
- Known system notices are dismissed automatically; you will not see most of them.
- If you are stuck, see an error you cannot resolve, or a decision needs a human, call `escalate`.
- When the goal is achieved and all requested data is extracted, call `done`. success_text must be \
short, stable text visible on the final screen that proves you are on the right screen (a heading \
or label, never a data value).
"""

_REF = {"type": "string", "description": "element reference from the CURRENT screen, e.g. work:12"}
_INTENT = {"type": "string", "description": "why you take this action (one short sentence)"}
_RISK = {"type": "string", "enum": ["safe", "reversible", "irreversible"]}

TOOLS = [
    ToolSpec(
        "click",
        "Click a link, button or other control.",
        {
            "type": "object",
            "properties": {"ref": _REF, "intent": _INTENT, "risk": _RISK},
            "required": ["ref", "intent", "risk"],
        },
    ),
    ToolSpec(
        "type_text",
        "Replace the content of a text box with the given text.",
        {
            "type": "object",
            "properties": {
                "ref": _REF,
                "text": {"type": "string"},
                "parameter": {"type": "string", "description": "snake_case parameter name if the value comes from the goal"},
                "parameter_description": {"type": "string"},
                "intent": _INTENT,
                "risk": _RISK,
            },
            "required": ["ref", "text", "intent", "risk"],
        },
    ),
    ToolSpec(
        "select_option",
        "Choose an option (by its visible label) in a drop-down.",
        {
            "type": "object",
            "properties": {
                "ref": _REF,
                "option": {"type": "string"},
                "parameter": {"type": "string"},
                "parameter_description": {"type": "string"},
                "intent": _INTENT,
                "risk": _RISK,
            },
            "required": ["ref", "option", "intent", "risk"],
        },
    ),
    ToolSpec(
        "press_key",
        "Press a keyboard key (e.g. Enter, Tab), optionally on a specific control.",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}, "ref": _REF, "intent": _INTENT, "risk": _RISK},
            "required": ["key", "ref", "intent", "risk"],
        },
    ),
    ToolSpec(
        "extract",
        "Read a value the goal asks for from a cell or field, and declare it as an output.",
        {
            "type": "object",
            "properties": {
                "ref": _REF,
                "output_name": {"type": "string", "description": "snake_case"},
                "output_type": {"type": "string", "enum": ["string", "money", "decimal", "integer", "date"]},
                "description": {"type": "string"},
                "row_key": {"type": "string", "description": "text identifying the table row (not the value)"},
                "sensitivity": {"type": "string", "enum": ["public", "internal", "pii", "financial"]},
                "intent": _INTENT,
            },
            "required": ["ref", "output_name", "output_type", "description", "intent"],
        },
    ),
    ToolSpec(
        "done",
        "The goal is achieved. Name and describe the reusable capability you just performed.",
        {
            "type": "object",
            "properties": {
                "success_text": {"type": "string", "description": "stable visible text proving the final screen"},
                "capability_name": {"type": "string", "description": "dotted snake_case, e.g. member.get_savings_balance"},
                "capability_description": {"type": "string", "description": "what it does, for another AI agent"},
                "summary": {"type": "string"},
            },
            "required": ["success_text", "capability_name", "capability_description", "summary"],
        },
    ),
    ToolSpec(
        "escalate",
        "Ask a human operator to take over the live session (stuck, unknown error, or a decision needed).",
        {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "kind": {"type": "string", "enum": ["stuck", "unknown_error", "needs_decision"]},
            },
            "required": ["reason", "kind"],
        },
    ),
]

ACTION_OF = {"click": "click", "type_text": "fill", "select_option": "select", "press_key": "press"}


@dataclass
class DiscoveryOptions:
    max_steps: int = 25
    max_seconds: int = 300
    screenshot: bool = True
    operator: Operator | None = None
    save_to_catalog: bool = True


@dataclass
class DiscoveryOutcome:
    result: RunResult
    capability: Capability | None = None
    artifact_path: Path | None = None
    trace: list[RecordedStep] = field(default_factory=list)


class _End(Exception):
    def __init__(self, status: str, code: ErrorCode | None = None, message: str = "") -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Discovery:
    def __init__(
        self,
        goal: str,
        tenant: TenantConfig,
        profile: AppProfile,
        surface: WebSurface,
        recorder: RunRecorder,
        redactor: Redactor,
        llm: LLMClient,
        opts: DiscoveryOptions | None = None,
    ) -> None:
        self.goal, self.tenant, self.profile = goal, tenant, profile
        self.surface, self.rec, self.redactor, self.llm = surface, recorder, redactor, llm
        self.opts = opts or DiscoveryOptions()
        stub = Capability(
            id=f"{profile.product}.discovery",
            version="0.0.0",
            title="discovery session",
            description=goal,
            app=AppRef(product=profile.product, versions=profile.versions),
            steps=[],
            success=TextCondition(pattern="-"),
            provenance=Provenance(method="llm_discovery", recorded_at=now_iso()),
        )
        # The replay runtime provides login, known-state recovery and escalation plumbing.
        self.rt = Replayer(stub, profile, tenant, surface, recorder, redactor, ReplayOptions(operator=self.opts.operator, max_escalations=3))
        self.policy: PolicyEngine = self.rt.policy
        self.trace: list[RecordedStep] = []
        self.params: dict[str, ParamDecl] = {}
        self.outputs: dict[str, Any] = {}
        self.history: list[str] = []
        self.feedback: str | None = None
        self.done: DoneInfo | None = None
        self._bad = 0
        self._no_progress = 0
        self._last_calls: list[str] = []
        self._screen_history: list[str] = []
        self._extracted_values: list[str] = []
        # Goal text is never persisted verbatim. Register numeric task data early
        # as well, before the first model reply can repeat a member/account id.
        for value in re.findall(r"\b[0-9][0-9,]*(?:\.[0-9]+)?\b", goal):
            self.redactor.add(value, "goal_value")

    # ------------------------------------------------------------------ main loop

    def run(self) -> DiscoveryOutcome:
        t0 = time.time()
        self.rec.event("discovery_started", goal="[omitted: see reviewed goal_template]", model=self.llm.model, tenant=self.tenant.tenant)
        status, error = "succeeded", None
        try:
            self.rt._login()
            self._loop(t0)
        except _End as e:
            status = e.status
            if e.code:
                error = ErrorDetail(code=e.code, message=self.redactor.text(e.message))
        except _Stop as e:
            status = e.kw["status"]
            error = e.kw.get("error")
        except PlaywrightError as e:
            status = "failed"
            error = ErrorDetail(code=ErrorCode.SURFACE_ERROR, message=self.redactor.text(str(e).splitlines()[0]))
        cap = path = None
        if status == "succeeded" and self.done:
            cap = compile_capability(
                trace=self.trace,
                params=self.params,
                done=self.done,
                goal=self.goal,
                product=self.profile.product,
                product_versions=self.profile.versions,
                tenant=self.tenant.tenant,
                app_version=self.tenant.product_version,
                model=self.llm.model,
                run_id=self.rec.run_id,
                sensitive_values=self._extracted_values,
            )
            text = dump_yaml(cap)
            leaked = [v for v in self._extracted_values + [p.example for p in self.params.values()] if v and v in text]
            if leaked:  # defense in depth: an artifact must never carry recorded data values
                raise RuntimeError("compiled artifact contains a recorded data value; refusing to save")
            self.rec.write_text("artifact.yaml", text, redact=False)
            if self.opts.save_to_catalog:
                path = capability_path(cap)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                self.rec.event("artifact_saved", path=str(path), capability=cap.id, version=cap.version)
        self.rec.write_json("trace.json", [_trace_json(t) for t in self.trace])
        result = RunResult(
            run_id=self.rec.run_id,
            mode="discovery",
            capability=cap.id if cap else None,
            capability_version=cap.version if cap else None,
            tenant=self.tenant.tenant,
            status=status,  # type: ignore[arg-type]
            outputs=dict(self.outputs),
            error=error,
            recoveries=self.rt.recoveries,
            interventions=self.rt.interventions,
            duration_ms=int((time.time() - t0) * 1000),
            evidence_dir=str(self.rec.dir),
        )
        self.rec.write_json("result.json", result)
        self.rec.event("discovery_finished", status=status, capability=result.capability, steps=len(self.trace))
        return DiscoveryOutcome(result=result, capability=cap, artifact_path=path, trace=self.trace)

    def _loop(self, t0: float) -> None:
        prev_sig = None
        for n in range(1, self.opts.max_steps + 1):
            if time.time() - t0 > self.opts.max_seconds:
                raise _End("failed", ErrorCode.TIMEOUT, f"discovery exceeded {self.opts.max_seconds}s")
            self._handle_known_states()
            obs = self.surface.observe(screenshot=self.opts.screenshot)
            self.redactor.add_many(self.surface.sensitive_texts(), "pii")
            screen = obs.render()
            sig = hashlib.sha1(screen.encode()).hexdigest()
            self._screen_history = (self._screen_history + [sig])[-9:]
            if prev_sig == sig and self._last_calls and self._last_calls[-1].split("(")[0] in ("click", "press_key"):
                self._no_progress += 1
            elif prev_sig != sig:
                self._no_progress = 0
            prev_sig = sig
            if self._is_stuck():
                self._stuck("no progress: repeated or ineffective actions")
                continue
            prompt = self._prompt(screen)
            shot = self.rec.shot(self.surface, f"step{n:02d}")
            reply = self._decide(prompt, obs.screenshot)
            if reply.call:
                # Register user-entered values before logging the model's call,
                # even if the model forgot to declare a parameter.
                for key in ("text", "option"):
                    if key in reply.call.args:
                        self.redactor.add(str(reply.call.args[key]), "input")
            evidence_prompt = prompt.replace(f"GOAL: {self.goal}", "GOAL: [omitted: see reviewed goal_template]", 1)
            self.rec.write_json(
                f"llm/{n:03d}.json",
                {"model": reply.model, "prompt": evidence_prompt, "tool": reply.call and reply.call.name,
                 "args": reply.call and reply.call.args, "usage": reply.usage, "screenshot": shot},
            )
            self.rec.event(
                "llm_decision",
                n=n,
                tool=reply.call.name if reply.call else None,
                args=reply.call.args if reply.call else None,
                ms=reply.latency_ms,
                usage=reply.usage,
            )
            if reply.call is None:
                self._invalid("You must call exactly one tool.")
                continue
            call = reply.call
            self._last_calls.append(f"{call.name}({json.dumps(call.args, sort_keys=True)})")
            if call.name in ACTION_OF:
                self._act(call.name, call.args)
            elif call.name == "extract":
                self._extract(call.args)
            elif call.name == "done":
                if self._done(call.args):
                    return
            elif call.name == "escalate":
                self._agent_escalation(call.args)
            else:
                self._invalid(f"Unknown tool {call.name}.")
        raise _End("failed", ErrorCode.STEP_BUDGET_EXCEEDED, f"goal not reached within {self.opts.max_steps} steps")

    def _decide(self, prompt: str, image: bytes | None) -> Any:
        """One model call. Transient provider errors are retried with backoff; anything
        else (quota, auth) ends the run cleanly as MODEL_ERROR with the session intact."""
        delay = 2.0
        for attempt in range(1, 4):
            try:
                return self.llm.decide(SYSTEM_PROMPT, prompt, image, TOOLS)
            except Exception as e:  # provider SDKs raise their own hierarchies
                msg = str(e).splitlines()[0][:300]
                status = getattr(e, "code", None) or getattr(e, "status_code", None)
                transient = status in (429, 500, 502, 503, 504) or "timeout" in msg.lower()
                self.rec.event("llm_error", attempt=attempt, status=status, error=msg, transient=transient)
                if not transient or attempt == 3:
                    raise _End("failed", ErrorCode.MODEL_ERROR, f"model call failed: {msg}") from None
                self.surface.pump(int(delay * 1000))
                delay *= 2

    # ------------------------------------------------------------------ prompt

    def _prompt(self, screen: str) -> str:
        lines = [f"GOAL: {self.goal}", ""]
        if self.params:
            lines.append("PARAMETERS declared: " + ", ".join(f"{p.name}={p.example!r}" for p in self.params.values()))
        if self.outputs:
            lines.append("OUTPUTS extracted: " + ", ".join(f"{k}={v!r}" for k, v in self.outputs.items()))
        lines.append("PREVIOUS ACTIONS:" if self.history else "PREVIOUS ACTIONS: none yet")
        lines += [f"{i}. {h}" for i, h in enumerate(self.history[-15:], 1)]
        if self.feedback:
            lines += ["", f"FEEDBACK ON YOUR LAST CALL: {self.feedback}"]
            self.feedback = None
        lines += ["", "CURRENT SCREEN:", screen]
        return "\n".join(lines)

    # ------------------------------------------------------------------ handlers

    def _handle_known_states(self) -> None:
        deadline = time.time() + 15
        while not self.surface.settled() and time.time() < deadline:
            self.surface.pump(150)
        o = self.rt._match_outcome(None)
        if o is not None and o.kind is OutcomeKind.recoverable:
            try:
                self.rt._handle_outcome(o, None)
            except _Restart:
                self.rt._login()
            self.history.append(f"(system) handled known state '{o.id}' automatically")

    def _wait_settled(self, timeout: float = 15) -> None:
        self.surface.pump(200)
        deadline = time.time() + timeout
        while not self.surface.settled() and time.time() < deadline:
            self.surface.pump(150)

    def _examples(self) -> dict[str, str]:
        return {p.example: p.name for p in self.params.values()}

    def _act(self, tool: str, a: dict[str, Any]) -> None:
        action = ACTION_OF[tool]
        intent = str(a.get("intent") or "")
        ref = a.get("ref")
        described = self.surface.describe(ref) if ref else None
        if ref and described is None:
            return self._invalid(f"Reference {ref} is not on the current screen.")
        if described is None:
            return self._invalid(f"{tool} needs a ref.")
        frame_key = ref.split(":", 1)[0] if ref else "top"
        frame, handle, info, cands = described if described else (None, None, None, [])
        try:
            declared = Risk(a.get("risk", "safe"))
        except ValueError:
            declared = Risk.irreversible
        d = self.policy.check(action, element=info, frame_url=frame.url if frame else None, key=a.get("key"), declared_risk=declared)
        self.rec.event("policy_check", action=action, target=info and info.get("name"), allowed=d.allowed, risk=d.risk.value, reason=d.reason or None)
        human_did_it = False
        if not d.allowed:
            if not d.needs_approval:
                return self._invalid(f"Blocked by policy: {d.reason}. Choose another action.")
            res = self.rt._escalate(
                "needs_approval",
                f"agent wants to {action} <{info and info.get('name')}> ({intent})",
                None,
                proposed={"action": action, "target": info and info.get("name"), "intent": intent},
                allowed=("approve", "deny", "completed", "abort"),
            )
            if res is None:
                raise _End("blocked", ErrorCode.APPROVAL_REQUIRED, f"irreversible action needs approval and no operator is attached: {action} <{info and info.get('name')}>")
            if res.decision == "abort":
                raise _End("failed", ErrorCode.APPROVAL_REQUIRED, "operator aborted the run")
            if res.decision == "deny":
                self.history.append(f"{tool} <{info and info.get('name')}> — DENIED by operator ({res.note or 'no note'})")
                self.feedback = "A human operator denied that irreversible action. Do not retry it."
                return
            if res.decision == "completed":
                human_did_it = True
                self._record_human(res.human_events)
                self.history.append(f"human operator performed: {info and info.get('name')} ({res.note or ''})")
            d.risk = Risk.irreversible

        value = None
        text = None
        if action in ("fill", "select"):
            text = str(a.get("text") if action == "fill" else a.get("option") or "")
            pname = a.get("parameter")
            if pname:
                pname = str(pname).strip().lower().replace(" ", "_")
                self.params[pname] = ParamDecl(pname, text, str(a.get("parameter_description") or pname))
                self.redactor.add(text, pname)
                value = {"param": pname}
            elif text in self._examples():
                value = {"param": self._examples()[text]}
            else:
                value = {"literal": text}
        target = build_target(self.surface, frame_key, info, cands, self._examples()) if info else None
        before = self.surface.locations()
        if not human_did_it:
            self.rt.control.ensure_automation()
            try:
                if action == "click":
                    handle.click(timeout=5000)
                elif action == "fill":
                    handle.fill(text, timeout=5000)
                elif action == "select":
                    try:
                        handle.select_option(label=text, timeout=5000)
                    except PlaywrightError:
                        handle.select_option(value=text, timeout=5000)
                elif action == "press":
                    (handle.press if handle else self.surface.page.keyboard.press)(str(a.get("key") or "Enter"))
            except PlaywrightError as e:
                return self._invalid(f"Action failed: {str(e).splitlines()[0]}")
        self._wait_settled()
        after = self.surface.locations()
        if not human_did_it:
            self.trace.append(
                RecordedStep(
                    actor="agent",
                    action=action,
                    intent=intent,
                    frame=frame_key,
                    target=target,
                    risk=d.risk,
                    before=before,
                    after=after,
                    value=value,
                    key=a.get("key"),
                )
            )
        self._bad = 0
        moved = {k: v.split("/", 3)[-1] for k, v in after.items() if before.get(k) != v}
        what = f'{tool} {target.description if target else a.get("key")}' + (f" = {text!r}" if text is not None else "")
        self.history.append(f"{what} — ok" + (f"; navigated {moved}" if moved else "; screen updated in place"))

    def _extract(self, a: dict[str, Any]) -> None:
        ref = a.get("ref")
        described = self.surface.describe(ref) if ref else None
        if described is None:
            return self._invalid(f"Reference {ref} is not on the current screen.")
        frame, handle, info, cands = described
        name = str(a.get("output_name") or "value").strip().lower().replace(" ", "_")
        otype = a.get("output_type") or "string"
        sens = a.get("sensitivity") or ("financial" if otype in ("money", "decimal") else "pii")
        raw = (handle.input_value() if info.get("tag") in ("input", "select", "textarea") else handle.inner_text()).strip()
        try:
            value = parse_output(raw, OutputSpec(name=name, type=otype, description=""))
        except Exception as e:
            return self._invalid(f"'{raw}' is not a valid {otype}: {e}")
        self.redactor.add(raw, name)
        self.redactor.add(str(value), name)
        self._extracted_values += [raw, str(value)]
        frame_key = ref.split(":", 1)[0]
        target = build_target(self.surface, frame_key, info, cands, self._examples(), row_hint=a.get("row_key"), for_extract=True)
        loc = self.surface.locations()
        self.trace.append(
            RecordedStep(
                actor="agent",
                action="extract",
                intent=str(a.get("intent") or f"read {name}"),
                frame=frame_key,
                target=target,
                risk=Risk.safe,
                before=loc,
                after=loc,
                output={"name": name, "type": otype, "description": str(a.get("description") or ""), "sensitivity": Sensitivity(sens)},
            )
        )
        self.outputs[name] = value
        self._bad = 0
        self.rec.event("extracted", output=name, value_type=otype, value=str(value), target=target.description)
        self.history.append(f"extract {name} from {target.description} — got {value!r}")

    def _done(self, a: dict[str, Any]) -> bool:
        st = str(a.get("success_text") or "").strip()
        if not st or not self.surface.text_present(st):
            self._invalid(f"success_text {st!r} is not visible on the current screen.")
            return False
        if any(v and v in st for v in self._extracted_values):
            self._invalid("success_text must not contain data values; use a heading or label.")
            return False
        if not self.trace:
            self._invalid("Nothing was done yet.")
            return False
        self.done = DoneInfo(
            capability_name=str(a.get("capability_name") or "unnamed"),
            description=str(a.get("capability_description") or self.goal),
            success_text=st,
            summary=str(a.get("summary") or ""),
        )
        self.rec.event("goal_reached", summary=self.done.summary, success_text=st, capability=self.done.capability_name)
        return True

    def _agent_escalation(self, a: dict[str, Any]) -> None:
        reason = str(a.get("reason") or "agent requested help")
        self._human_takeover(str(a.get("kind") or "agent_requested"), reason)

    def _stuck(self, why: str) -> None:
        self._human_takeover("stuck", why)
        self._no_progress = self._bad = 0
        self._last_calls.clear()
        self._screen_history.clear()

    def _human_takeover(self, kind: str, reason: str) -> None:
        res = self.rt._escalate(kind, reason, None, allowed=("resume", "completed", "abort"))
        if res is None:
            raise _End("failed", ErrorCode.AGENT_STUCK, f"{kind}: {reason} (no operator attached)")
        if res.decision == "abort":
            raise _End("failed", ErrorCode.AGENT_STUCK, f"operator aborted: {res.note or reason}")
        self._record_human(res.human_events)
        acts = ", ".join(f"{e.get('type')} {(e.get('info') or {}).get('name')!r}" for e in res.human_events) or "nothing"
        self.history.append(f"HUMAN OPERATOR took over ({kind}) and did: {acts}. Note: {res.note or '-'}")
        self.feedback = "A human operator just intervened (see last history entry). Re-read the screen and continue."

    def _record_human(self, events: list[dict[str, Any]]) -> None:
        for ev in events:
            info, locs = ev.get("info") or {}, ev.get("locators") or []
            role = info.get("role")
            if ev.get("type") == "click" and info.get("control") and role not in ("textbox", "combobox"):
                action, value = "click", None
            elif ev.get("type") == "change" and role in ("textbox", "combobox"):
                action = "select" if info.get("tag") == "select" else "fill"
                v = str(info.get("value") or "")
                if v not in self._examples():
                    # Never persist what a human typed: it becomes a parameter for review.
                    pname = f"operator_input_{len(self.params) + 1}"
                    self.params[pname] = ParamDecl(pname, v, f"value an operator typed into {info.get('name')!r}; review")
                    self.redactor.add(v, pname)
                value = {"param": self._examples()[v]}
            else:
                continue
            frame_key = ev.get("frame") or "top"
            target = build_target(self.surface, frame_key, info, locs, self._examples(), verify=False)
            loc = self.surface.locations()
            risk = self.policy.classify(action, info)
            self.trace.append(
                RecordedStep(
                    actor="human",
                    action=action,
                    intent=f"(operator) {action} {info.get('name')}",
                    frame=frame_key,
                    target=target,
                    risk=risk,
                    before=loc,
                    after=loc,
                    value=value,
                )
            )

    def _invalid(self, msg: str) -> None:
        self._bad += 1
        self.feedback = msg
        self.rec.event("agent_feedback", message=msg, consecutive_invalid=self._bad)

    def _is_stuck(self) -> bool:
        repeated = len(self._last_calls) >= 3 and len(set(self._last_calls[-3:])) == 1
        cycle = any(len(self._screen_history) >= width * 3 and
                    self._screen_history[-width * 3:] == self._screen_history[-width:] * 3 for width in (1, 2, 3))
        return self._bad >= 3 or self._no_progress >= 3 or repeated or cycle


def _trace_json(t: RecordedStep) -> dict[str, Any]:
    return {
        "actor": t.actor,
        "action": t.action,
        "intent": t.intent,
        "frame": t.frame,
        "target": t.target.model_dump(mode="json", exclude_none=True) if t.target else None,
        "risk": t.risk.value,
        "value": t.value,
        "key": t.key,
        "output": {k: (v.value if hasattr(v, "value") else v) for k, v in t.output.items()} if t.output else None,
        "before": t.before,
        "after": t.after,
    }


def discover(
    goal: str,
    tenant_name: str,
    llm: LLMClient,
    opts: DiscoveryOptions | None = None,
    *,
    headless: bool = True,
    runs_dir: Path = RUNS,
    console: bool = True,
    tenant: TenantConfig | None = None,
) -> DiscoveryOutcome:
    tenant = tenant or load_tenant(tenant_name)
    profile = load_profile(tenant.product)
    redactor = Redactor()
    recorder = RunRecorder(runs_dir, "discovery", redactor, console=console)
    policy = PolicyEngine(tenant.policy)
    surface = WebSurface(url_allowed=policy.allows_url, headless=headless, on_event=recorder.event).start()
    try:
        return Discovery(goal, tenant, profile, surface, recorder, redactor, llm, opts).run()
    finally:
        surface.close()
        recorder.close()
