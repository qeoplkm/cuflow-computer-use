"""Deterministic replay: execute a capability with typed inputs and no LLM in the loop.

The core loop never sleeps for a fixed time. After every action it waits for the screen to
*settle* (no document request in flight, frames loaded, DOM quiet) and then classifies the
settled screen against a closed set of possibilities, in this order:

    1. a known outcome (capability-specific, then app-profile-wide)
         business    -> stop, return BusinessOutcome to the caller
         recoverable -> run its recovery (dismiss / restart / re-login), keep going
         failure     -> optional bounded recovery, else stop with APP_ERROR
    2. the step's expected postcondition            -> next step
    3. nothing matches, and the screen stays settled -> UNRECOGNIZED_STATE
       (escalate to a human if one is attached; otherwise fail with evidence)
    4. the screen never settles within the step budget -> TIMEOUT
       (transient: restart once if everything so far was idempotent)

Restarts are only allowed while no non-idempotent step has executed, so recovery can never
double-submit a transaction.
"""

from __future__ import annotations

import datetime as dt
import re
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from .conditions import describe as describe_cond
from .conditions import evaluate, frames_of, path_query
from .config import RUNS, SecretStore, effective_capability, link_targets, load_capability, load_profile, load_tenant, version_satisfies
from .control import NoOperator, Operator, InterventionRequest, Resolution, SessionControl
from .evidence import RunRecorder
from .policy import PolicyEngine
from .redaction import Redactor
from .results import (
    BusinessOutcome,
    DriftEvent,
    ErrorCode,
    ErrorDetail,
    InterventionRecord,
    HumanAction,
    Observed,
    RecoveryEvent,
    RunResult,
    StepRecord,
)
from .schema import (
    AppProfile,
    Capability,
    Condition,
    LiteralValue,
    Outcome,
    OutcomeKind,
    OutputSpec,
    ParamRef,
    Risk,
    SecretRef,
    Sensitivity,
    Step,
    Target,
    TenantConfig,
    Value,
)
from .surface.base import Resolved
from .surface.web import WebSurface, describe_locator, substitute

SENSITIVE = {Sensitivity.pii, Sensitivity.financial, Sensitivity.secret}


@dataclass
class ReplayOptions:
    allow_draft: bool = False  # unattended production replay requires review.status == approved
    approve_irreversible: bool = False  # the caller's explicit approval for irreversible steps
    operator: Operator | None = None  # a human to escalate to; None = unattended
    settle_grace_ms: int = 2000  # settled-but-unclassified for this long => unrecognized state
    max_restarts: int = 2
    max_escalations: int = 2
    poll_ms: int = 150
    slow_step_ms: int = 3000


class _Stop(Exception):
    def __init__(self, **kw: Any) -> None:
        super().__init__(kw.get("status"))
        self.kw = kw


class _Restart(Exception):
    def __init__(self, relogin: bool, reason: str) -> None:
        super().__init__(reason)
        self.relogin = relogin
        self.reason = reason


def parse_output(raw: str, spec: OutputSpec) -> Any:
    s = raw.strip()
    if spec.type == "string":
        return s
    if spec.type in ("money", "decimal"):
        # Validate the entire display value. Stripping arbitrary text would turn
        # an error like 'E1001' into a plausible but incorrect balance.
        neg = s.startswith("(") and s.endswith(")")
        if neg:
            s = s[1:-1].strip()
        elif s.startswith("-") or s.endswith("-"):
            neg = True
            s = s[1:] if s.startswith("-") else s[:-1]
        s = s.removeprefix("$").strip() if spec.type == "money" else s
        fraction = r"(?:\.[0-9]{1,2})?" if spec.type == "money" else r"(?:\.[0-9]+)?"
        if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)" + fraction, s):
            raise ValueError(f"invalid {spec.type} display format")
        d = Decimal(s.replace(",", ""))
        d = -d if neg else d
        return str(d.quantize(Decimal("0.01"))) if spec.type == "money" else str(d)
    if spec.type == "integer":
        if not re.fullmatch(r"-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)", s):
            raise ValueError("invalid integer display format")
        return int(s.replace(",", ""))
    if spec.type == "date":
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                return dt.datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                pass
        raise ValueError(f"unrecognized date {s!r}")
    raise ValueError(f"unknown output type {spec.type}")


