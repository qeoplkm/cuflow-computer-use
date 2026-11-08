"""Exercise the shipped catalog, not just the hand-written test fixture."""

import json
from types import SimpleNamespace

import pytest

from cuflow.config import load_capability, load_tenant
from scripts.make_evidence import validate_scenario
from .conftest import tenant_at


def test_shipped_capability_and_summit_override_replay_without_drift(app, run_replay):
    cap = load_capability("mockcore.member.get_savings_balance")
    assert cap.review.status == "approved"
    summit = load_tenant("summit")
    assert summit.overrides.get(cap.id)
    result = run_replay(cap, tenant_at("summit", app("summit"), overrides=summit.overrides), {"member_id": "10051"})
    assert result.status == "succeeded"
    assert result.outputs == {"savings_balance": "15002.11"}
    assert result.drift == []


def test_evidence_generation_rejects_false_zero_drift_claim():
    result = SimpleNamespace(status="succeeded", outcome=None, error=None, drift=[object()] * 4)
    with pytest.raises(AssertionError):
        validate_scenario("14-tenant-summit-with-override", result)


def test_evidence_generation_rejects_wrong_business_outcome():
    result = SimpleNamespace(status="succeeded", outcome=None, error=None)
    with pytest.raises(AssertionError):
        validate_scenario("03-business-not-found", result)
