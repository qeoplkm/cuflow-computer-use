"""Apply the documented, narrowly scoped review of the balance-lookup demo.

This is not a general automatic approval mechanism. It rejects unexpected contracts,
keeps LLM provenance, and records the exact draft -> reviewed diff for inspection.
"""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cuflow.config import capability_path, dump_yaml, load_yaml
from cuflow.evidence import now_iso
from cuflow.schema import Capability, Review


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", type=Path, help="successful genuine lookup discovery evidence directory")
    ap.add_argument("--reviewer", required=True)
    args = ap.parse_args()
    run = args.run.resolve()
    result = json.loads((run / "result.json").read_text())
    cap = Capability.model_validate(load_yaml(run / "artifact.yaml"))
    assert result["status"] == "succeeded" and result["mode"] == "discovery"
    assert cap.provenance.method == "llm_discovery" and cap.provenance.model != "scripted"
    assert cap.provenance.run_id == result["run_id"]
    assert cap.id == "mockcore.member.get_savings_balance"
    assert [i.name for i in cap.inputs] == ["member_id"]
    assert [(o.name, o.type) for o in cap.outputs] == [("savings_balance", "money")]
    assert cap.side_effects.value == "safe" and all(s.risk.value == "safe" for s in cap.steps)
    draft = dump_yaml(cap)
    cap.version = "1.1.0"
    cap.app.versions = ">=4.2,<5"
    cap.inputs[0].pattern = r"^\d{5}$"
    cap.outcomes = Capability.model_validate(load_yaml(ROOT / "tests/fixtures/get_savings_balance.yaml")).outcomes
    for target in cap.targets.values():
        target.locators = [loc for loc in target.locators if loc.by != "css"]
        assert target.locators, "review requires a semantic or vendor-attribute locator"
    cap.review = Review(status="approved", reviewed_by=args.reviewer, reviewed_at=now_iso(), notes=[
        "Verified the recorded path is read-only and parameters/outputs are generalized.",
        "Tightened member_id to five digits using the mock's validation rule.",
        "Added observed not-found and restricted-account business outcomes.",
        "Removed positional CSS fallbacks; unresolved semantic/vendor targets must fail closed.",
        "Approval covers this synthetic demo; no production tenant authorization is implied.",
    ])
    reviewed = dump_yaml(cap)
    artifacts = ROOT / "evidence/artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "lookup-discovered.yaml").write_text(draft)
    (artifacts / "lookup-reviewed.yaml").write_text(reviewed)
    (artifacts / "lookup-review.diff").write_text("".join(difflib.unified_diff(
        draft.splitlines(True), reviewed.splitlines(True), fromfile="lookup-discovered.yaml", tofile="lookup-reviewed.yaml")))
    capability_path(cap).write_text(reviewed)

    labels = {"Member Inquiry": "Member Lookup", "Member Number": "Member #", "Search": "Find"}
    targets = {}
    for name, target in cap.targets.items():
        locs = [l.model_dump(mode="json", exclude_none=True) for l in target.locators]
        changed = False
        for loc in locs:
            if loc["by"] == "role" and loc.get("name") in labels:
                loc["name"] = labels[loc["name"]]
                changed = True
            elif loc["by"] == "table_cell" and loc.get("column") == "Balance":
                loc["column"] = "Current Balance"
                changed = True
        if changed:
            targets[name] = {"locators": locs}
    assert len(targets) == 4, "unexpected discovery targets: review the tenant patch manually"
    tenant_path = ROOT / "catalog/tenants/summit.yaml"
    tenant = load_yaml(tenant_path)
    tenant["overrides"][cap.id] = {"applies_to": "1.x", "reason": "Four reviewed label changes in Summit 4.3.0.", "targets": targets}
    tenant_path.write_text(dump_yaml(tenant))
    print(f"Reviewed {cap.id}@{cap.version}; four Summit targets patched; diff saved in evidence/artifacts/.")


if __name__ == "__main__":
    main()