class Replayer:
    def __init__(
        self,
        cap: Capability,
        profile: AppProfile,
        tenant: TenantConfig,
        surface: WebSurface,
        recorder: RunRecorder,
        redactor: Redactor,
        opts: ReplayOptions | None = None,
        patched_steps: list[str] | None = None,
    ) -> None:
        self.cap, self.profile, self.tenant = link_targets(cap), profile, tenant
        self.surface, self.rec, self.redactor = surface, recorder, redactor
        self.opts = opts or ReplayOptions()
        self.patched_steps = patched_steps or []
        self.policy = PolicyEngine(tenant.policy)
        self.secrets = SecretStore(tenant)
        self.control = SessionControl(recorder)
        self.base = tenant.base_url.rstrip("/")
        self.params: dict[str, str] = {}
        self.outputs: dict[str, Any] = {}
        self.step_records: list[StepRecord] = []
        self.recoveries: list[RecoveryEvent] = []
        self.drift: list[DriftEvent] = []
        self.interventions: list[InterventionRecord] = []
        self._attempts: dict[str, int] = {}
        self._suppressed: set[str] = set()
        self._non_idempotent_done = False
        self._escalations = 0
        self._approved_steps: set[str] = set()
        self._step_started = 0.0
        self._blocked_before = 0
        self._recovered_in_step = False

    # ================================================================== entry point

    def run(self, raw_params: dict[str, Any]) -> RunResult:
        t0 = time.time()
        self.rec.event(
            "replay_started",
            capability=self.cap.id,
            version=self.cap.version,
            tenant=self.tenant.tenant,
            tenant_patched_steps=self.patched_steps,
            review_status=self.cap.review.status,
        )
        try:
            self._preflight(raw_params)
            self._execute()
            kw: dict[str, Any] = {"status": "succeeded", "outputs": dict(self.outputs)}
        except _Stop as s:
            kw = s.kw
        except PlaywrightError as e:
            kw = self._fail(ErrorCode.SURFACE_ERROR, f"surface error: {e}", None).kw
        result = RunResult(
            run_id=self.rec.run_id,
            mode="replay",
            capability=self.cap.id,
            capability_version=self.cap.version,
            tenant=self.tenant.tenant,
            steps=self.step_records,
            recoveries=self.recoveries,
            drift=self.drift,
            interventions=self.interventions,
            duration_ms=int((time.time() - t0) * 1000),
            evidence_dir=str(self.rec.dir),
            **kw,
        )
        if result.status != "failed":
            self.rec.shot(self.surface, f"final-{result.status}")
        self.rec.write_json("result.json", result)  # outputs are redacted here (known values)
        self.rec.event(
            "replay_finished",
            status=result.status,
            outputs=result.outputs,
            outcome=result.outcome,
            error=result.error.code.value if result.error else None,
            recoveries=len(result.recoveries),
            drift=len(result.drift),
            interventions=len(result.interventions),
            ms=result.duration_ms,
        )
        return result

    # ================================================================== preflight

    def _preflight(self, raw: dict[str, Any]) -> None:
        errors: list[str] = []
        specs = {p.name: p for p in self.cap.inputs}
        for name in set(raw) - set(specs):
            errors.append(f"unknown input '{name}'")
        for spec in self.cap.inputs:
            v = raw.get(spec.name)
            if v is None or v == "":
                if spec.required:
                    errors.append(f"'{spec.name}' is required")
                continue
            s = str(v)
            try:
                if spec.type == "integer":
                    int(s)
                elif spec.type == "decimal":
                    Decimal(s)
                elif spec.type == "boolean" and s.lower() not in ("true", "false"):
                    raise ValueError
            except (ValueError, InvalidOperation):
                errors.append(f"'{spec.name}' must be {spec.type}")
            if spec.pattern and not re.fullmatch(spec.pattern, s):
                errors.append(f"'{spec.name}' does not match {spec.pattern}")
            if spec.enum and s not in spec.enum:
                errors.append(f"'{spec.name}' must be one of {spec.enum}")
            self.params[spec.name] = s
            if spec.sensitivity in SENSITIVE:
                self.redactor.add(s, spec.name)
        self.rec.event("inputs_validated", inputs={k: v for k, v in self.params.items()}, errors=errors)
        if errors:
            raise _Stop(
                status="failed",
                error=ErrorDetail(
                    code=ErrorCode.INVALID_INPUT, message="; ".join(errors), retryable=False, expected="inputs per contract"
                ),
            )
        if self.cap.review.status != "approved" and not self.opts.allow_draft:
            raise _Stop(
                status="blocked",
                error=ErrorDetail(
                    code=ErrorCode.NOT_APPROVED,
                    message=f"capability is '{self.cap.review.status}'; unattended replay requires 'approved'",
                ),
            )
        if not version_satisfies(self.tenant.product_version, self.cap.app.versions):
            raise _Stop(
                status="failed",
                error=ErrorDetail(
                    code=ErrorCode.INCOMPATIBLE_APP,
                    message=f"{self.tenant.tenant} runs {self.cap.app.product} {self.tenant.product_version}; "
                    f"capability supports {self.cap.app.versions}",
                ),
            )
        every_step = list(self.cap.steps) + list(self.profile.login)
        for o in list(self.cap.outcomes) + list(self.profile.outcomes):
            every_step += o.recovery.steps if o.recovery else []
        for st in every_step:
            if st.action.type not in self.tenant.policy.allowed_actions:
                raise self._fail(ErrorCode.POLICY_VIOLATION, f"step uses disallowed action {st.action.type}", st, shot=False)
            if st.action.type == "navigate":
                ok, why = self.policy.allows_url(self._url(st.action.url))
                if not ok:
                    raise self._fail(ErrorCode.POLICY_VIOLATION, why, st, shot=False)
        irreversible = [s for s in self.cap.steps if s.risk is Risk.irreversible]
        if irreversible and not self.opts.approve_irreversible and not self._has_operator():
            raise _Stop(
                status="blocked",
                error=ErrorDetail(
                    code=ErrorCode.APPROVAL_REQUIRED,
                    message=f"step '{irreversible[0].id}' is irreversible ({irreversible[0].intent}); "
                    "pass an explicit approval or attach an operator. Nothing was executed.",
                    step_id=irreversible[0].id,
                    step_intent=irreversible[0].intent,
                ),
            )

    # ================================================================== flow

    def _execute(self) -> None:
        relogin = self.cap.requires_session
        restarts = 0
        while True:
            try:
                if relogin:
                    self._login()
                else:
                    self._go_home()
                for step in self.cap.steps:
                    self._step(step)
                self._checkpoint()
                return
            except _Restart as r:
                restarts += 1
                if self._non_idempotent_done:
                    raise self._fail(
                        ErrorCode.RECOVERY_EXHAUSTED,
                        f"cannot restart after '{r.reason}': a non-idempotent step already executed",
                        None,
                    )
                if restarts > self.opts.max_restarts:
                    raise self._fail(ErrorCode.RECOVERY_EXHAUSTED, f"gave up after {restarts - 1} restarts ({r.reason})", None)
                self.rec.event("flow_restart", reason=r.reason, relogin=r.relogin, attempt=restarts)
                relogin = r.relogin
                self.outputs.clear()

    def _login(self) -> None:
        self.rec.event("session_login", product=self.profile.product)
        self.control.ensure_automation()
        self.surface.navigate(self.base + "/")
        for step in self.profile.login:
            self._step(step, phase="login")
        self._await(self.profile.landing, None, timeout_ms=15000)
        self.rec.event("session_established")

    def _go_home(self) -> None:
        self.surface.navigate(self.base + "/main")
        self._await(self.profile.landing, None, timeout_ms=15000)

    def _checkpoint(self) -> None:
        self._await(self.cap.success, None, timeout_ms=10000, fail_code=ErrorCode.CHECKPOINT_FAILED)
        missing = [o.name for o in self.cap.outputs if o.name not in self.outputs]
        if missing:
            raise self._fail(ErrorCode.EXTRACTION_FAILED, f"declared outputs not produced: {missing}", None)
        self.rec.event("checkpoint_verified", condition=describe_cond(self.cap.success))

    # ================================================================== one step

    def _step(self, step: Step, phase: str = "capability") -> None:
        t0 = time.time()
        self._step_started = t0
        self._recovered_in_step = False
        dialogs_before = len(self.surface.dialogs)
        self._blocked_before = len(self.surface.blocked_requests)
        self.rec.event("step_started", step=step.id, phase=phase, action=step.action.type, intent=step.intent)
        self._precheck(step)
        a = step.action
        resolved: Resolved | None = None
        if a.type == "navigate":
            url = self._url(a.url)
            if self._gate(step, None, nav_url=url):
                self.control.ensure_automation()
                self.surface.navigate(url, a.frame)
        else:
            target: Target | None = getattr(a, "target", None)
            if target is not None:
                resolved = self._resolve(step, target)
            if self._gate(step, resolved):
                self._perform(step, resolved)
        if not step.idempotent:
            self._non_idempotent_done = True
        if step.expect is not None:
            self._await(step.expect, step)
        for d in self.surface.dialogs[dialogs_before:]:
            self.recoveries.append(
                RecoveryEvent(
                    outcome_id="native_dialog",
                    step_id=step.id,
                    strategy="accept_alert" if d["accepted"] else "dismiss_dialog",
                    succeeded=True,
                    detail=self.redactor.text(d["message"]),
                )
            )
            self._recovered_in_step = True
        self._check_blocked(step)
        for frame, url in self.surface.locations().items():
            ok, why = self.policy.allows_url(url)
            if not ok:
                raise self._fail(ErrorCode.POLICY_VIOLATION, f"frame {frame} left the allowlist: {why}", step)
        ms = int((time.time() - t0) * 1000)
        self.step_records.append(
            StepRecord(
                step_id=step.id,
                status="recovered" if self._recovered_in_step else "ok",
                locator=describe_locator(resolved.locator) if resolved else None,
                duration_ms=ms,
            )
        )
        if ms > self.opts.slow_step_ms:
            self.rec.event("slow_step", step=step.id, ms=ms, note="absorbed by settle-wait (no fixed sleeps)")
        self.rec.event("step_ok", step=step.id, ms=ms, locator=describe_locator(resolved.locator) if resolved else None)

    def _check_blocked(self, step: Step | None) -> None:
        """A request outside the allowlist was aborted at the network layer: stop now,
        rather than classifying the resulting error page as an unknown screen."""
        if len(self.surface.blocked_requests) > self._blocked_before:
            url = self.surface.blocked_requests[-1]
            self._blocked_before = len(self.surface.blocked_requests)
            raise self._fail(ErrorCode.POLICY_VIOLATION, f"request outside the allowlist was blocked: {url}", step)

    def _precheck(self, step: Step) -> None:
        """An interstitial or expired session may already be on screen before we act."""
        deadline = time.time() + 10
        while not self.surface.settled() and time.time() < deadline:
            self.surface.pump(self.opts.poll_ms)
        o = self._match_outcome(step)
        if o:
            self._handle_outcome(o, step)

    def _perform(self, step: Step, r: Resolved | None) -> None:
        a = step.action
        for attempt in (1, 2):
            self.control.ensure_automation()
            try:
                if a.type == "click":
                    self.surface.click(r)
                elif a.type == "fill":
                    self.surface.fill(r, self._value(a.value))
                elif a.type == "select":
                    self.surface.select(r, self._value(a.option))
                elif a.type == "press":
                    self.surface.press(a.key, r)
                elif a.type == "extract":
                    self._extract(step, r)
                return
            except PlaywrightError as e:
                if attempt == 2 or not step.idempotent or a.target is None:  # type: ignore[union-attr]
                    raise self._fail(ErrorCode.SURFACE_ERROR, f"{a.type} failed: {e}".splitlines()[0], step)
                self.rec.event("action_retry", step=step.id, error=str(e).splitlines()[0])
                r = self._resolve(step, a.target)  # type: ignore[union-attr]

    def _extract(self, step: Step, r: Resolved | None) -> None:
        assert r is not None and step.action.type == "extract"
        name = step.action.output
        spec = next((o for o in self.cap.outputs if o.name == name), None)
        if spec is None:
            raise self._fail(ErrorCode.EXTRACTION_FAILED, f"output '{name}' is not declared", step)
        raw = self.surface.read(r)
        if spec.sensitivity in SENSITIVE:
            self.redactor.add(raw, name)
        try:
            value = parse_output(raw, spec)
        except (ValueError, InvalidOperation) as e:
            raise self._fail(ErrorCode.EXTRACTION_FAILED, f"'{name}' is not a valid {spec.type}: {e}", step)
        if spec.sensitivity in SENSITIVE:
            self.redactor.add(str(value), name)
        self.outputs[name] = value
        self.rec.event("extracted", step=step.id, output=name, value_type=spec.type, value=str(value))

    def _value(self, v: Value) -> str:
        if isinstance(v, ParamRef):
            return self.params[v.param]
        if isinstance(v, SecretRef):
            secret = self.secrets.get(v.secret)
            self.redactor.add(secret, f"secret:{v.secret}")
            return secret
        if isinstance(v, LiteralValue):
            return v.literal
        raise TypeError(v)

    def _url(self, template: str) -> str:
        return urllib.parse.urljoin(self.base + "/", substitute(template, self.params).lstrip("/"))

    # ================================================================== policy gate

    def _gate(self, step: Step, r: Resolved | None, nav_url: str | None = None) -> bool:
        """True = perform the action; False = a human already did it."""
        a = step.action
        approved = self.opts.approve_irreversible or step.id in self._approved_steps
        d = self.policy.check(
            a.type,
            element=r.info if r else None,
            frame_url=r.frame.url if r else None,
            nav_url=nav_url,
            key=getattr(a, "key", None),
            declared_risk=step.risk,
            approved=approved,
        )
        self.rec.event("policy_check", step=step.id, allowed=d.allowed, risk=d.risk.value, reason=d.reason or None)
        if d.risk is Risk.irreversible:
            # Runtime classification is authoritative even if an artifact understated risk.
            step.idempotent = False
        if d.allowed:
            return True
        if d.needs_approval:
            res = self._escalate(
                "needs_approval",
                f"step '{step.id}' is irreversible and needs approval: {step.intent}",
                step,
                proposed={"action": a.type, "target": getattr(getattr(a, "target", None), "description", None)},
                allowed=("approve", "deny", "completed", "abort"),
            )
            if res and res.decision == "approve":
                self._approved_steps.add(step.id)
                return self._gate(step, r, nav_url)
            if res and res.decision == "completed":
                return False
            raise _Stop(
                status="blocked",
                error=ErrorDetail(
                    code=ErrorCode.APPROVAL_REQUIRED,
                    message=f"irreversible step not approved ({res.decision if res else 'no approval given'})",
                    step_id=step.id,
                    step_intent=step.intent,
                ),
            )
        raise self._fail(ErrorCode.POLICY_VIOLATION, d.reason, step)

    # ================================================================== waiting & classification

    def _resolve(self, step: Step, target: Target) -> Resolved:
        deadline = time.time() + step.timeout_ms / 1000
        settled_since: float | None = None
        frames = {target.frame or "top"}
        while True:
            self._check_blocked(step)
            if self.surface.settled(frames=frames):
                o = self._match_outcome(step)
                if o:
                    self._handle_outcome(o, step)
                    settled_since, deadline = None, time.time() + step.timeout_ms / 1000
                    continue
                r = self.surface.resolve(target, self.params)
                if isinstance(r, Resolved):
                    if r.index > 0:
                        self._report_drift(step, target, r)
                    return r
                settled_since = settled_since or time.time()
                if (time.time() - settled_since) * 1000 >= self.opts.settle_grace_ms:
                    code = ErrorCode.TARGET_AMBIGUOUS if r.reason == "ambiguous" else ErrorCode.TARGET_NOT_FOUND
                    msg = f"could not uniquely resolve <{target.description}>: " + "; ".join(r.attempts)
                    if self._escalate_and_resume(code.value.lower(), msg, step):
                        settled_since, deadline = None, time.time() + step.timeout_ms / 1000
                        continue
                    raise self._fail(code, msg, step, expected=f"element <{target.description}>")
            else:
                settled_since = None
            if time.time() > deadline:
                self._on_timeout(step, f"element <{target.description}>")
                deadline = time.time() + step.timeout_ms / 1000
            self.surface.pump(self.opts.poll_ms)

    def _await(
        self,
        cond: Condition,
        step: Step | None,
        timeout_ms: int | None = None,
        fail_code: ErrorCode = ErrorCode.UNRECOGNIZED_STATE,
    ) -> None:
        budget = (timeout_ms or (step.timeout_ms if step else 15000)) / 1000
        deadline = time.time() + budget
        settled_since: float | None = None
        frames = frames_of(cond) or None
        while True:
            self._check_blocked(step)
            if self.surface.settled(frames=frames):
                o = self._match_outcome(step)
                if o:
                    self._handle_outcome(o, step)
                    settled_since, deadline = None, time.time() + budget
                    continue
                if evaluate(cond, self.surface, self.params, since=self._step_started):
                    return
                settled_since = settled_since or time.time()
                if (time.time() - settled_since) * 1000 >= self.opts.settle_grace_ms:
                    msg = (
                        "screen settled in a state that matches neither the expected postcondition nor any known outcome"
                        if fail_code is ErrorCode.UNRECOGNIZED_STATE
                        else "success checkpoint not satisfied"
                    )
                    if self._escalate_and_resume(fail_code.value.lower(), msg, step):
                        settled_since, deadline = None, time.time() + budget
                        continue
                    raise self._fail(fail_code, msg, step, expected=describe_cond(cond))
            else:
                settled_since = None
            if time.time() > deadline:
                self._on_timeout(step, describe_cond(cond))
                deadline = time.time() + budget
            self.surface.pump(self.opts.poll_ms)

    def _on_timeout(self, step: Step | None, expected: str) -> None:
        """Transient slowness: restart once (if safe), then escalate, then fail."""
        key = "transient_timeout"
        n = self._attempts[key] = self._attempts.get(key, 0) + 1
        if n == 1 and not self._non_idempotent_done:
            self.recoveries.append(RecoveryEvent(outcome_id=key, step_id=step.id if step else None, strategy="restart", succeeded=True))
            raise _Restart(relogin=False, reason=f"timeout waiting for {expected}")
        if self._escalate_and_resume("timeout", f"timed out waiting for {expected}", step):
            return
        raise self._fail(ErrorCode.TIMEOUT, f"timed out waiting for {expected}", step, expected=expected, retryable=True)

    def _match_outcome(self, step: Step | None) -> Outcome | None:
        sid = step.id if step else None
        pool = list(self.cap.outcomes) + list(self.profile.outcomes)
        for o in pool:
            if o.id in self._suppressed:
                continue
            if o.steps is not None and sid not in o.steps:
                continue
            if evaluate(o.when, self.surface, self.params, since=self._step_started):
                return o
        return None

    def _handle_outcome(self, o: Outcome, step: Step | None) -> None:
        sid = step.id if step else None
        self.rec.event("outcome_detected", outcome=o.id, kind=o.kind.value, code=o.code, step=sid)
        if o.kind is OutcomeKind.business:
            shot = self.rec.shot(self.surface, f"outcome-{o.id}")
            self.rec.event("business_outcome", outcome=o.id, code=o.code, screenshot=shot)
            raise _Stop(
                status="business_outcome",
                outcome=BusinessOutcome(code=o.code, message=o.message, outcome_id=o.id, step_id=sid),
            )
        n = self._attempts[o.id] = self._attempts.get(o.id, 0) + 1
        rec_cfg = o.recovery
        if rec_cfg is None or n > rec_cfg.max_attempts:
            if o.kind is OutcomeKind.failure:
                raise self._fail(ErrorCode.APP_ERROR, o.message, step, outcome_code=o.code, retryable=o.retryable)
            raise self._fail(
                ErrorCode.RECOVERY_EXHAUSTED, f"{o.id}: recovery attempted {n - 1} times without success", step, outcome_code=o.code
            )
        self._recovered_in_step = True
        if rec_cfg.strategy == "run_steps":
            self._suppressed.add(o.id)
            started = self._step_started
            try:
                for rs in rec_cfg.steps:
                    self._step(rs, phase=f"recovery:{o.id}")
            finally:
                self._suppressed.discard(o.id)
                self._step_started = started
            self.recoveries.append(RecoveryEvent(outcome_id=o.id, step_id=sid, strategy="run_steps", succeeded=True))
            return
        if rec_cfg.strategy in ("restart", "relogin_restart"):
            self.recoveries.append(RecoveryEvent(outcome_id=o.id, step_id=sid, strategy=rec_cfg.strategy, succeeded=True))
            raise _Restart(relogin=rec_cfg.strategy == "relogin_restart", reason=o.id)
        if rec_cfg.strategy == "escalate":
            if self._escalate_and_resume(o.id, o.message, step):
                self.recoveries.append(RecoveryEvent(outcome_id=o.id, step_id=sid, strategy="escalate", succeeded=True))
                return
            raise self._fail(ErrorCode.APP_ERROR, o.message, step, outcome_code=o.code)

    def _report_drift(self, step: Step, target: Target, r: Resolved) -> None:
        ev = DriftEvent(
            step_id=step.id,
            target=target.description,
            primary=describe_locator(target.locators[0]),
            used=describe_locator(r.locator),
            note="primary locator failed: " + "; ".join(r.attempts),
        )
        self.drift.append(ev)
        self.rec.event("drift", **ev.model_dump())

    # ================================================================== escalation

    def _has_operator(self) -> bool:
        return self.opts.operator is not None and not isinstance(self.opts.operator, NoOperator)

    def _escalate(
        self,
        kind: str,
        reason: str,
        step: Step | None,
        proposed: dict[str, Any] | None = None,
        allowed: tuple[str, ...] = ("resume", "abort"),
    ) -> Resolution | None:
        if not self._has_operator() or self._escalations >= self.opts.max_escalations:
            return None
        self._escalations += 1
        req = InterventionRequest(
            id=self.control.next_id(),
            run_id=self.rec.run_id,
            kind=kind,
            reason=self.redactor.text(reason),
            subject=f"{self.cap.id}@{self.cap.version} (tenant {self.tenant.tenant})",
            step_id=step.id if step else None,
            step_intent=step.intent if step else None,
            locations={k: self.redactor.text(path_query(v)) for k, v in self.surface.locations().items()},
            alerts=[self.redactor.text(a) for a in self.surface.visible_alerts()],
            screenshot=self.rec.shot(self.surface, f"intervention-{kind}"),
            proposed_action=proposed,
            allowed=allowed,
        )
        assert self.opts.operator is not None
        res = self.control.escalate(req, self.opts.operator, self.surface, self.redactor)
        self.interventions.append(
            InterventionRecord(
                id=req.id,
                reason=req.reason,
                kind=kind,
                step_id=req.step_id,
                operator=res.operator,
                resolution=res.decision,
                note=res.note,
                waited_ms=res.waited_ms,
                human_actions=[
                    HumanAction(
                        type=e.get("type", "?"),
                        target=f'{(e.get("info") or {}).get("role")} "{(e.get("info") or {}).get("name")}"',
                        frame=e.get("frame") or "top",
                        value=self.redactor.text(str((e.get("info") or {}).get("value")))
                        if e.get("type") == "change"
                        else None,
                    )
                    for e in res.human_events
                ],
            )
        )
        return res

    def _escalate_and_resume(self, kind: str, reason: str, step: Step | None) -> bool:
        res = self._escalate(kind, reason, step)
        if res is None:
            return False
        if res.decision == "resume":
            self._recovered_in_step = True
            return True
        return False

    # ================================================================== failure evidence

    def _fail(
        self,
        code: ErrorCode,
        message: str,
        step: Step | None,
        expected: str | None = None,
        retryable: bool = False,
        outcome_code: str | None = None,
        shot: bool = True,
    ) -> _Stop:
        observed = None
        if shot:
            label = step.id if step else code.value
            screenshot, dom = self.rec.failure_bundle(self.surface, label)
            observed = Observed(
                locations={k: self.redactor.text(path_query(v)) for k, v in self.surface.locations().items()},
                alerts=[self.redactor.text(a) for a in self.surface.visible_alerts()],
                screenshot=screenshot,
                dom=dom,
            )
        err = ErrorDetail(
            code=code,
            message=self.redactor.text(message) + (f" [{outcome_code}]" if outcome_code else ""),
            step_id=step.id if step else None,
            step_intent=step.intent if step else None,
            expected=expected,
            observed=observed,
            retryable=retryable,
        )
        self.rec.event("failure", code=code.value, step=err.step_id, message=err.message, expected=expected)
        return _Stop(status="failed", error=err)


# ====================================================================== convenience


def replay(
    capability: str | Capability,
    tenant_name: str,
    params: dict[str, Any],
    opts: ReplayOptions | None = None,
    *,
    headless: bool = True,
    runs_dir: Path = RUNS,
    console: bool = True,
    tenant: TenantConfig | None = None,
) -> RunResult:
    tenant = tenant or load_tenant(tenant_name)
    cap = load_capability(capability) if isinstance(capability, str) else capability
    profile = load_profile(cap.app.product)
    cap_eff, patched = effective_capability(cap, tenant)
    redactor = Redactor()
    recorder = RunRecorder(runs_dir, "replay", redactor, console=console)
    policy = PolicyEngine(tenant.policy)
    surface = WebSurface(url_allowed=policy.allows_url, headless=headless, on_event=recorder.event).start()
    try:
        return Replayer(cap_eff, profile, tenant, surface, recorder, redactor, opts, patched).run(params)
    finally:
        surface.close()
        recorder.close()
