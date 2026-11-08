"""Check submission evidence against the shipped artifact and optional pytest report.

This checks local deliverables, not repository visibility or email submission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cuflow.config import dump_yaml, load_capability, load_yaml
from cuflow.results import RunResult
from cuflow.schema import Capability
from scripts.make_evidence import SCENARIOS, validate_scenario, verify_redaction


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--require-live", action="store_true")
    ap.add_argument("--test-report", type=Path)
    ap.add_argument("--write-report", action="store_true")
    args = ap.parse_args()
    cap = load_capability("mockcore.member.get_savings_balance")
    replay_dir = ROOT / "evidence/replays"
    checked = json.loads((replay_dir / "verification.json").read_text())
    assert checked["artifact_sha256"] == hashlib.sha256(dump_yaml(cap).encode()).hexdigest(), "replay evidence is stale"
    for name, *_ in SCENARIOS:
        folder = replay_dir / name
        result = RunResult.model_validate_json((folder / "result.json").read_text())
        assert result.capability == cap.id and result.capability_version == cap.version
        validate_scenario(name, result)
        assert (folder / "events.jsonl").is_file()
        if result.error and result.error.observed:
            obs = result.error.observed
            for file in ([obs.screenshot] if obs.screenshot else []) + obs.dom:
                assert (folder / file).is_file(), f"missing failure attachment: {name}"
    verify_redaction(replay_dir)
    assert (ROOT / "evidence/artifacts/lookup-reviewed.yaml").read_text() == dump_yaml(cap)
    discovery = []
    matching = False
    for result_file in sorted((ROOT / "evidence/discovery").glob("*/result.json")):
        result = json.loads(result_file.read_text())
        run = result_file.parent
        events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
        calls = [e for e in events if e["type"] == "llm_decision"]
        row = {"directory": run.relative_to(ROOT).as_posix(), "status": result["status"], "model_calls": len(calls)}
        if result["status"] == "succeeded":
            draft = Capability.model_validate(load_yaml(run / "artifact.yaml"))
            assert draft.provenance.method == "llm_discovery" and draft.provenance.model != "scripted"
            assert calls and sum((e.get("usage") or {}).get("input_tokens", 0) or 0 for e in calls) > 0
            assert draft.provenance.run_id == result["run_id"]
            row["model"] = draft.provenance.model
            if result["run_id"] == cap.provenance.run_id:
                matching = True
                assert (ROOT / "evidence/artifacts/lookup-discovered.yaml").read_text() == dump_yaml(draft)
        discovery.append(row)
    if args.require_live:
        assert matching, "no successful genuine discovery matching the shipped capability"
    approvals = json.loads((ROOT / "evidence/approval-replays/verification.json").read_text())
    assert [r["cumulative_mock_commits"] for r in approvals] == [0, 0, 1]
    assert [r["code"] for r in approvals] == ["NOT_APPROVED", "APPROVAL_REQUIRED", None]
    for row in approvals:
        result = json.loads((ROOT / "evidence/approval-replays" / row["scenario"] / "result.json").read_text())
        assert result["status"] == row["status"]
        if row["code"]:
            assert result["error"]["code"] == row["code"] and not result["steps"]
        else:
            assert len(result["interventions"]) == 1 and result["interventions"][0]["resolution"] == "approve"
    # Confirm the sensitive local files are excluded; do not print their contents.
    secret_values = [v for k, v in os.environ.items() if k.endswith("API_KEY") and len(v) > 10]
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip().endswith("API_KEY") and len(value.strip()) > 10:
                secret_values.append(value.strip().strip('"').strip("'"))
    for file in (ROOT / "evidence").rglob("*"):
        if file.suffix in {".json", ".jsonl", ".yaml", ".html", ".md", ".diff"}:
            text = file.read_text()
            assert not any(value in text for value in secret_values), "credential found in evidence"
    for md in [ROOT / "README.md", ROOT / "REPORT.md", ROOT / "evidence/README.md", replay_dir / "README.md"]:
        for target in re.findall(r"\]\(([^)]+)\)", md.read_text()):
            if "://" not in target and not target.startswith("#"):
                destination = (md.parent / target.split("#")[0]).resolve()
                if args.write_report and destination == ROOT / "evidence/verification.json":
                    continue  # this successful check produces the linked report below
                assert destination.exists(), f"broken link in {md.name}: {target}"
    report = {"local_deliverables_verified": True, "genuine_lookup_verified": matching,
              "capability": f"{cap.id}@{cap.version}", "replay_scenarios": len(SCENARIOS),
              "approval_replays": len(approvals), "discovery_attempts": discovery}
    if args.test_report:
        suites = ET.parse(args.test_report).getroot().iter("testsuite")
        totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
        for suite in suites:
            for key in totals:
                totals[key] += int(suite.attrib.get(key, 0))
        assert totals["tests"] > 0 and totals["failures"] == totals["errors"] == totals["skipped"] == 0
        report["pytest"] = totals
    if args.write_report:
        (ROOT / "evidence/verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
