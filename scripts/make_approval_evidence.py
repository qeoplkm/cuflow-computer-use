"""Replay a genuine open-account draft and check both gates and one committed action."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cuflow.config import capability_path, dump_yaml, load_dotenv, load_tenant, load_yaml
from cuflow.control import ScriptedOperator
from cuflow.replay import ReplayOptions, replay
from cuflow.schema import Capability
from mockcore.server import serve_in_thread


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", type=Path)
    args = ap.parse_args()
    load_dotenv()
    os.environ.setdefault("MOCKCORE_PASSWORD", "demo-teller-pass")
    os.environ.setdefault("CUFLOW_SECRET_HARBOR_OPERATOR_USERNAME", "teller01")
    os.environ.setdefault("CUFLOW_SECRET_HARBOR_OPERATOR_PASSWORD", os.environ["MOCKCORE_PASSWORD"])
    cap = Capability.model_validate(load_yaml(args.run / "artifact.yaml"))
    result = json.loads((args.run / "result.json").read_text())
    assert result["status"] == "succeeded" and cap.provenance.run_id == result["run_id"]
    assert cap.provenance.method == "llm_discovery" and cap.provenance.model != "scripted"
    assert cap.id == "mockcore.member.open_sub_account" and cap.side_effects.value == "irreversible"
    assert cap.review.status == "draft"
    capability_path(cap).write_text(dump_yaml(cap))
    (ROOT / "evidence/artifacts/open-account-discovered.yaml").write_text(dump_yaml(cap))
    server, base = serve_in_thread(0, "harbor")
    tenant = load_tenant("harbor")
    tenant.base_url = base
    tenant.policy.allowed_origins = [base]
    state = server.RequestHandlerClass.app
    initial_count = state.next_suffix
    out = ROOT / "evidence/approval-replays"
    out.mkdir(exist_ok=True)
    cases = [
        ("01-draft-blocked", ReplayOptions(), "blocked", "NOT_APPROVED", 0),
        ("02-approval-required", ReplayOptions(allow_draft=True), "blocked", "APPROVAL_REQUIRED", 0),
        ("03-operator-approved", ReplayOptions(allow_draft=True, operator=ScriptedOperator(
            "demo-supervisor", [], "approve", "Simulated approval for synthetic local demo data only.")), "succeeded", None, 1),
    ]
    checks = []
    try:
        for name, opts, status, code, commits in cases:
            r = replay(cap, "harbor", {"member_id": "10051", "account_type": "Holiday Club", "initial_deposit": "25.00"},
                       opts, tenant=tenant, runs_dir=out, console=False)
            assert r.status == status and (r.error.code.value if r.error else None) == code
            assert state.next_suffix - initial_count == commits
            if commits:
                assert len(r.interventions) == 1 and r.interventions[0].resolution == "approve"
            else:
                assert not r.steps
            dest = out / name
            if dest.exists():
                shutil.rmtree(dest)
            Path(r.evidence_dir).rename(dest)
            data = json.loads((dest / "result.json").read_text())
            data["evidence_dir"] = dest.relative_to(ROOT).as_posix()
            (dest / "result.json").write_text(json.dumps(data, indent=2) + "\n")
            checks.append({"scenario": name, "status": status, "code": code, "cumulative_mock_commits": commits})
    finally:
        server.shutdown()
        server.server_close()
    (out / "verification.json").write_text(json.dumps(checks, indent=2) + "\n")
    print(json.dumps(checks, indent=2))


if __name__ == "__main__":
    main()
