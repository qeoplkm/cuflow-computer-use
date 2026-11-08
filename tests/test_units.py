"""Fast unit tests: schema, redaction, policy, config/overrides, control state machine."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cuflow.config import dump_yaml, merge_patch, version_satisfies, effective_capability, load_yaml
from cuflow.control import ControlError, ControlState, SessionControl
from cuflow.policy import PolicyEngine
from cuflow.redaction import Redactor
from cuflow.schema import Capability, CapabilityOverride, Policy, Risk, Target
from cuflow.targets import canonical_location, parametrize

from .conftest import FIXTURES, fixture_capability, tenant_at


# ---- schema ---------------------------------------------------------------------------

def test_fixture_round_trips_through_yaml() -> None:
    cap = fixture_capability()
    again = Capability.model_validate(load_yaml_str(dump_yaml(cap)))
    assert again == cap
    assert again.steps[0].action.target.locators[0].by == "role"


def load_yaml_str(s: str):
    import yaml
    return yaml.safe_load(s)


def test_target_requires_a_locator() -> None:
    with pytest.raises(ValidationError):
        Target(description="x", locators=[])


def test_unknown_fields_are_rejected() -> None:
    data = load_yaml(FIXTURES / "get_savings_balance.yaml")
    data["steps"][0]["action"]["target"]["selector"] = "#oops"
    with pytest.raises(ValidationError):
        Capability.model_validate(data)


def test_json_schema_is_generated() -> None:
    s = Capability.model_json_schema()
    assert "Locator" in str(s) or "RoleLocator" in str(s)


# ---- redaction ------------------------------------------------------------------------

def test_redaction_patterns_and_known_values() -> None:
    r = Redactor()
    r.add("SAMPLE, JORDAN Q", "pii")
    r.add("10042", "member_id")
    out = r.text("Member 10042 SAMPLE, JORDAN Q ssn 123-45-6789 bal $2,345.67 tel (555) 010-4242 a@b.co")
    for leaked in ("10042", "JORDAN", "123-45-6789", "2,345.67", "010-4242", "a@b.co"):
        assert leaked not in out
    assert "‹member_id›" in out


def test_redaction_luhn_cards_only_and_short_values_ignored() -> None:
    r = Redactor()
    r.add("ab", "x")  # too short to be safely redacted everywhere
    assert "4111 1111 1111 1111" not in r.text("card 4111 1111 1111 1111")
    assert r.text("tab") == "tab"


# ---- policy ---------------------------------------------------------------------------

POLICY = Policy(allowed_origins=["http://h:1"], allowed_paths=["/", "/work/*"], denied_paths=["/__admin*"])


@pytest.mark.parametrize(
    "url,ok",
    [("http://h:1/work/inquiry", True), ("http://evil:1/work/x", False), ("http://h:1/__admin/faults", False), ("http://h:1/other", False)],
)
def test_allowlist(url: str, ok: bool) -> None:
    assert PolicyEngine(POLICY).allows_url(url)[0] is ok


def test_risk_classification() -> None:
    p = PolicyEngine(POLICY)
    assert p.classify("click", {"name": "Search"}) is Risk.safe
    assert p.classify("click", {"name": "Submit"}) is Risk.irreversible
    assert p.classify("fill", {"name": "Amount"}) is Risk.safe
    # pressing Enter in a form is clicking its submit button
    assert p.classify("press", {"name": "Amount", "form_submit_name": "Submit"}, key="Enter") is Risk.irreversible
    assert p.classify("click", {"name": "Save draft"}) is Risk.reversible


def test_gate_requires_approval_and_blocks_disallowed() -> None:
    p = PolicyEngine(POLICY)
    d = p.check("click", element={"name": "Submit"}, frame_url="http://h:1/work/review")
    assert not d.allowed and d.needs_approval
    assert p.check("click", element={"name": "Submit"}, frame_url="http://h:1/work/review", approved=True).allowed
    assert not p.check("click", element={"name": "Search"}, frame_url="http://evil:1/").allowed
    assert not PolicyEngine(POLICY.model_copy(update={"allowed_actions": ["click"]})).check("fill").allowed
    # the model under-declaring risk does not lower it
    assert not p.check("click", element={"name": "Confirm"}, declared_risk=Risk.safe).allowed


# ---- config / overrides --------------------------------------------------------------

def test_merge_patch() -> None:
    assert merge_patch({"a": {"b": 1, "c": 2}, "l": [1]}, {"a": {"b": None, "d": 3}, "l": [2]}) == {"a": {"c": 2, "d": 3}, "l": [2]}


@pytest.mark.parametrize("v,spec,ok", [("4.2.1", ">=4.2,<5", True), ("5.0", ">=4.2,<5", False), ("1.3.0", "1.x", True), ("2.0.0", "1.x", False)])
def test_version_satisfies(v: str, spec: str, ok: bool) -> None:
    assert version_satisfies(v, spec) is ok


def test_tenant_override_patches_by_step_id() -> None:
    cap = fixture_capability()
    ov = CapabilityOverride(
        applies_to="0.x",
        reason="relabelled field",
        steps={"enter_member_number": {"action": {"target": {"locators": [{"by": "role", "role": "textbox", "name": "Member #"}]}}}},
    )
    t = tenant_at("harbor", "http://h:1", overrides={cap.id: ov})
    eff, patched = effective_capability(cap, t)
    assert patched == ["steps.enter_member_number"]
    step = next(s for s in eff.steps if s.id == "enter_member_number")
    assert step.action.target.locators[0].name == "Member #"
    assert step.action.target.description == "Member Number text box"  # untouched fields survive
    bad = tenant_at("harbor", "http://h:1", overrides={cap.id: ov.model_copy(update={"steps": {"nope": {}}})})
    with pytest.raises(ValueError):
        effective_capability(cap, bad)


# ---- generalization helpers -----------------------------------------------------------

def test_parametrize_and_canonical_location() -> None:
    ex = {"10042": "member_id"}
    assert parametrize("View 10042", ex) == "View {member_id}"
    assert parametrize("110042", ex) == "110042"  # whole tokens only
    assert canonical_location("/work/member?m=10042&t=9cc643", ex) == "/work/member?m={member_id}&t=*"


# ---- control state machine ------------------------------------------------------------

class _Rec:
    def __init__(self) -> None:
        self.events: list = []

    def event(self, *a, **k) -> None:
        self.events.append((a, k))


def test_control_transitions() -> None:
    c = SessionControl(_Rec())  # type: ignore[arg-type]
    c.ensure_automation()
    c._transition(ControlState.AWAITING_HUMAN, "nobody")
    with pytest.raises(ControlError):
        c.ensure_automation()
    with pytest.raises(ControlError):
        c._transition(ControlState.AWAITING_HUMAN, "x")
    c._transition(ControlState.HUMAN, "op")
    with pytest.raises(ControlError):
        c.ensure_automation()
    c._transition(ControlState.AUTOMATION, "automation")
    c.ensure_automation()
