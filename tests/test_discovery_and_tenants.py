"""Discovery (with a scripted model) -> compiled artifact -> replay; and cross-tenant reuse."""

from __future__ import annotations

from pathlib import Path
import json
import pytest

from cuflow.config import dump_yaml, load_profile
from cuflow.control import ScriptedOperator
from cuflow.discovery import Discovery, DiscoveryOptions
from cuflow.evidence import RunRecorder
from cuflow.llm import ScriptedLLM
from cuflow.policy import PolicyEngine
from cuflow.redaction import Redactor
from cuflow.replay import ReplayOptions
from cuflow.results import ErrorCode
from cuflow.schema import CapabilityOverride, Risk
from cuflow.surface.web import WebSurface

from .conftest import fixture_capability, tenant_at

LOOKUP_SCRIPT = [
    {"tool": "click", "args": {"ref": '@link "Member Inquiry"', "intent": "Open member inquiry", "risk": "safe"}},
    {"tool": "type_text", "args": {"ref": '@textbox "Member Number"', "text": "10042", "parameter": "member_id",
                                   "parameter_description": "5-digit member number", "intent": "Enter the member", "risk": "safe"}},
    {"tool": "click", "args": {"ref": '@button "Search"', "intent": "Search", "risk": "safe"}},
    {"tool": "click", "args": {"ref": '@link "Select"', "intent": "Open the member", "risk": "safe"}},
    {"tool": "extract", "args": {"ref": '@text "2,345.67"', "output_name": "savings_balance", "output_type": "money",
                                 "description": "Share Savings balance", "row_key": "Share Savings", "intent": "Read balance"}},
    {"tool": "done", "args": {"success_text": "MEMBER DETAIL", "capability_name": "member.get_savings_balance",
                              "capability_description": "Return a member's Share Savings balance.", "summary": "done"}},
]


def _discover(tmp_path: Path, tenant, script, operator=None):
    red = Redactor()
    rec = RunRecorder(tmp_path / "runs", "discovery", red, console=False)
    s = WebSurface(url_allowed=PolicyEngine(tenant.policy).allows_url, on_event=rec.event).start()
    try:
        return Discovery(
            "Look up member 10042 and read their current savings balance", tenant, load_profile("mockcore"), s, rec, red,
            ScriptedLLM(script), DiscoveryOptions(screenshot=False, save_to_catalog=False, operator=operator),
        ).run()
    finally:
        s.close()
        rec.close()


def test_discovery_compiles_a_generalized_artifact_that_replays(app, run_replay, tmp_path) -> None:
    base = app(faults="maintenance_notice")
    out = _discover(tmp_path, tenant_at("harbor", base), LOOKUP_SCRIPT)
    assert out.result.status == "succeeded", out.result.error
    cap = out.capability
    text = dump_yaml(cap)
    assert "10042" not in text and "2,345.67" not in text  # parameterized, no recorded data
    assert "{member_id}" in text and "acknowledge" not in text.lower()  # interstitial not recorded as a step
    assert [p.name for p in cap.inputs] == ["member_id"] and cap.outputs[0].type == "money"
    assert cap.review.status == "draft"
    evidence = Path(out.result.evidence_dir)
    blob = "\n".join(p.read_text() for p in evidence.rglob("*") if p.suffix in {".json", ".jsonl", ".yaml"})
    for value in ("10042", "2,345.67", "2345.67", "SAMPLE, JORDAN", "demo-teller-pass"):
        assert value not in blob
    # replay the compiled draft for a *different* member whose account rows are ordered differently
    r = run_replay(cap, tenant_at("harbor", app()), {"member_id": "10051"}, ReplayOptions(allow_draft=True))
    assert r.status == "succeeded", r.error
    assert r.outputs == {"savings_balance": "15002.11"}
    op = ScriptedOperator("ops", [{"action": "click", "frame": "work", "role": "button", "name": "Not Now"}], "resume")
    # Survey recovery returns the same member URL without the discovery-time token.
    r = run_replay(cap, tenant_at("harbor", app(faults="survey_popup")), {"member_id": "10042"},
                   ReplayOptions(allow_draft=True, operator=op))
    assert r.status == "succeeded" and len(r.interventions) == 1


def test_discovery_detects_two_screen_navigation_cycle(app, tmp_path):
    script = [
        {"tool": "click", "args": {"ref": '@link "New Sub-Account"', "intent": f"open {i}", "risk": "safe"}}
        if i % 2 == 0 else
        {"tool": "click", "args": {"ref": '@link "Member Inquiry"', "intent": f"inquire {i}", "risk": "safe"}}
        for i in range(12)
    ]
    out = _discover(tmp_path, tenant_at("harbor", app()), script)
    assert out.result.status == "failed" and out.result.error.code is ErrorCode.AGENT_STUCK
    assert len(out.trace) < 12


