"""Typed, versioned data model for capabilities (the recorded artifact) and their context.

Layering (each layer is a separate, separately-reviewed file):

* ``AppProfile``   — one per *vendor product* (e.g. MockCore Teller 4.x). How to sign on,
                     and the cross-cutting screen states every flow can hit (maintenance
                     notices, session expiry, app errors). Shared by all tenants.
* ``Capability``   — one per *task* on that product ("get savings balance"). Typed inputs and
                     outputs, ordered steps with robust targets, postconditions, a success
                     checkpoint, and the task-specific business outcomes.
* ``TenantConfig`` — one per *institution*. Base URL, secret references, policy, and small
                     per-capability patches (keyed by stable step ids) for local relabeling.

Nothing in a capability is web-specific except the locator kinds a surface understands and
the ``frame`` scope; a desktop surface adds locator kinds (e.g. UIA AutomationId) without
changing steps, conditions, outcomes or the result contract.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

CAPABILITY_SCHEMA = "cuflow.capability/v1"
PROFILE_SCHEMA = "cuflow.app_profile/v1"
TENANT_SCHEMA = "cuflow.tenant/v1"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ======================================================================================
# Targets: how a control is identified
# ======================================================================================
#
# A Target carries an ordered list of independent locator strategies. Replay uses the first
# one that resolves to *exactly one* element. Anything after index 0 is a fallback, and
# using one is reported as drift (the flow still runs; the artifact is flagged for review).
#
# Ranking (most to least robust in our environment):
#   role        — accessibility role + accessible (or inferred) name: what an operator sees.
#                 Survives markup refactors; changes when a tenant relabels a field.
#   table_cell  — a cell addressed by column header + row key, or by row label. Survives
#                 row reordering (different members have different accounts).
#   attr        — vendor-internal attributes (form field name, href route). Invisible to
#                 tenants, so survives relabeling; changes on vendor upgrades.
#   text        — visible text of a text block.
#   css         — structural path. Deterministic but brittle; last resort, always drift.


class RoleLocator(Model):
    by: Literal["role"] = "role"
    role: str
    name: str = Field(description="accessible or inferred name; may contain {param} placeholders")
    match: Literal["exact", "contains", "regex"] = "exact"
    row_text: str | None = Field(None, description="only elements whose table row contains this text")
    nth: int | None = None


class RowKey(Model):
    column: str
    value: str
    match: Literal["exact", "contains"] = "exact"


class TableCellLocator(Model):
    by: Literal["table_cell"] = "table_cell"
    column: str | None = Field(None, description="header text of the column (header = first row)")
    row_key: RowKey | None = Field(None, description="select the row whose <column> cell equals <value>")
    row_label: str | None = Field(None, description="key/value layout: the row whose first cell is this label")
    offset: int = 1
    nth: int | None = None


class AttrLocator(Model):
    by: Literal["attr"] = "attr"
    tag: str | None = None
    attrs: dict[str, str] = Field(description="exact match; 'href' is a glob")
    nth: int | None = None


class TextLocator(Model):
    by: Literal["text"] = "text"
    text: str
    match: Literal["exact", "contains", "regex"] = "exact"
    nth: int | None = None


class CssLocator(Model):
    by: Literal["css"] = "css"
    css: str
    nth: int | None = None


Locator = Annotated[
    Union[RoleLocator, TableCellLocator, AttrLocator, TextLocator, CssLocator], Field(discriminator="by")
]


class BBox(Model):
    x: int
    y: int
    w: int
    h: int


class Target(Model):
    description: str = Field(description="human-readable, e.g. 'Member Number text box'")
    frame: str | None = Field(None, description="surface scope: frame name (web) / window (desktop)")
    locators: list[Locator] = Field(min_length=1)
    bbox_hint: BBox | None = Field(None, description="where it was at record time; diagnostics only")


class TargetRef(Model):
    """Reference into Capability.targets. Each control is defined once (like a page object),
    so a reviewer reads it once and a tenant override patches it once."""

    ref: str


TargetLike = Union[Target, TargetRef]


# ======================================================================================
# Values (never raw secrets; never raw PII from the recording)
# ======================================================================================


class ParamRef(Model):
    param: str


class SecretRef(Model):
    secret: str = Field(description="logical secret name, resolved through the tenant's secret store")


class LiteralValue(Model):
    literal: str


Value = Union[ParamRef, SecretRef, LiteralValue]


# ======================================================================================
# Conditions: postconditions, checkpoints, outcome detectors
# ======================================================================================


class TextCondition(Model):
    kind: Literal["text"] = "text"
    pattern: str
    regex: bool = False
    frame: str | None = None


class ElementCondition(Model):
    kind: Literal["element"] = "element"
    target: TargetLike


class LocationCondition(Model):
    kind: Literal["location"] = "location"
    pattern: str = Field(description="glob over path?query, {param} placeholders allowed")
    frame: str | None = None
    ignore_query: list[str] = Field(default_factory=list, description="explicit transient query keys ignored in both expected and observed locations")


class DialogCondition(Model):
    kind: Literal["dialog"] = "dialog"
    pattern: str


class AllCondition(Model):
    kind: Literal["all"] = "all"
    of: list["Condition"]


class AnyCondition(Model):
    kind: Literal["any"] = "any"
    of: list["Condition"]


class NotCondition(Model):
    kind: Literal["not"] = "not"
    cond: "Condition"


Condition = Annotated[
    Union[
        TextCondition, ElementCondition, LocationCondition, DialogCondition, AllCondition, AnyCondition, NotCondition
    ],
    Field(discriminator="kind"),
]


# ======================================================================================
# Actions and steps
# ======================================================================================


class ClickAction(Model):
    type: Literal["click"] = "click"
    target: TargetLike


class FillAction(Model):
    type: Literal["fill"] = "fill"
    target: TargetLike
    value: Value


class SelectAction(Model):
    type: Literal["select"] = "select"
    target: TargetLike
    option: Value


class PressAction(Model):
    type: Literal["press"] = "press"
    key: str
    target: TargetLike | None = None


class NavigateAction(Model):
    type: Literal["navigate"] = "navigate"
    url: str = Field(description="path relative to the tenant base_url; {param} placeholders allowed")
    frame: str | None = None


class ExtractAction(Model):
    type: Literal["extract"] = "extract"
    target: TargetLike
    output: str


Action = Annotated[
    Union[ClickAction, FillAction, SelectAction, PressAction, NavigateAction, ExtractAction],
    Field(discriminator="type"),
]

ACTION_TYPES = ("click", "fill", "select", "press", "navigate", "extract")


class Risk(str, Enum):
    safe = "safe"  # no persisted business effect (navigate, read, type into an unsaved form)
    reversible = "reversible"  # persists something that can be undone (save a draft, place a hold)
    irreversible = "irreversible"  # commits something (submit, post, transfer, delete)


RISK_ORDER = {Risk.safe: 0, Risk.reversible: 1, Risk.irreversible: 2}


class Step(Model):
    id: str = Field(description="stable id; logs, drift reports and tenant overrides key off it")
    intent: str = Field(description="why this step exists, for reviewers")
    action: Action
    risk: Risk = Risk.safe
    idempotent: bool = Field(True, description="safe to repeat if the flow must restart")
    expect: Condition | None = Field(None, description="postcondition to wait for (we never sleep)")
    timeout_ms: int = 15000
    actor: Literal["agent", "human", "author"] = Field("agent", description="who performed it when recorded")


# ======================================================================================
# Outcomes: the error/exception taxonomy
# ======================================================================================


class OutcomeKind(str, Enum):
    business = "business"  # a legitimate answer for the caller ("no such member")
    recoverable = "recoverable"  # we know how to get past it (interstitial, session expiry)
    failure = "failure"  # stop and surface a debuggable error (app error)


class Recovery(Model):
    strategy: Literal["run_steps", "restart", "relogin_restart", "escalate"]
    steps: list[Step] = Field(default_factory=list, description="for run_steps (e.g. click 'Acknowledge')")
    max_attempts: int = 2


class Outcome(Model):
    id: str
    kind: OutcomeKind
    when: Condition
    code: str = Field(description="stable machine-readable code returned to the caller")
    message: str
    steps: list[str] | None = Field(None, description="step ids where this can occur; null = anywhere")
    recovery: Recovery | None = None
    retryable: bool = Field(False, description="failure only: may the caller retry later")


# ======================================================================================
# Contract
# ======================================================================================


class Sensitivity(str, Enum):
    public = "public"
    internal = "internal"
    pii = "pii"  # identifies a person: masked in logs
    financial = "financial"  # balances, amounts: masked in logs
    secret = "secret"  # credentials: never logged, never in artifacts


class ParamSpec(Model):
    name: str
    type: Literal["string", "integer", "decimal", "boolean"] = "string"
    description: str
    pattern: str | None = None
    enum: list[str] | None = None
    required: bool = True
    sensitivity: Sensitivity = Sensitivity.internal


class OutputSpec(Model):
    name: str
    type: Literal["string", "money", "decimal", "integer", "date"] = "string"
    description: str
    sensitivity: Sensitivity = Sensitivity.internal


class AppRef(Model):
    product: str
    versions: str = Field(description="compatible product versions, e.g. '>=4.2,<5'")
    surface: Literal["web", "desktop", "terminal"] = "web"


class Provenance(Model):
    method: Literal["llm_discovery", "human_demonstration", "authored"]
    recorded_at: str
    run_id: str | None = None
    model: str | None = None
    tenant: str | None = None
    app_version: str | None = None
    goal_template: str | None = Field(None, description="the goal with concrete inputs replaced by {params}")


class Review(Model):
    status: Literal["draft", "approved", "deprecated"] = "draft"
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    notes: list[str] = Field(default_factory=list)


class Capability(Model):
    schema_version: Literal["cuflow.capability/v1"] = CAPABILITY_SCHEMA
    id: str = Field(description="<product>.<domain>.<verb_noun>, e.g. mockcore.member.get_savings_balance")
    version: str = Field(description="semver of this capability")
    title: str
    description: str = Field(description="what it does, for a calling agent")
    app: AppRef
    requires_session: bool = Field(True, description="run the app profile's login before step 1")
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    side_effects: Risk = Risk.safe
    targets: dict[str, Target] = Field(default_factory=dict, description="named controls, referenced as {ref: name}")
    steps: list[Step]
    success: Condition = Field(description="checkpoint asserted after the last step")
    outcomes: list[Outcome] = Field(default_factory=list, description="task-specific; profile outcomes also apply")
    review: Review = Field(default_factory=Review)
    provenance: Provenance


class AppProfile(Model):
    schema_version: Literal["cuflow.app_profile/v1"] = PROFILE_SCHEMA
    product: str
    versions: str
    surface: Literal["web", "desktop", "terminal"] = "web"
    login: list[Step] = Field(description="session establishment; credentials only via SecretRef")
    landing: Condition = Field(description="true once a session is established")
    outcomes: list[Outcome] = Field(default_factory=list, description="cross-cutting states any flow can hit")


# ======================================================================================
# Tenant
# ======================================================================================


class Policy(Model):
    allowed_origins: list[str]
    allowed_paths: list[str] = Field(description="globs over the URL path")
    denied_paths: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=lambda: list(ACTION_TYPES))
    irreversible_keywords: list[str] = Field(
        default_factory=lambda: ["submit", "confirm", "post", "transfer", "delete", "close account", "approve", "send"]
    )
    reversible_keywords: list[str] = Field(default_factory=lambda: ["save", "update", "apply", "hold"])
    on_irreversible: Literal["block", "require_approval"] = "require_approval"
    max_steps: int = 25
    max_seconds: int = 300


class CapabilityOverride(Model):
    applies_to: str = Field(description="capability version range this patch was written for, e.g. '1.x'")
    reason: str
    targets: dict[str, dict] = Field(default_factory=dict, description="target name -> JSON merge patch")
    steps: dict[str, dict] = Field(default_factory=dict, description="step id -> JSON merge patch")


class TenantConfig(Model):
    schema_version: Literal["cuflow.tenant/v1"] = TENANT_SCHEMA
    tenant: str
    display_name: str
    product: str
    product_version: str
    base_url: str
    secrets: dict[str, str] = Field(description="logical name -> secret reference (never a value)")
    policy: Policy
    overrides: dict[str, CapabilityOverride] = Field(default_factory=dict)


AllCondition.model_rebuild()
AnyCondition.model_rebuild()
NotCondition.model_rebuild()
