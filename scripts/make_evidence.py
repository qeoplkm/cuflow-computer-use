"""Reproduce the replay evidence in evidence/replays/ (no LLM involved).

Starts isolated MockCore instances on ephemeral ports and checks every scenario's
expected result before replacing the published evidence. No model calls are made.

    .venv/bin/python scripts/make_evidence.py [capability-ref]
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cuflow.cli import tool_definition  # noqa: E402
from cuflow.config import dump_yaml, load_capability, load_dotenv, load_tenant  # noqa: E402
from cuflow.control import ScriptedOperator  # noqa: E402
from cuflow.replay import ReplayOptions, replay  # noqa: E402
from mockcore.server import serve_in_thread  # noqa: E402
from mockcore.data import MEMBERS  # noqa: E402
from cuflow.schema import TextCondition  # noqa: E402

OUT = ROOT / "evidence" / "replays"
def expected_balance(member_id: str) -> str | None:
    from mockcore.data import MEMBERS

    m = MEMBERS.get(member_id)
    acct = next((a for a in m.accounts if a.description == "Share Savings"), None) if m else None
    return f"{Decimal(acct.balance.replace(',', '')):.2f}" if acct else None


SURVEY_OPERATOR = ScriptedOperator(
    "ops-anna",
    [{"action": "click", "frame": "work", "role": "button", "name": "Not Now"}],
    "resume",
    "Unknown 'member experience' prompt; chose Not Now (sending a survey is not part of this task).",
)

# (name, tenant, faults, params, options, what it demonstrates)
SCENARIOS = [
    ("01-success", "harbor", "", {"member_id": "10042"}, {}, "happy path; typed output"),
    ("02-success-other-member", "harbor", "", {"member_id": "10051"}, {}, "different member, accounts in a different row order"),
    ("03-business-not-found", "harbor", "", {"member_id": "99999"}, {}, "'no such member' is a business outcome, not a crash"),
    ("04-business-restricted", "harbor", "", {"member_id": "10077"}, {}, "permission denial on a restricted account"),
    ("05-invalid-input", "harbor", "", {"member_id": "12AB"}, {}, "bad input rejected by the contract before touching the UI"),
    ("06-recovered-interstitial-and-alert", "harbor", "maintenance_notice,alert_on_home", {"member_id": "10042"}, {}, "known interstitial dismissed + native alert accepted"),
    ("07-recovered-session-expiry", "harbor", "expire_session_after=6", {"member_id": "10042"}, {}, "session expired mid-flow -> re-login -> restart"),
    ("08-recovered-slow-load", "harbor", "slow_member_ms=4000", {"member_id": "10042"}, {}, "4s server delay absorbed by settle-wait (no sleeps)"),
    ("09-recovered-transient-app-error", "harbor", "member_errors=1", {"member_id": "10042"}, {}, "one HTTP 500 -> bounded restart -> success"),
    ("10-failure-app-error", "harbor", "member_errors=-1", {"member_id": "10042"}, {}, "persistent HTTP 500 -> hard failure with screenshot + DOM"),
    ("11-failure-unrecognized-state", "harbor", "survey_popup", {"member_id": "10042"}, {}, "unknown interstitial, unattended -> UNRECOGNIZED_STATE"),
    ("12-hitl-unrecognized-state", "harbor", "survey_popup", {"member_id": "10042"}, {"operator": SURVEY_OPERATOR}, "same, with an operator: takes over the live session, hands back"),
    ("13-tenant-summit-no-override", "summit", "", {"member_id": "10042"}, {"_no_override": True}, "Harbor-recorded artifact on Summit: fallbacks + drift report"),
    ("14-tenant-summit-with-override", "summit", "", {"member_id": "10042"}, {}, "with Summit's reviewed patch: no drift"),
    ("15-failure-private-detail", "harbor", "", {"member_id": "10042"}, {"_bad_checkpoint": True}, "failure on a PII-bearing page; DOM redaction checked"),
]

EXPECTED = {
    "03": ("business_outcome", "MEMBER_NOT_FOUND"),
    "04": ("business_outcome", "ACCOUNT_RESTRICTED"),
    "05": ("failed", "INVALID_INPUT"),
    "10": ("failed", "APP_ERROR"),
    "11": ("failed", "UNRECOGNIZED_STATE"),
    "15": ("failed", "CHECKPOINT_FAILED"),
}


def validate_scenario(name, result):
    status, code = EXPECTED.get(name[:2], ("succeeded", None))
    assert result.status == status, f"{name}: expected {status}, got {result.status}"
    actual = result.outcome.code if result.outcome else result.error.code.value if result.error else None
    assert actual == code, f"{name}: unexpected outcome/error {actual}"
    if name.startswith("13-"):
        assert len(result.drift) == 4, "expected four tenant label fallbacks"
    elif status == "succeeded":
        assert not result.drift, f"{name}: unexpected drift"
    if name.startswith("14-"):
        assert not result.drift, "reviewed tenant override must eliminate drift"
    if name.startswith("12-"):
        assert len(result.interventions) == 1 and result.interventions[0].resolution == "resume"
    required = {"06": "maintenance_notice", "07": "session_expired", "09": "core_system_error"}.get(name[:2])
    if required:
        assert required in {r.outcome_id for r in result.recoveries}, f"{name}: recovery not exercised"


def verify_redaction(directory):
    text = "\n".join(p.read_text() for p in directory.rglob("*") if p.suffix in {".json", ".jsonl", ".html"})
    values = [os.environ.get("MOCKCORE_PASSWORD", "demo-teller-pass")]
    for member in MEMBERS.values():
        values.extend([member.number, member.name, member.address, member.phone])
        for account in member.accounts:
            values.extend([account.balance, account.available])
    for value in values:
        # Whole-token match: the synthetic balance "0.00" must not match inside a
        # timestamp such as "05:52:50.001" (a substring check made this flaky).
        hit = re.search(rf"(?<![\w.]){re.escape(value)}(?!\w)", text)
        assert hit is None, f"raw synthetic sensitive value found in evidence: {text[max(0, hit.start() - 60):hit.end() + 20]!r}"


def main() -> None:
    load_dotenv()
    os.environ.setdefault("MOCKCORE_PASSWORD", "demo-teller-pass")
    for tenant in ("HARBOR", "SUMMIT"):
        os.environ.setdefault(f"CUFLOW_SECRET_{tenant}_OPERATOR_USERNAME", "teller01")
        os.environ.setdefault(f"CUFLOW_SECRET_{tenant}_OPERATOR_PASSWORD", os.environ["MOCKCORE_PASSWORD"])
    ref = sys.argv[1] if len(sys.argv) > 1 else "mockcore.member.get_savings_balance"
    cap = load_capability(ref)
    print(f"capability {cap.id}@{cap.version} ({cap.review.status})")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="replay-evidence-"))
    rows = []
    verified = []
    for name, tenant, faults, params, opts, what in SCENARIOS:
        opts = dict(opts)
        tenant_cfg = load_tenant(tenant)
        if opts.pop("_no_override", False):
            tenant_cfg.overrides = {}
        scenario_cap = cap.model_copy(deep=True)
        if opts.pop("_bad_checkpoint", False):
            scenario_cap.success = TextCondition(pattern="INTENTIONALLY MISSING CHECKPOINT", frame="work")
        print(f"- {name}: {what}")
        server, base = serve_in_thread(0, tenant, faults)
        tenant_cfg.base_url = base
        tenant_cfg.policy.allowed_origins = [base]
        try:
            r = replay(scenario_cap, tenant, params, ReplayOptions(**opts), runs_dir=staging, console=False, tenant=tenant_cfg)
        finally:
            server.shutdown()
            server.server_close()
        validate_scenario(name, r)
        src = Path(r.evidence_dir)
        dst = staging / name
        src.rename(dst)
        data = json.loads((dst / "result.json").read_text())
        data["evidence_dir"] = f"evidence/replays/{name}"
        (dst / "result.json").write_text(json.dumps(data, indent=2) + "\n")
        if r.status == "succeeded":
            # don't print balances into the evidence index; check them against the synthetic record
            want = expected_balance(params["member_id"])
            got = r.outputs.get("savings_balance")
            assert got == want, f"{name}: output did not match the synthetic source record"
            detail = "savings_balance matches the synthetic record"
        else:
            detail = r.outcome.code if r.outcome else f"{r.error.code.value} @ {r.error.step_id}"
        rows.append(
            f"| [{name}]({name}/) | {tenant} | `{faults or '-'}` | **{r.status}** | {detail} | "
            f"{', '.join(f'{x.outcome_id}:{x.strategy}' for x in r.recoveries) or '-'} | "
            f"{len(r.drift)} | {', '.join(f'{i.kind}->{i.resolution}' for i in r.interventions) or '-'} | {r.duration_ms} | {what} |"
        )
        print(f"    {r.status} {detail}")
        verified.append({"scenario": name, "status": r.status, "checks_passed": True,
                         "output_verified": r.status == "succeeded", "drift": len(r.drift)})
    verify_redaction(staging)
    (staging / "catalog.json").write_text(json.dumps([tool_definition(cap)], indent=2))
    (staging / "verification.json").write_text(json.dumps({
        "capability": cap.id, "version": cap.version,
        "artifact_sha256": hashlib.sha256(dump_yaml(cap).encode()).hexdigest(),
        "scenarios": verified, "text_redaction_checked": True,
    }, indent=2) + "\n")
    index = [
        f"# Replay evidence — `{cap.id}@{cap.version}`",
        f"Artifact provenance: `{cap.provenance.method}`; review: `{cap.review.status}`.",
        "",
        "Generated by `scripts/make_evidence.py`. Every run is a deterministic replay (no LLM). Each folder holds",
        "`events.jsonl` (structured, redacted log), `result.json` (the result contract; outputs redacted on disk),",
        "`screens/` (screenshots with PII-labelled values blacked out) and, on failure, `failure/` (screenshot +",
        "per-frame DOM, redacted). Note: on-disk logs redact outputs; the caller receives real values in memory.",
        "",
        "| scenario | tenant | injected condition | status | outputs / outcome / error | recoveries | drift | interventions | ms | shows |",
        "|---|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        "`catalog.json` is the capability rendered as an agent-callable tool definition (`cuflow catalog`).",
    ]
    (staging / "README.md").write_text("\n".join(index) + "\n")
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(staging, OUT)
    shutil.rmtree(staging)
    print(f"\nwrote {OUT.relative_to(ROOT)}/README.md")


if __name__ == "__main__":
    main()
