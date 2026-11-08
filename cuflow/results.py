"""The result contract returned to callers (an AI agent, a CLI, a test).

Four terminal statuses, deliberately not collapsed into success/failure:

* ``succeeded``         — checkpoint verified; ``outputs`` holds the typed values.
* ``business_outcome``  — the app gave a legitimate answer the caller must handle
                          ("no such member", "restricted account"). Not an error.
* ``blocked``           — we stopped *before* acting because policy requires something the
                          caller has not provided (approval for an irreversible step, an
                          approved artifact). Nothing was committed.
* ``failed``            — a hard failure with enough detail to debug: which step, what we
                          expected, what was on screen, and pointers to evidence.

Recoveries, drift and human interventions are reported alongside any status, so a
"succeeded after re-login" is distinguishable from a clean success.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"  # caller params failed the contract; UI never touched
    NOT_APPROVED = "NOT_APPROVED"  # artifact is draft and unattended replay requires approved
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"  # irreversible step needs an explicit approval
    POLICY_VIOLATION = "POLICY_VIOLATION"  # action/location outside the allowlist
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    UNRECOGNIZED_STATE = "UNRECOGNIZED_STATE"  # screen settled into a state we have no rule for
    TIMEOUT = "TIMEOUT"  # surface never settled / expectation never met
    APP_ERROR = "APP_ERROR"  # a known failure outcome (e.g. HTTP 500 page)
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    INCOMPATIBLE_APP = "INCOMPATIBLE_APP"  # tenant runs a product version the capability does not support
    STEP_BUDGET_EXCEEDED = "STEP_BUDGET_EXCEEDED"
    MODEL_ERROR = "MODEL_ERROR"  # discovery: the LLM provider failed (quota, auth, outage)
    AGENT_STUCK = "AGENT_STUCK"  # discovery: no progress / repeated invalid actions, and no human took over
    SURFACE_ERROR = "SURFACE_ERROR"


class Observed(BaseModel):
    locations: dict[str, str] = Field(default_factory=dict, description="frame -> url (redacted)")
    alerts: list[str] = Field(default_factory=list, description="error-looking text on screen (redacted)")
    screenshot: str | None = None
    dom: list[str] = Field(default_factory=list)


class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str
    step_id: str | None = None
    step_intent: str | None = None
    expected: str | None = None
    observed: Observed | None = None
    retryable: bool = False


class BusinessOutcome(BaseModel):
    code: str
    message: str
    outcome_id: str
    step_id: str | None = None


class DriftEvent(BaseModel):
    step_id: str
    target: str
    primary: str = Field(description="the locator we expected to work")
    used: str = Field(description="the fallback that actually resolved")
    note: str


class RecoveryEvent(BaseModel):
    outcome_id: str
    step_id: str | None
    strategy: str
    succeeded: bool
    detail: str = ""


class HumanAction(BaseModel):
    type: str
    target: str
    frame: str | None = None
    value: str | None = None  # redacted


class InterventionRecord(BaseModel):
    id: str
    reason: str
    kind: str
    step_id: str | None
    operator: str | None
    resolution: str | None
    note: str | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)
    waited_ms: int = 0


class StepRecord(BaseModel):
    step_id: str
    status: Literal["ok", "recovered", "failed", "skipped", "blocked"]
    locator: str | None = None
    attempts: int = 1
    duration_ms: int = 0


class RunResult(BaseModel):
    run_id: str
    mode: Literal["replay", "discovery"]
    capability: str | None = None
    capability_version: str | None = None
    tenant: str | None = None
    status: Literal["succeeded", "business_outcome", "blocked", "failed"]
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: BusinessOutcome | None = None
    error: ErrorDetail | None = None
    steps: list[StepRecord] = Field(default_factory=list)
    recoveries: list[RecoveryEvent] = Field(default_factory=list)
    drift: list[DriftEvent] = Field(default_factory=list)
    interventions: list[InterventionRecord] = Field(default_factory=list)
    duration_ms: int = 0
    evidence_dir: str | None = None