@pytest.mark.parametrize("activation", ["click", "Space", "Enter"])
def test_irreversible_action_in_discovery_needs_human_approval(app, tmp_path, activation) -> None:
    script = [
        {"tool": "click", "args": {"ref": '@link "Member Inquiry"', "intent": "open", "risk": "safe"}},
        {"tool": "type_text", "args": {"ref": '@textbox "Member Number"', "text": "10042", "parameter": "member_id", "intent": "m", "risk": "safe"}},
        {"tool": "click", "args": {"ref": '@button "Search"', "intent": "s", "risk": "safe"}},
        {"tool": "click", "args": {"ref": '@link "Select"', "intent": "o", "risk": "safe"}},
        {"tool": "click", "args": {"ref": '@button "Open Sub-Account"', "intent": "start", "risk": "safe"}},
        {"tool": "select_option", "args": {"ref": '@combobox "Account Type"', "option": "Holiday Club", "parameter": "account_type", "intent": "t", "risk": "safe"}},
        {"tool": "type_text", "args": {"ref": '@textbox "Initial Deposit"', "text": "25.00", "parameter": "initial_deposit", "intent": "a", "risk": "safe"}},
        {"tool": "click", "args": {"ref": '@button "Continue"', "intent": "review", "risk": "safe"}},
        # the model under-declares the risk; policy classifies "Submit" as irreversible anyway
        {"tool": "click", "args": {"ref": '@button "Submit"', "intent": "open the account", "risk": "safe"}},
        {"tool": "done", "args": {"success_text": "SUB-ACCOUNT OPENED", "capability_name": "member.open_sub_account",
                                  "capability_description": "Open a sub-account.", "summary": "ok"}},
    ]
    if activation != "click":
        script[-2] = {"tool": "press_key", "args": {"ref": '@button "Submit"', "key": activation,
                                                    "intent": "open the account", "risk": "safe"}}
    # unattended: blocked, nothing submitted
    out = _discover(tmp_path, tenant_at("harbor", app()), script)
    assert out.result.status == "blocked" and out.result.error.code is ErrorCode.APPROVAL_REQUIRED
    assert not any("/work/subacct/submit" in url for step in out.trace for url in step.after.values())
    # with an operator who approves: the step is executed by automation and recorded as irreversible
    op = ScriptedOperator("supervisor", [], "approve", "member consent on file")
    out = _discover(tmp_path, tenant_at("harbor", app()), script, operator=op)
    assert out.result.status == "succeeded", out.result.error
    submit = next(s for s in out.capability.steps if "submit" in s.id)
    assert submit.risk is Risk.irreversible and submit.idempotent is False
    assert out.capability.side_effects is Risk.irreversible
    assert out.result.interventions[0].kind == "needs_approval"


def test_replay_of_irreversible_capability_is_blocked_without_approval(app, run_replay) -> None:
    cap = fixture_capability()
    cap.steps[-2].risk = Risk.irreversible  # pretend 'open_member' commits something
    r = run_replay(cap, tenant_at("harbor", app()), {"member_id": "10042"})
    assert r.status == "blocked" and r.error.code is ErrorCode.APPROVAL_REQUIRED and r.steps == []


SUMMIT_PATCH = {
    "open_member_inquiry": {"action": {"target": {"locators": [{"by": "role", "role": "link", "name": "Member Lookup"}]}}},
    "enter_member_number": {"action": {"target": {"locators": [{"by": "role", "role": "textbox", "name": "Member #"}]}}},
    "search": {"action": {"target": {"locators": [{"by": "role", "role": "button", "name": "Find"}]}}},
    "read_savings_balance": {"action": {"target": {"locators": [
        {"by": "table_cell", "column": "Current Balance", "row_key": {"column": "Description", "value": "Share Savings"}}]}}},
}


def test_same_artifact_on_another_tenant_degrades_gracefully_and_reports_drift(app, run_replay) -> None:
    r = run_replay(fixture_capability(), tenant_at("summit", app("summit")), {"member_id": "10042"})
    assert r.status == "succeeded" and r.outputs == {"savings_balance": "2345.67"}
    drifted = {d.step_id for d in r.drift}
    assert {"open_member_inquiry", "enter_member_number", "search", "read_savings_balance"} <= drifted


def test_tenant_override_removes_drift(app, run_replay) -> None:
    cap = fixture_capability()
    ov = CapabilityOverride(applies_to="0.x", reason="Summit relabels these controls", steps=SUMMIT_PATCH)
    r = run_replay(cap, tenant_at("summit", app("summit"), overrides={cap.id: ov}), {"member_id": "10042"})
    assert r.status == "succeeded" and r.outputs == {"savings_balance": "2345.67"}
    assert r.drift == []
