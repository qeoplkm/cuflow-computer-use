"""Security and output-integrity regressions from the submission review."""

import pytest

from cuflow.control import summarize_human_event
from cuflow.policy import PolicyEngine
from cuflow.redaction import Redactor
from cuflow.replay import parse_output
from cuflow.schema import OutputSpec, Policy, Risk


@pytest.mark.parametrize("key", ["Enter", "Space", " ", "Control+Enter"])
def test_keyboard_submission_requires_approval(key):
    policy = PolicyEngine(Policy(allowed_origins=["http://local"], allowed_paths=["/*"]))
    decision = policy.check("press", element={"name": "Submit", "form_submit_name": "Submit"}, key=key)
    assert not decision.allowed and decision.needs_approval
    assert decision.risk is Risk.irreversible


@pytest.mark.parametrize("key", ["Enter", "Space", "Tab"])
def test_implicit_focus_is_rejected_even_with_approval(key):
    policy = PolicyEngine(Policy(allowed_origins=["http://local"], allowed_paths=["/*"]))
    assert not policy.check("press", key=key, approved=True).allowed


def test_block_policy_cannot_be_overridden_by_caller_approval():
    policy = PolicyEngine(Policy(allowed_origins=["http://local"], allowed_paths=["/*"], on_irreversible="block"))
    assert not policy.check("click", element={"name": "Submit"}, approved=True).allowed


@pytest.mark.parametrize("raw", ["ERROR E1001", "balance unavailable (E500)", "USD 1e3", "1,23.45", "1.234", "NaN", "Infinity", "(--5)", "1.00 / 2.00", ""])
def test_money_rejects_malformed_or_error_text(raw):
    with pytest.raises(ValueError):
        parse_output(raw, OutputSpec(name="balance", type="money", description="Balance"))


@pytest.mark.parametrize("raw,expected", [("2,345.67", "2345.67"), ("$2,345.67", "2345.67"), ("(2,345.67)", "-2345.67"), ("2,345.67-", "-2345.67"), ("-2.50", "-2.50"), ("0", "0.00"), ("15002.1", "15002.10")])
def test_money_accepts_supported_display_formats(raw, expected):
    assert parse_output(raw, OutputSpec(name="balance", type="money", description="Balance")) == expected


def test_numeric_outputs_do_not_silently_strip_errors():
    for kind in ("decimal", "integer"):
        with pytest.raises(ValueError):
            parse_output("E500", OutputSpec(name="value", type=kind, description="Value"))


def test_operator_input_is_redacted_before_event_is_written():
    red = Redactor()
    event = {"type": "change", "info": {"name": "Last Name", "value": "EXAMPLE PRIVATE NAME"}}
    assert "EXAMPLE PRIVATE NAME" not in str(summarize_human_event(event, red))


def test_redacts_html_escaped_names_and_url_tokens():
    red = Redactor()
    red.add("O'EXAMPLE & FAMILY", "pii")
    text = red.text("O&#x27;EXAMPLE &amp; FAMILY /work?m=10042&amp;t=abc123&token=secretABC")
    assert "FAMILY" not in text and "abc123" not in text and "secretABC" not in text


def test_location_ignores_only_explicit_tokens_not_member_identity():
    from types import SimpleNamespace
    from cuflow.conditions import evaluate
    from cuflow.compiler import location_condition
    cond = location_condition("/work/member?m=10042&t=abc", "work", {"10042": "member_id"})
    for url, expected in [("/work/member?m=10042", True), ("/work/member?t=new&m=10042", True),
                          ("/work/member?m=100420", False), ("/work/member?m=10051", False)]:
        assert evaluate(cond, SimpleNamespace(location=lambda _: url), {"member_id": "10042"}) is expected


def test_event_payload_cannot_overwrite_event_type(tmp_path):
    import json
    from cuflow.evidence import RunRecorder
    rec = RunRecorder(tmp_path, "test", Redactor(), console=False)
    rec.event("human_action", type="change", seq=999)
    rec.close()
    event = json.loads((rec.dir / "events.jsonl").read_text())
    assert event["type"] == "human_action" and event["detail_type"] == "change" and event["seq"] == 1
