# cuflow — design report

## 1. Architecture

A natural-language goal goes through **LLM discovery → recorded trace → draft capability →
review → deterministic replay**. The calling agent receives typed outputs or a structured
business/error result. Discovery uses Gemini or Claude; replay never makes a model call.

Python, synchronous Playwright and Pydantic keep this a single inspectable process. A YAML
catalog avoids a database and deployment infrastructure. The target, MockCore Teller, is a
synthetic local app with framesets, nested tables, unlabelled fields, JavaScript navigation
and dynamic URL tokens. Fault injection makes exceptional states reproducible.

The model gets the current text observation, a screenshot and the last 15 actions. It calls
one tool at a time. A deterministic app profile establishes the session first, so passwords
never enter the model prompt. Transient provider failures have bounded retries; billing or
authentication failures stop explicitly. Model/API errors are evidence of a failed attempt,
not successful discovery.

## 2. Artifact schema

Three versioned layers separate vendor login/error behavior, task capabilities and tenant
configuration. A capability holds inputs with validation and sensitivity, output types,
named targets, ordered actions, risk/idempotency, postconditions, a final checkpoint,
business outcomes, review state and provenance. Values use parameter or secret references.

The compiler records what happened on screen rather than serializing the model transcript.
It verifies candidate locators against the element actually used, replaces example inputs
with placeholders, and canonicalizes transient URL query values. After a navigation, the
next target plus the observed location form a postcondition. An extracted table value is
located by column and row identity, so a different account order still works.

Discovery produces a draft, not automatic production approval: one successful example
cannot establish all valid inputs or outcomes. The submitted lookup review tightens the
member ID to five digits, adds not-found/restricted-account outcomes and removes positional
CSS fallbacks. The original artifact, reviewed version and exact diff are in
`evidence/artifacts/`; provenance still points to the genuine model run. The demo review
script checks this specific contract and is not a general automatic approval service.

## 3. Determinism & error handling

Replay validates inputs, review state, app compatibility and declared irreversible steps
before operating the UI. Ordered locator strategies must resolve a unique element. After
actions, condition-based polling waits for document requests to finish, frames to load and
the relevant DOM to become quiet. A known outcome takes precedence over a postcondition.
A settled, unrecognized screen fails after a grace period; a screen that remains busy hits
a timeout. Polling intervals are implementation detail, not blind delays between actions.

The result distinguishes **business outcomes**, bounded **recoveries**, **hard failures**
and **blocked** runs. A failure includes the step, expected state, observed locations/error
text and masked screenshot/DOM evidence. Examples include missing members, restricted
accounts, session expiry, transient/persistent application errors and an unknown survey.

Restarts are bounded and prohibited after a non-idempotent action. Runtime risk also makes
an irreversible action non-idempotent even if an artifact understated it. Money is parsed
with a strict whole-string format and exact Decimal arithmetic; `E1001` must fail rather
than become a balance. The final checkpoint and presence of declared outputs are checked.
The evidence generator asserts expected outcomes and values; a successful process alone
is not considered a passing scenario.

## 4. Heterogeneity & multi-tenant

Perception infers role/name from visible controls and neighboring table labels. The same
JavaScript describes and resolves controls. Semantic locators are preferred, followed by
vendor attributes and table-row anchors. The reviewed lookup drops structural CSS fallbacks
because a uniquely resolved but wrong row would be worse than a visible failure.

The Surface protocol sketches observe/describe/resolve/act/probe/evidence/handoff operations.
**Only web is implemented.** This is not yet a drop-in desktop interface: discovery still
uses Playwright handles and frame URLs, and the runtime catches Playwright errors. A UI
Automation adapter would first require moving those calls and exceptions behind the seam,
then mapping roles/names to ControlType/Name and tables to Grid patterns. An OCR adapter
would need its own anchor/coordinate schema and reliability evaluation.

A capability is shared per vendor product. Tenant configuration holds origin, credentials,
version and reviewed merge patches keyed by target or step identity. A patch fails if its
version range or referenced identities no longer match. Harbor's recorded lookup runs on
Summit through four fallbacks; its explicit four-target patch removes that drift. Tests
exercise the shipped catalog/configuration, not only an independently constructed fixture.
At scale, drift aggregation and sandbox canaries would propose reviewed patches; they
would not automatically repair unknown checkpoints.

## 5. Escalation & handoff

Repeated invalid/ineffective discovery actions, model-requested help, unrecognized replay
states and irreversible actions can raise an intervention. The request carries context,
reason, locations, screenshot, proposed action and allowed resolutions.

Session ownership follows **AUTOMATION → AWAITING_HUMAN → HUMAN → AUTOMATION**. The operator
controls the same live browser context, retaining cookies and partially filled forms.
Automation checks ownership before acting; after release it checks the screen again.
The terminal operator supports a real person using a headed browser. A scripted operator
exercises the same transfer mechanism and is explicitly labelled simulated in evidence.
Input events are captured and their values registered for redaction before logging.

The submitted approval demonstration uses a simulated supervisor on synthetic data, not
an independently authenticated human approver. A production console would require a
claimed lease, expiry handling, authenticated operator identity and authorization bound
to the exact action and arguments. Those services are outside this implementation.

## 6. Safety

A configurable origin/path/action allowlist is checked before actions, at browser request
routing and after replay steps. Admin fault-injection paths are denied to automation.
Risk is the maximum of declared risk and control/action rules. Enter and Space activation
inherit submit risk; unsupported shortcuts require approval, and implicit-focus keyboard
actions are rejected. An explicit block policy stays blocked even with caller approval.
Keyword rules remain imperfect for unusually named commit controls; production needs
vendor-specific route/action annotations and independent authorization.

Evidence gathers sensitive values before screenshots or DOM serialization, including
labelled fields, grid columns and form inputs. Known values, HTML-escaped forms, common PII,
amounts and URL tokens are redacted. DOM copies remove scripts and form values without
changing the live session. Discovery omits raw goal text and model prose from persisted
prompts and registers entered values before logging tool arguments. This deliberately
limits exact prompt reproduction. Screenshots mask data; raw Playwright traces are disabled.

Discovery still sends unredacted observations to the provider and therefore belongs on
synthetic sandbox tenants. Redaction is best effort outside the supported layouts and
patterns. The caller receives unmasked outputs and owns its logging. Local YAML approval
and caller flags are developer controls, not production identity or consent enforcement.

## 7. Cuts

No desktop/OCR implementation, remote operator console, queue, worker fleet, vault backend
or evidence encryption. No automatic outcome learning, dead-end pruning or LLM recovery
inside replay. These cuts preserve a small, testable end-to-end implementation.

Next: bind approval to authenticated identities and exact arguments; probe more failure
states with synthetic inputs; complete the surface abstraction; add repeated-run reliability
measurement before approval. Feature breadth is secondary to verified behavior and honest
provenance. `evidence/README.md` records what was actually run, including failed attempts.
