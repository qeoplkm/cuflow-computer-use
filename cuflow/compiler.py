"""Compile a discovery trace into a Capability artifact.

The artifact is derived from *what happened on screen*, not from the model transcript:
each recorded action becomes a step with a verified target; postconditions come from the
observed state change (which frame navigated where, and the next control we need being
present); inputs/outputs come from the parameters and extractions the agent declared.

Postcondition rule: after step i, wait until (a) every frame that navigated during
discovery is at the canonicalized location, and (b) the target of step i+1 resolves.
"Wait until the next thing I need exists" is a principled, generic expectation that needs
no per-app tuning and never relies on sleeps.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .evidence import now_iso
from .schema import (
    RISK_ORDER,
    AllCondition,
    AppRef,
    Capability,
    ClickAction,
    Condition,
    ElementCondition,
    ExtractAction,
    FillAction,
    LiteralValue,
    LocationCondition,
    OutputSpec,
    ParamRef,
    ParamSpec,
    PressAction,
    Provenance,
    Review,
    Risk,
    SelectAction,
    Sensitivity,
    Step,
    Target,
    TargetRef,
    TextCondition,
)
from .targets import canonical_location, parametrize


def location_condition(url: str, frame: str, examples: dict[str, str]) -> LocationCondition:
    volatile = {"t", "token", "csrf", "nonce", "session", "sessionid", "access_token"}
    ignored = sorted({k for k, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query) if k.lower() in volatile})
    return LocationCondition(frame=None if frame == "top" else frame,
                             pattern=canonical_location(url, examples), ignore_query=ignored)


@dataclass
class RecordedStep:
    actor: str  # agent | human
    action: str  # click | fill | select | press | extract
    intent: str
    frame: str
    target: Target | None
    risk: Risk
    before: dict[str, str]
    after: dict[str, str]
    value: dict[str, str] | None = None  # {"param": name} | {"literal": text}
    key: str | None = None
    output: dict[str, Any] | None = None  # name, type, description, sensitivity


@dataclass
class ParamDecl:
    name: str
    example: str
    description: str
    sensitivity: Sensitivity = Sensitivity.pii


@dataclass
class DoneInfo:
    capability_name: str
    description: str
    success_text: str
    summary: str = ""
    notes: list[str] = field(default_factory=list)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40] or "step"


def _short(desc: str) -> str:
    return re.sub(r"\b(text box|button|link|drop-down|cell|of the row where.*)$", "", desc).strip() or desc


def compile_capability(
    *,
    trace: list[RecordedStep],
    params: dict[str, ParamDecl],
    done: DoneInfo,
    goal: str,
    product: str,
    product_versions: str,
    tenant: str,
    app_version: str,
    model: str,
    run_id: str,
    sensitive_values: list[str],
) -> Capability:
    examples = {p.example: p.name for p in params.values()}
    steps: list[Step] = []
    used: set[str] = set()
    targets: dict[str, Target] = {}

    def ref(t: Target) -> TargetRef:
        """Define each control once; identical targets (same frame + locators) share a name."""
        for name, existing in targets.items():
            if existing.frame == t.frame and existing.locators == t.locators:
                return TargetRef(ref=name)
        base_name = _target_name(t)
        name, k = base_name, 2
        while name in targets:
            name, k = f"{base_name}_{k}", k + 1
        targets[name] = t
        return TargetRef(ref=name)

    for i, rs in enumerate(trace):
        base = f"{rs.action}_{_slug(_short(rs.target.description) if rs.target else rs.key or 'key')}"
        if rs.action == "extract" and rs.output:
            base = f"extract_{rs.output['name']}"
        sid, n = base, 2
        while sid in used:
            sid, n = f"{base}_{n}", n + 1
        used.add(sid)

        tref = ref(rs.target) if rs.target is not None else None
        if rs.action == "click":
            action: Any = ClickAction(target=tref)
        elif rs.action == "fill":
            action = FillAction(target=tref, value=_value(rs.value))
        elif rs.action == "select":
            action = SelectAction(target=tref, option=_value(rs.value))
        elif rs.action == "press":
            action = PressAction(key=rs.key or "Enter", target=tref)
        else:
            action = ExtractAction(target=tref, output=rs.output["name"])  # type: ignore[index]

        conds: list[Condition] = []
        if rs.action in ("click", "press", "select"):
            changed = {f: u for f, u in rs.after.items() if rs.before.get(f) != u}
            for f, u in changed.items():
                if f == "top" and len(changed) > 1:
                    continue
                conds.append(location_condition(u, f, examples))
            nxt = next((t for t in trace[i + 1 :] if t.target is not None), None)
            if nxt is not None and nxt.target is not None:
                conds.append(ElementCondition(target=ref(nxt.target)))
        expect = None if not conds else conds[0] if len(conds) == 1 else AllCondition(of=conds)
        steps.append(
            Step(
                id=sid,
                intent=parametrize(rs.intent, examples) or sid,
                action=action,
                risk=rs.risk,
                # re-typing a value is idempotent even if the model over-declared its risk
                idempotent=rs.risk is Risk.safe or (rs.action in ("fill", "select") and rs.risk is not Risk.irreversible),
                expect=expect,
                actor="human" if rs.actor == "human" else "agent",
            )
        )

    last = trace[-1] if trace else None
    success: list[Condition] = []
    text_ok = done.success_text and not any(v and v in done.success_text for v in sensitive_values)
    if text_ok:
        success.append(TextCondition(pattern=parametrize(done.success_text, examples), frame=last.frame if last and last.frame != "top" else None))
    if last is not None:
        url = last.after.get(last.frame)
        if url:
            success.append(location_condition(url, last.frame, examples))
    if not success:
        success.append(TextCondition(pattern=done.success_text))

    inputs = []
    notes = list(done.notes)
    for p in params.values():
        pattern = r"^\d+$" if p.example.isdigit() else None
        if pattern:
            notes.append(f"inputs.{p.name}: pattern {pattern} inferred from a single example; tighten it if the app enforces a format.")
        inputs.append(ParamSpec(name=p.name, type="string", description=p.description, pattern=pattern, sensitivity=p.sensitivity))
    outputs = [
        OutputSpec(
            name=rs.output["name"],
            type=rs.output.get("type", "string"),
            description=rs.output.get("description", ""),
            sensitivity=rs.output.get("sensitivity", Sensitivity.financial),
        )
        for rs in trace
        if rs.action == "extract" and rs.output
    ]
    for s in steps:
        if s.actor == "human":
            notes.append(f"steps.{s.id} was performed by a human operator during discovery; verify its target.")
        tgt = getattr(s.action, "target", None)
        tgt = targets.get(tgt.ref) if isinstance(tgt, TargetRef) else tgt
        if tgt is not None and all(loc.by == "css" for loc in tgt.locators):
            notes.append(f"steps.{s.id} has only a structural (css) locator; it will be brittle.")
    notes.append(
        "Outcomes: discovery only observed the happy path. Add the business outcomes and error states this "
        "flow can hit (not found, validation, restricted) before approving."
    )
    side = max((s.risk for s in steps), key=lambda r: RISK_ORDER[r], default=Risk.safe)
    name = ".".join(filter(None, (_slug(part) for part in done.capability_name.split("."))))
    cap_id = name if name.startswith(product + ".") else f"{product}.{name}"
    return Capability(
        id=cap_id,
        version="1.0.0",
        title=done.capability_name.replace("_", " ").replace(".", " ").strip().capitalize(),
        description=parametrize(done.description, examples),
        app=AppRef(product=product, versions=product_versions, surface="web"),
        requires_session=True,
        inputs=inputs,
        outputs=outputs,
        side_effects=side,
        targets=targets,
        steps=steps,
        success=success[0] if len(success) == 1 else AllCondition(of=success),
        outcomes=[],
        review=Review(status="draft", notes=notes),
        provenance=Provenance(
            method="llm_discovery",
            recorded_at=now_iso(),
            run_id=run_id,
            model=model,
            tenant=tenant,
            app_version=app_version,
            goal_template=parametrize(goal, examples),
        ),
    )


def _target_name(t: Target) -> str:
    first = t.locators[0]
    if first.by == "table_cell" and first.column and first.row_key:
        return _slug(f"{first.column} {first.row_key.value}".replace("{", "").replace("}", ""))
    if first.by == "table_cell" and first.row_label:
        return _slug(f"{first.row_label} value")
    return _slug(t.description.replace('"', "").replace("{", "").replace("}", ""))


def _value(v: dict[str, str] | None) -> Any:
    if v and "param" in v:
        return ParamRef(param=v["param"])
    return LiteralValue(literal=(v or {}).get("literal", ""))
