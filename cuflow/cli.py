"""cuflow command line.

    cuflow discover  --tenant harbor --goal "look up member 10042 and read their savings balance"
    cuflow replay    mockcore.member.get_savings_balance --tenant harbor -p member_id=10042
    cuflow catalog   --tenant harbor                 # capabilities as agent-callable tools
    cuflow invoke    mockcore.member.get_savings_balance --tenant harbor --args '{"member_id": "10042"}'
    cuflow approve   mockcore.member.get_savings_balance@1.1.0 --reviewer alice
    cuflow schema                                    # JSON Schema of the artifact format
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import (
    CATALOG,
    ROOT,
    capability_path,
    dump_yaml,
    effective_capability,
    list_capabilities,
    load_capability,
    load_dotenv,
    load_tenant,
    version_satisfies,
)
from .control import NoOperator, Operator, ScriptedOperator, TerminalOperator
from .evidence import now_iso
from .schema import Capability


def _operator(spec: str | None) -> Operator | None:
    if not spec or spec == "none":
        return NoOperator()
    if spec == "terminal":
        return TerminalOperator()
    if spec.startswith("scripted:"):
        cfg = json.loads(Path(spec.split(":", 1)[1]).read_text())
        return ScriptedOperator(cfg["operator_id"], cfg.get("actions", []), cfg["decision"], cfg.get("note"))
    raise SystemExit(f"unknown operator {spec!r} (none | terminal | scripted:<file.json>)")


def _params(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k.strip()] = v
    return out


def _print_result(result: Any) -> None:
    print(json.dumps(result.model_dump(mode="json", exclude_none=True), indent=2, ensure_ascii=False))


def cmd_discover(a: argparse.Namespace) -> int:
    from .discovery import DiscoveryOptions, discover
    from .llm import make_llm

    out = discover(
        a.goal,
        a.tenant,
        make_llm(a.provider, a.model),
        DiscoveryOptions(
            max_steps=a.max_steps, screenshot=not a.no_screenshot, operator=_operator(a.operator), save_to_catalog=not a.dry_run
        ),
        headless=not a.headed,
    )
    _print_result(out.result)
    if out.artifact_path:
        print(f"\nartifact: {out.artifact_path.relative_to(ROOT)}  (review.status=draft)", file=sys.stderr)
    return 0 if out.result.status == "succeeded" else 1


def cmd_replay(a: argparse.Namespace) -> int:
    from .replay import ReplayOptions, replay

    result = replay(
        a.capability,
        a.tenant,
        _params(a.param),
        ReplayOptions(allow_draft=a.allow_draft, approve_irreversible=a.approve, operator=_operator(a.operator)),
        headless=not a.headed,
    )
    _print_result(result)
    return {"succeeded": 0, "business_outcome": 0, "blocked": 2}.get(result.status, 1)


def tool_definition(cap: Capability) -> dict[str, Any]:
    """A capability as an agent-callable tool (name, description, JSON Schema in/out)."""
    props, required = {}, []
    for p in cap.inputs:
        s: dict[str, Any] = {"type": "string" if p.type in ("string", "decimal") else p.type, "description": p.description}
        if p.pattern:
            s["pattern"] = p.pattern
        if p.enum:
            s["enum"] = p.enum
        props[p.name] = s
        if p.required:
            required.append(p.name)
    outs = {o.name: {"type": "string", "description": f"{o.description} ({o.type})"} for o in cap.outputs}
    return {
        "name": cap.id.replace(".", "__"),
        "capability": f"{cap.id}@{cap.version}",
        "description": cap.description
        + f" Side effects: {cap.side_effects.value}."
        + (f" Business outcomes: {', '.join(o.code for o in cap.outcomes if o.kind.value == 'business')}." if cap.outcomes else ""),
        "input_schema": {"type": "object", "properties": props, "required": required, "additionalProperties": False},
        "output_schema": {"type": "object", "properties": outs},
        "requires_approval": cap.side_effects.value == "irreversible",
    }


def cmd_catalog(a: argparse.Namespace) -> int:
    tenant = load_tenant(a.tenant) if a.tenant else None
    tools = []
    for cap in list_capabilities(tenant.product if tenant else None):
        if cap.review.status != "approved" and not a.include_drafts:
            continue
        if tenant and not version_satisfies(tenant.product_version, cap.app.versions):
            continue
        tools.append(tool_definition(cap))
    print(json.dumps(tools, indent=2))
    return 0


def cmd_invoke(a: argparse.Namespace) -> int:
    """The production path an AI agent uses: approved capabilities only, no LLM, JSON in/out."""
    from .replay import ReplayOptions, replay

    name = a.capability.replace("__", ".")
    result = replay(
        name,
        a.tenant,
        json.loads(a.args),
        ReplayOptions(approve_irreversible=a.approve, operator=_operator(a.operator)),
        console=a.verbose,
    )
    _print_result(result)
    return 0 if result.status in ("succeeded", "business_outcome") else 1


def cmd_approve(a: argparse.Namespace) -> int:
    cap = load_capability(a.capability)
    cap.review.status = "approved"
    cap.review.reviewed_by = a.reviewer
    cap.review.reviewed_at = now_iso()
    path = capability_path(cap)
    path.write_text(dump_yaml(cap))
    print(f"approved {cap.id}@{cap.version} -> {path.relative_to(ROOT)}")
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    cap = load_capability(a.capability)
    if a.tenant:
        cap, patched = effective_capability(cap, load_tenant(a.tenant))
        print(f"# effective for tenant {a.tenant}; patched steps: {patched or 'none'}", file=sys.stderr)
    print(dump_yaml(cap))
    return 0


def cmd_schema(a: argparse.Namespace) -> int:
    from .schema import AppProfile, TenantConfig

    out = ROOT / "schemas"
    out.mkdir(exist_ok=True)
    for name, model in (("capability", Capability), ("app_profile", AppProfile), ("tenant", TenantConfig)):
        (out / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), indent=2))
    print(f"wrote JSON Schemas to {out.relative_to(ROOT)}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="cuflow", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="LLM-driven discovery run -> draft capability artifact")
    d.add_argument("--tenant", required=True)
    d.add_argument("--goal", required=True)
    d.add_argument("--provider", default="gemini", choices=["gemini", "claude"])
    d.add_argument("--model")
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--operator", default="none", help="none | terminal | scripted:<file.json>")
    d.add_argument("--headed", action="store_true", help="show the browser (needed for a human to take over)")
    d.add_argument("--no-screenshot", action="store_true", help="text-only observations")
    d.add_argument("--dry-run", action="store_true", help="do not write the artifact into the catalog")
    d.set_defaults(fn=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay of a capability (no LLM)")
    r.add_argument("capability", help="id, id@version, or path to a .yaml")
    r.add_argument("--tenant", required=True)
    r.add_argument("-p", "--param", action="append", default=[], help="name=value (repeatable)")
    r.add_argument("--allow-draft", action="store_true", help="allow replaying a draft (attended/dev runs)")
    r.add_argument("--approve", action="store_true", help="explicitly approve irreversible steps for this run")
    r.add_argument("--operator", default="none", help="none | terminal | scripted:<file.json>")
    r.add_argument("--headed", action="store_true")
    r.set_defaults(fn=cmd_replay)

    c = sub.add_parser("catalog", help="list approved capabilities as agent-callable tool definitions")
    c.add_argument("--tenant")
    c.add_argument("--include-drafts", action="store_true")
    c.set_defaults(fn=cmd_catalog)

    i = sub.add_parser("invoke", help="invoke a capability by name with JSON args (agent-facing path)")
    i.add_argument("capability")
    i.add_argument("--tenant", required=True)
    i.add_argument("--args", required=True, help="JSON object of inputs")
    i.add_argument("--approve", action="store_true")
    i.add_argument("--operator", default="none")
    i.add_argument("-v", "--verbose", action="store_true")
    i.set_defaults(fn=cmd_invoke)

    ap_ = sub.add_parser("approve", help="mark a reviewed capability version as approved")
    ap_.add_argument("capability")
    ap_.add_argument("--reviewer", required=True)
    ap_.set_defaults(fn=cmd_approve)

    s = sub.add_parser("show", help="print a capability (optionally as effective for a tenant)")
    s.add_argument("capability")
    s.add_argument("--tenant")
    s.set_defaults(fn=cmd_show)

    sc = sub.add_parser("schema", help="write JSON Schemas for the artifact formats")
    sc.set_defaults(fn=cmd_schema)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
