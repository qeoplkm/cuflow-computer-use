"""Loading the catalog: app profiles, capabilities, tenants, overrides and secrets.

Catalog layout (plain files, reviewable in a PR):

    catalog/apps/<product>/profile.yaml
    catalog/apps/<product>/capabilities/<capability id>@<version>.yaml
    catalog/tenants/<tenant>.yaml
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from .schema import AppProfile, Capability, TenantConfig

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path(os.environ.get("CUFLOW_CATALOG", ROOT / "catalog"))
RUNS = Path(os.environ.get("CUFLOW_RUNS", ROOT / "runs"))


# ---- yaml ----------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_presenter)


def _dict_presenter(dumper: yaml.SafeDumper, data: dict) -> yaml.Node:
    # Small all-scalar mappings (locators, bboxes, row keys) read best on one line.
    flow = 0 < len(data) <= 5 and all(not isinstance(v, (dict, list)) for v in data.values())
    flow = flow and sum(len(str(k)) + len(str(v)) for k, v in data.items()) < 90
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=flow)


_Dumper.add_representer(dict, _dict_presenter)

# Defaults that only add noise for a reviewer. Discriminators (by/kind/type), risk,
# idempotent and actor are always kept on purpose.
_NOISE = {"match": "exact", "offset": 1, "regex": False, "required": True, "timeout_ms": 15000}


def _strip_noise(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _strip_noise(v) for k, v in o.items() if not (k in _NOISE and v == _NOISE[k])}
    if isinstance(o, list):
        return [_strip_noise(v) for v in o]
    return o


def dump_yaml(data: Any) -> str:
    if isinstance(data, BaseModel):
        data = _strip_noise(data.model_dump(mode="json", exclude_none=True))
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110)


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(Path(path).read_text())


# ---- dotenv (tiny; avoids a dependency) ------------------------------------------------


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---- catalog -------------------------------------------------------------------------


def load_tenant(name: str) -> TenantConfig:
    return TenantConfig.model_validate(load_yaml(CATALOG / "tenants" / f"{name}.yaml"))


def load_profile(product: str) -> AppProfile:
    return AppProfile.model_validate(load_yaml(CATALOG / "apps" / product / "profile.yaml"))


def capability_dir(product: str) -> Path:
    return CATALOG / "apps" / product / "capabilities"


def capability_path(cap: Capability) -> Path:
    return capability_dir(cap.app.product) / f"{cap.id}@{cap.version}.yaml"


def _semver_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def list_capabilities(product: str | None = None) -> list[Capability]:
    """Latest version of every capability (optionally for one product)."""
    root = CATALOG / "apps"
    latest: dict[str, Capability] = {}
    for p in sorted(root.glob(f"{product or '*'}/capabilities/*.yaml")):
        cap = Capability.model_validate(load_yaml(p))
        cur = latest.get(cap.id)
        if cur is None or _semver_key(cap.version) > _semver_key(cur.version):
            latest[cap.id] = cap
    return list(latest.values())


def load_capability(ref: str) -> Capability:
    """A path, 'id' (latest version) or 'id@version'."""
    p = Path(ref)
    if p.suffix in (".yaml", ".yml") and p.exists():
        return Capability.model_validate(load_yaml(p))
    cap_id, _, version = ref.partition("@")
    paths = sorted(
        (CATALOG / "apps").glob(f"*/capabilities/{cap_id}@*.yaml"),
        key=lambda x: _semver_key(x.stem.rpartition("@")[2]),
    )
    if version:
        paths = [p for p in paths if p.stem == f"{cap_id}@{version}"]
    if not paths:
        raise FileNotFoundError(f"capability {ref!r} not found in {CATALOG}")
    return Capability.model_validate(load_yaml(paths[-1]))


# ---- versions ------------------------------------------------------------------------


def version_satisfies(version: str, spec: str) -> bool:
    """Tiny range check: '>=4.2,<5' / '1.x' / '4.2.1'."""
    v = _semver_key(version)
    for part in filter(None, (s.strip() for s in spec.split(","))):
        if part.endswith(".x"):
            if v[: len(_semver_key(part))] != _semver_key(part):
                return False
            continue
        m = re.match(r"(>=|<=|>|<|==)?\s*(.+)", part)
        assert m
        op, other = m.group(1) or "==", _semver_key(m.group(2))
        n = max(len(v), len(other))
        a, b = v + (0,) * (n - len(v)), other + (0,) * (n - len(other))
        if not {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b}[op]:
            return False
    return True


# ---- tenant overrides ------------------------------------------------------------------


def merge_patch(base: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch (dicts merge recursively, null deletes, lists replace)."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def link_targets(cap: Capability) -> Capability:
    """Inline every {ref: name} with the named Target, so the engine only sees Targets."""
    data = cap.model_dump(mode="json", exclude_none=True)
    targets = data.get("targets", {})

    def walk(o: Any) -> Any:
        if isinstance(o, dict):
            if set(o) == {"ref"}:
                if o["ref"] not in targets:
                    raise ValueError(f"{cap.id}: unknown target ref {o['ref']!r}")
                return targets[o["ref"]]
            return {k: (v if k == "targets" else walk(v)) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o

    return Capability.model_validate(walk(data))


def effective_capability(cap: Capability, tenant: TenantConfig) -> tuple[Capability, list[str]]:
    """Apply the tenant's patch (if any) on top of the shared capability, then link targets.

    Returns the effective capability and the names of the patched targets/steps."""
    ov = tenant.overrides.get(cap.id)
    if ov is None:
        return link_targets(cap), []
    if not version_satisfies(cap.version, ov.applies_to):
        raise ValueError(
            f"tenant {tenant.tenant} override for {cap.id} targets {ov.applies_to}, capability is {cap.version}"
        )
    data = cap.model_dump(mode="json", exclude_none=True)
    unknown = (set(ov.steps) - {s["id"] for s in data["steps"]}) | (set(ov.targets) - set(data.get("targets", {})))
    if unknown:
        raise ValueError(f"override patches unknown steps/targets {sorted(unknown)} (capability changed?)")
    data["targets"] = {k: merge_patch(v, ov.targets[k]) if k in ov.targets else v for k, v in data.get("targets", {}).items()}
    data["steps"] = [merge_patch(s, ov.steps[s["id"]]) if s["id"] in ov.steps else s for s in data["steps"]]
    patched = [f"targets.{k}" for k in sorted(ov.targets)] + [f"steps.{k}" for k in sorted(ov.steps)]
    return link_targets(Capability.model_validate(data)), patched


# ---- secrets ---------------------------------------------------------------------------


class SecretStore:
    """Resolves logical secret names through the tenant's references.

    Only the ``env:`` scheme is implemented (local/dev). Production would add a vault
    scheme; the rest of the system only ever sees ``SecretRef(name)``.
    """

    def __init__(self, tenant: TenantConfig) -> None:
        self.tenant = tenant

    def get(self, name: str) -> str:
        ref = self.tenant.secrets.get(name)
        if ref is None:
            raise KeyError(f"secret {name!r} is not configured for tenant {self.tenant.tenant}")
        scheme, _, key = ref.partition(":")
        if scheme != "env":
            raise NotImplementedError(f"secret scheme {scheme!r} not implemented")
        val = os.environ.get(key)
        if val is None:
            raise KeyError(f"secret {name!r}: environment variable {key} is not set")
        return val
