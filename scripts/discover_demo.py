"""Run genuine model discovery against an isolated local MockCore instance.

No scripted-model option: failed provider calls remain failed evidence.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cuflow.config import load_dotenv, load_tenant
from cuflow.control import ScriptedOperator
from cuflow.discovery import DiscoveryOptions, discover
from cuflow.llm import make_llm
from mockcore.server import serve_in_thread

GOALS = {
    "lookup": "Look up member 10042 and read their current Share Savings balance. Use member_id as the input name, savings_balance as the output name, and member.get_savings_balance as the capability name.",
    "open-account": "Look up member 10042 first, then from that member's record open a Holiday Club sub-account with an initial deposit of 25.00. Submit after supervisor approval and reach SUB-ACCOUNT OPENED. Use member_id, account_type and initial_deposit as input names, and member.open_sub_account as the capability name.",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", choices=list(GOALS), default="lookup")
    ap.add_argument("--provider", choices=["gemini", "claude"], default="gemini")
    ap.add_argument("--model")
    ap.add_argument("--prefer-dotenv", action="store_true", help="explicitly prefer the model key in .env over the shell")
    args = ap.parse_args()
    if args.prefer_dotenv:
        # Read only the selected provider's key. Never print or persist it.
        key = "GOOGLE_API_KEY" if args.provider == "gemini" else "ANTHROPIC_API_KEY"
        path = ROOT / ".env"
        for line in path.read_text().splitlines() if path.exists() else []:
            k, sep, value = line.partition("=")
            if sep and k.strip() == key and value.strip().strip('"').strip("'"):
                os.environ[key] = value.strip().strip('"').strip("'")
    load_dotenv()
    os.environ.setdefault("MOCKCORE_PASSWORD", "demo-teller-pass")
    os.environ.setdefault("CUFLOW_SECRET_HARBOR_OPERATOR_USERNAME", "teller01")
    os.environ.setdefault("CUFLOW_SECRET_HARBOR_OPERATOR_PASSWORD", os.environ["MOCKCORE_PASSWORD"])
    llm = make_llm(args.provider, args.model)
    server, base = serve_in_thread(0, "harbor")
    tenant = load_tenant("harbor")
    tenant.base_url = base
    tenant.policy.allowed_origins = [base]
    operator = None
    if args.scenario == "open-account":
        operator = ScriptedOperator("demo-supervisor", [], "approve", "Simulated approval for synthetic local demo data only.")
    try:
        out = discover(GOALS[args.scenario], "harbor", llm,
                       DiscoveryOptions(save_to_catalog=False, operator=operator),
                       tenant=tenant, console=False)
    finally:
        server.shutdown()
        server.server_close()
    source = Path(out.result.evidence_dir)
    parent = ROOT / "evidence" / "discovery"
    parent.mkdir(parents=True, exist_ok=True)
    # Immutable attempts preserve both failed and successful model runs honestly.
    dest = parent / f"{args.scenario}-{out.result.run_id}"
    shutil.copytree(source, dest)
    data = json.loads((dest / "result.json").read_text())
    data["evidence_dir"] = dest.relative_to(ROOT).as_posix()
    (dest / "result.json").write_text(json.dumps(data, indent=2) + "\n")
    print(json.dumps({"status": out.result.status, "error": out.result.error.code.value if out.result.error else None,
                      "evidence": dest.relative_to(ROOT).as_posix(), "model": llm.model}, indent=2))
    return 0 if out.result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
