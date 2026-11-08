from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import pytest

os.environ.setdefault("MOCKCORE_PASSWORD", "demo-teller-pass")
for t in ("HARBOR", "SUMMIT"):
    os.environ.setdefault(f"CUFLOW_SECRET_{t}_OPERATOR_USERNAME", "teller01")
    os.environ.setdefault(f"CUFLOW_SECRET_{t}_OPERATOR_PASSWORD", "demo-teller-pass")

from cuflow.config import effective_capability, load_profile, load_tenant, load_yaml  # noqa: E402
from cuflow.evidence import RunRecorder  # noqa: E402
from cuflow.policy import PolicyEngine  # noqa: E402
from cuflow.redaction import Redactor  # noqa: E402
from cuflow.replay import Replayer, ReplayOptions  # noqa: E402
from cuflow.results import RunResult  # noqa: E402
from cuflow.schema import Capability, TenantConfig  # noqa: E402
from cuflow.surface.web import WebSurface  # noqa: E402
from mockcore.server import serve_in_thread  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def app() -> Any:
    """Factory: start a MockCore server for (tenant, faults); returns its base URL."""
    servers = []

    def start(tenant: str = "harbor", faults: str = "") -> str:
        srv, base = serve_in_thread(0, tenant, faults)
        servers.append(srv)
        return base

    yield start
    for s in servers:
        s.shutdown()


def tenant_at(name: str, base: str, **updates: Any) -> TenantConfig:
    t = load_tenant(name)
    updates.setdefault("overrides", {})  # tests opt in to overrides explicitly
    policy = t.policy.model_copy(update={"allowed_origins": [base]}, deep=True)
    return t.model_copy(update={"base_url": base, "policy": policy, **updates})


def fixture_capability(**review: Any) -> Capability:
    cap = Capability.model_validate(load_yaml(FIXTURES / "get_savings_balance.yaml"))
    if review:
        cap.review = cap.review.model_copy(update=review)
    return cap


@pytest.fixture
def run_replay(tmp_path: Path) -> Callable[..., RunResult]:
    def run(cap: Capability, tenant: TenantConfig, params: dict[str, Any], opts: ReplayOptions | None = None) -> RunResult:
        cap_eff, patched = effective_capability(cap, tenant)
        red = Redactor()
        rec = RunRecorder(tmp_path / "runs", "replay", red, console=False)
        surface = WebSurface(url_allowed=PolicyEngine(tenant.policy).allows_url, on_event=rec.event).start()
        try:
            return Replayer(cap_eff, load_profile(cap.app.product), tenant, surface, rec, red, opts, patched).run(params)
        finally:
            surface.close()
            rec.close()

    return run
