"""Integration: deterministic replay against the hostile mock app, one runtime condition each."""

from __future__ import annotations

import json
from pathlib import Path

from cuflow.control import ScriptedOperator
from cuflow.replay import ReplayOptions
from cuflow.results import ErrorCode
from cuflow.schema import TextCondition

from .conftest import fixture_capability, tenant_at


def test_success_returns_typed_output(app, run_replay) -> None:
    r = run_replay(fixture_capability(), tenant_at("harbor", app()), {"member_id": "10042"})
    assert r.status == "succeeded", r.error
    assert r.outputs == {"savings_balance": "2345.67"}
    assert not r.drift and not r.recoveries


def test_row_order_does_not_matter(app, run_replay) -> None:
    # member 10051 lists Holiday Club first; a positional locator would read 310.00
    r = run_replay(fixture_capability(), tenant_at("harbor", app()), {"member_id": "10051"})
    assert r.outputs == {"savings_balance": "15002.11"}


def test_not_found_is_a_business_outcome(app, run_replay) -> None:
    r = run_replay(fixture_capability(), tenant_at("harbor", app()), {"member_id": "99999"})
    assert r.status == "business_outcome"
    assert r.outcome.code == "MEMBER_NOT_FOUND" and r.error is None


def test_permission_denial_is_a_business_outcome(app, run_replay) -> None:
    r = run_replay(fixture_capability(), tenant_at("harbor", app()), {"member_id": "10077"})
    assert r.status == "business_outcome" and r.outcome.code == "ACCOUNT_RESTRICTED"


def test_bad_input_fails_before_touching_the_ui(app, run_replay) -> None:
    r = run_replay(fixture_capability(), tenant_at("harbor", app()), {"member_id": "12AB"})
    assert r.status == "failed" and r.error.code is ErrorCode.INVALID_INPUT
    assert r.steps == []


def test_draft_artifacts_are_not_replayed_unattended(app, run_replay) -> None:
    r = run_replay(fixture_capability(status="draft"), tenant_at("harbor", app()), {"member_id": "10042"})
    assert r.status == "blocked" and r.error.code is ErrorCode.NOT_APPROVED


def test_interstitial_and_native_alert_are_recovered(app, run_replay) -> None:
    r = run_replay(fixture_capability(), tenant_at("harbor", app(faults="maintenance_notice,alert_on_home")), {"member_id": "10042"})
    assert r.status == "succeeded"
    kinds = {x.outcome_id for x in r.recoveries}
    assert {"maintenance_notice", "native_dialog"} <= kinds


def test_session_expiry_triggers_relogin_and_restart(app, run_replay) -> None:
    r = run_replay(fixture_capability(), tenant_at("harbor", app(faults="expire_session_after=6")), {"member_id": "10042"})
    assert r.status == "succeeded"
    assert any(x.strategy == "relogin_restart" for x in r.recoveries)


def test_slow_load_is_absorbed_by_settle_wait(app, run_replay) -> None:
    r = run_replay(fixture_capability(), tenant_at("harbor", app(faults="slow_member_ms=3000")), {"member_id": "10042"})
    assert r.status == "succeeded"
    assert next(s for s in r.steps if s.step_id == "open_member").duration_ms >= 3000


def test_transient_app_error_restarts_once(app, run_replay) -> None:
    r = run_replay(fixture_capability(), tenant_at("harbor", app(faults="member_errors=1")), {"member_id": "10042"})
    assert r.status == "succeeded"
    assert [x.outcome_id for x in r.recoveries] == ["core_system_error"]


def test_persistent_app_error_is_a_debuggable_hard_failure(app, run_replay) -> None:
    r = run_replay(fixture_capability(), tenant_at("harbor", app(faults="member_errors=-1")), {"member_id": "10042"})
    assert r.status == "failed" and r.error.code is ErrorCode.APP_ERROR
    assert r.error.step_id == "open_member" and r.error.retryable
    obs = r.error.observed
    assert any("MCX-500" in a for a in obs.alerts)
    assert (Path(r.evidence_dir) / obs.screenshot).exists() and obs.dom


def test_unknown_state_fails_unattended(app, run_replay) -> None:
    r = run_replay(fixture_capability(), tenant_at("harbor", app(faults="survey_popup")), {"member_id": "10042"})
    assert r.status == "failed" and r.error.code is ErrorCode.UNRECOGNIZED_STATE
    assert "location" in (r.error.expected or "")


def test_unknown_state_human_takes_over_same_session_and_hands_back(app, run_replay) -> None:
    op = ScriptedOperator("ops", [{"action": "click", "frame": "work", "role": "button", "name": "Not Now"}], "resume")
    r = run_replay(fixture_capability(), tenant_at("harbor", app(faults="survey_popup")), {"member_id": "10042"}, ReplayOptions(operator=op))
    assert r.status == "succeeded" and r.outputs["savings_balance"] == "2345.67"
    (iv,) = r.interventions
    assert iv.kind == "unrecognized_state" and iv.resolution == "resume" and iv.operator == "simulated:ops"
    assert [h.target for h in iv.human_actions] == ['button "Not Now"']
    events = [json.loads(l)["type"] for l in (Path(r.evidence_dir) / "events.jsonl").read_text().splitlines()]
    assert events.count("control_transfer") == 3  # automation -> awaiting -> human -> automation


def test_policy_blocks_navigation_outside_allowlist(app, run_replay) -> None:
    base = app()
    t = tenant_at("harbor", base)
    t.policy.denied_paths.append("/work/member")
    r = run_replay(fixture_capability(), t, {"member_id": "10042"})
    assert r.status == "failed" and r.error.code is ErrorCode.POLICY_VIOLATION


def test_logs_and_results_are_redacted(app, run_replay) -> None:
    r = run_replay(fixture_capability(), tenant_at("harbor", app()), {"member_id": "10042"})
    d = Path(r.evidence_dir)
    blob = (d / "events.jsonl").read_text() + (d / "result.json").read_text()
    for secret in ("10042", "2,345.67", "2345.67", "demo-teller-pass", "SAMPLE, JORDAN"):
        assert secret not in blob, secret
    assert r.outputs["savings_balance"] == "2345.67"  # the caller still gets the value


def test_checkpoint_failure_redacts_newly_visible_pii_in_dom(app, run_replay) -> None:
    cap = fixture_capability()
    cap.success = TextCondition(pattern="MISSING SUCCESS HEADING", frame="work")
    r = run_replay(cap, tenant_at("harbor", app()), {"member_id": "10042"})
    assert r.error.code is ErrorCode.CHECKPOINT_FAILED
    assert r.error.observed.dom
    files = Path(r.evidence_dir)
    blob = "\n".join(p.read_text() for p in files.rglob("*") if p.suffix in {".html", ".json", ".jsonl"})
    for value in ("SAMPLE, JORDAN", "10042", "demo-teller-pass", "2,345.67", "2345.67"):
        assert value not in blob
    assert "MEMBER DETAIL" in blob  # useful failure context survives
