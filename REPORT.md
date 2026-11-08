# cuflow — design report

**The model discovers. The artifact becomes a reusable capability. Deterministic replay is how the agent invokes it.**
The proxy target, **MockCore Teller**, is a synthetic local app built to be hostile: framesets, nested layout tables,
no ids/labels/test ids, cryptic field names, JavaScript links, red-`<font>` errors, per-render URL tokens. It runs as
two tenants and supports injectable runtime faults.

*Real vs simulated:*
- Discovery is genuine: Gemini `gemini-3.5-flash`, five attempts, three succeeded, two failures kept in `evidence/`.
- The supervisor approving the irreversible *Submit* is a scripted operator, labeled as such.
- All data is synthetic.

## 1. Architecture

```
 goal ─▶ Discovery agent ──trace──▶ Compiler ──▶ capability YAML (draft) ──human review──▶ approved
          (LLM: observe→decide→act)                                                      │
                ├───────────────▶ Surface adapter ◀────────── Replay engine (no LLM) ◀── agent: invoke(name, typed args)
   Policy gate (allowlist, risk) · SessionControl (who drives) · Operator (human) · Evidence recorder (redacted)
```

It is one synchronous Python process using Playwright and Pydantic, with a YAML catalog and no services.

- **Perception is an accessibility-style view computed in the page** (`surface/perception.js`). Every control gets a
  role and an accessible name. Where markup has no name (the usual legacy case), the name is inferred the way an
  operator reads the screen: from the preceding cell in the same table row, or the text just before the control.
  - The same script describes an element at record time and finds it at replay time, so the two cannot disagree.
  - Role + name maps directly onto desktop accessibility trees (UIA/AX). That is why I chose it over CSS selectors or
    pixels. A screenshot is a secondary signal for the model.
- **The agent is stateless per step.** Each turn sends the goal, the last 15 actions and the current screen, and asks
  for exactly one tool call.
  - Context stays bounded, and every decision is reproducible from its logged prompt.
  - Changing provider (Gemini/Claude behind `LLMClient`) touches one method.
  - Billing or authentication errors end the run as `MODEL_ERROR`; they never count as a discovery.
- **Login is not the model's job.** A per-product app profile signs on deterministically, with secrets referenced by
  name. Passwords never reach the model.
  - Known interstitials are dismissed by replay's own recovery code before the model looks, so they never become steps.
- **The artifact is compiled from what happened on screen, not from the transcript.** The model declares intent,
  risk, parameters and outputs through tool arguments. Targets, postconditions and checkpoints come from the observed
  state.
- **Simplicity.** Replay is a pure function of (artifact, tenant config, params) plus a surface, so moving it onto
  workers later is a clean cut. That infrastructure is deliberately not built.

## 2. Artifact schema

There are three versioned layers (JSON Schemas in `schemas/`):

| File | Scope | Holds |
|---|---|---|
| **app profile** | per vendor product | login and cross-cutting outcomes: maintenance notice, session expiry, system error, not authorized |
| **capability** | per task | the contract plus the program |
| **tenant** | per institution | base URL, product version, secret refs, policy, and small reviewed patches |

An excerpt of the shipped `mockcore.member.get_savings_balance@1.1.0`:

```yaml
inputs:  [{name: member_id, type: string, pattern: '^\d{5}$', sensitivity: pii}]
outputs: [{name: savings_balance, type: money, sensitivity: financial}]
side_effects: safe
targets:                                    # each control defined once (page-object style)
  member_number_text_box:
    frame: work
    locators:                               # ordered; first that matches exactly one element wins
    - {by: role, role: textbox, name: Member Number}         # what the operator sees (name inferred from the row)
    - {by: attr, tag: input, attrs: {name: fld_mbr_no}}     # vendor-internal: survives tenant relabeling
  select_link:           {locators: [{by: role, role: link, name: Select, row_text: '{member_id}'}, …]}
  balance_share_savings: {locators: [{by: table_cell, column: Balance, row_key: {column: Description, value: Share Savings}}, …]}
steps:
- id: click_select
  intent: Select member from search results              # the model's rationale, kept for reviewers
  action: {type: click, target: {ref: select_link}}
  risk: safe   idempotent: true   actor: agent
  expect: {kind: all, of: [{kind: location, frame: work, pattern: '/work/member?m={member_id}&t=*', ignore_query: [t]},
                           {kind: element, target: {ref: balance_share_savings}}]}
success:    {kind: all, of: [{kind: text, frame: work, pattern: MEMBER DETAIL}, …]}
outcomes:   [{id: member_not_found, kind: business, code: MEMBER_NOT_FOUND, when: {kind: text, pattern: I2001 NO RECORDS MATCH}}, …]
review:     {status: approved, reviewed_by: Shuai Yao, notes: […]}
provenance: {method: llm_discovery, model: gemini-3.5-flash, run_id: discovery-…-3d3c, tenant: harbor, app_version: 4.2.1}
```

Why it is shaped this way:

- **A calling agent needs a contract.** Inputs are typed and validated before the UI is touched. Outputs are typed.
  Side effects and business-outcome codes are declared. `cuflow catalog` renders exactly this as a tool definition.
- **A reviewer needs intent and provenance.** Each step records why it exists, its risk, and who performed it
  (`agent | human | author`). Named targets make the step list read like a script.
- **Locators are an ordered list of independent strategies**, each checked at record time to resolve to *exactly*
  the element acted on. A locator that is ambiguous when recorded only gets worse.
  - Order: role + name, then table cell by header + row key, then vendor attributes, then text, then CSS.
  - The row key survives row order: member 10051's accounts are listed in a different order.
  - A control repeated on every row is qualified by its row, which generalizes as `row_text: '{member_id}'`.
- **Values are typed references** (`{param}`, `{secret}`, `{literal}`).
  - Recorded data becomes `{param}`.
  - Volatile URL tokens become `*` and are listed in `ignore_query`; identity-bearing query values stay exact.
  - The compiler refuses to save an artifact containing any recorded input or output value.
- **Postconditions are derived, not hand-tuned.** After step *i*, the engine waits until each navigated frame is at
  the canonical location *and* step *i+1*'s target resolves.
- **Discovery yields a draft, never an approval.** One example cannot establish all inputs or outcomes. The review
  (diff in `evidence/artifacts/lookup-review.diff`):
  - tightened `member_id` to five digits;
  - added the not-found and restricted-account outcomes;
  - removed the positional CSS fallbacks, so a missing target fails visibly instead of reading the wrong row;
  - signed the approval by name.

## 3. Determinism & error handling

Pre-flight checks run before any UI action: the input contract, review status, app-version compatibility, and
approval for declared irreversible steps.

**There are no fixed sleeps.** After each action the engine waits until the screen *settles*:
- no document request in flight;
- every frame loaded;
- the DOM quiet for 350 ms in the frames the step cares about (so a banner clock cannot stall the work frame).

It then classifies the settled screen in a fixed order:
1. A **known outcome** (capability first, then app-wide).
2. The **expected postcondition** → next step.
3. Nothing matches and the screen **stays settled for 2 s** → `UNRECOGNIZED_STATE`.
4. The screen **never settles** within the step budget → `TIMEOUT`.

Separating slow (requests in flight: keep waiting) from stuck (stable but unknown: fail fast) lets a 4 s load pass
while an unknown page fails in about 2 s instead of 15.

| Class | Exercised in `evidence/replays/` | Result |
|---|---|---|
| **business outcome** | 03 not found, 04 restricted account | `business_outcome` + code; not an error |
| **recoverable** | 06 maintenance interstitial + native `alert()`, 07 session expiry → re-login + restart, 08 4 s slow load, 09 one HTTP 500 → restart | continues; each recovery listed in `recoveries` |
| **hard failure** | 10 persistent 500 (`APP_ERROR`), 11 unknown interstitial (`UNRECOGNIZED_STATE`), 15 failed checkpoint on a PII-heavy page | `failed` + step, intent, *expected*, *observed* (locations, error text, masked screenshot, sanitized DOM), `retryable` |
| **gated** | 05 bad input; approval replays 01 draft, 02 no approval | `INVALID_INPUT` / `NOT_APPROVED` / `APPROVAL_REQUIRED`, with zero UI steps |

- Recovery is bounded: per-outcome attempts, and at most two restarts.
- **A restart is only allowed while every executed step is idempotent.** A step classified irreversible at runtime
  becomes non-idempotent even if the artifact understated it, so recovery cannot double-submit.
- `money` is parsed as a whole-string format into an exact `Decimal`, so `E1001` fails extraction instead of becoming
  a balance.
- The checkpoint and every declared output are verified at the end.
- Using a fallback locator emits a `drift` event naming the failed and the used strategy.
- The evidence generator *asserts* each scenario's status, value, recovery path, drift count and redaction. It
  refuses to publish on any failure, and records the SHA-256 of the capability it replayed.

## 4. Heterogeneity & multi-tenant

`surface/base.py` defines the seam: observe, describe, resolve, act, state probes, masked evidence, and handoff hooks.
The artifact format, conditions, outcomes and result contract are surface-agnostic. Locator kinds are an open union:
a kind a surface cannot handle fails and the next strategy is tried.

**Only web is built, and the seam is not yet clean.** Discovery still holds Playwright element handles, and the
runtime catches Playwright errors; both must move behind the protocol before a second adapter exists.

- **Windows desktop:** a UI Automation adapter would map role → ControlType, name → Name/LabeledBy,
  `attr` → AutomationId, `table_cell` → Grid/Table patterns, frame → window. "Settled" would mean UIA structure
  events have gone quiet.
- **Citrix / terminal screens:** an OCR adapter would need text anchors with relative offsets, plus its own
  reliability evaluation before any capability is approved.

**Reuse across tenants.** Tenants configure *labels*, not internals. So semantic locators come first and
vendor-internal attributes (field names, routes) are the fallback, which degrades gracefully across tenants of one
product.

- The capability recorded on Harbor 4.2.1 runs unchanged on Summit 4.3.0 (four relabeled controls). It succeeds via
  fallbacks with exactly four drift events (scenario 13).
- Summit's patch in `tenants/summit.yaml` is a JSON merge patch keyed by **stable target names** and pinned to
  `applies_to: 1.x`. With it, drift is zero (scenario 14).
- If the base changes major version or renames a patched target, the patch **fails closed** pending review.

At scale (designed, not built): drift events keyed by (capability, tenant, target) carry the new name, so recurring
fallbacks become *proposed* patches. A tenant version bump triggers sandbox canary replays. Unknown states are never
auto-patched. A capability is written once per product and patched only where a tenant diverges.

## 5. Escalation & handoff

**Detection.**

| Where | What triggers a hand-off |
|---|---|
| Discovery | the model calls `escalate`; three no-effect clicks; three invalid or identical calls; oscillation between two or three screens; any irreversible action |
| Replay | `UNRECOGNIZED_STATE`; `TARGET_NOT_FOUND`/`AMBIGUOUS`; `TIMEOUT` after restarts; an irreversible step without approval |

The oscillation rule came from a real failed run, kept in the evidence, in which the model bounced between two menus
for all 25 steps.

An `InterventionRequest` carries: the subject, step and intent, the reason, every frame's location, on-screen errors,
a masked screenshot, the proposed action, and the allowed resolutions.

**Control-transfer model** (`control.py`). The live session has exactly one controller:
`AUTOMATION → AWAITING_HUMAN → HUMAN → AUTOMATION`. Every transition is validated and logged.

- Automation calls `ensure_automation()` before every action, so it cannot act while a human holds the session.
- The operator works in the **same browser context**: cookies, frames, and half-filled forms are all preserved.
- Every human click and change is captured in-page by the model's own descriptor code. In discovery these become
  recorded steps (`actor: human`). Typed values become parameters, registered for redaction before logging.
- On `release(decision)` (resume / approve / deny / completed / abort), automation **re-classifies the screen** rather
  than assuming the fix worked.

**Operators.** `TerminalOperator` lets a person drive the headed browser. `ScriptedOperator` is a labeled
*simulated* human whose input goes through the real page.

**Evidence of hand-off.**
- Replay 12: an unknown survey page; the operator clicks *Not Now* and hands back.
- The open-account discovery stops *Submit* for approval.
- Approval replays: a draft is blocked; a missing approval is blocked with zero UI steps; an approved run commits
  exactly once, for a different member.

**Not built.** The approver is simulated and unauthenticated. Production would add:
- a CDP-screencast console behind an intervention queue, with leased claims, heartbeats and expiry;
- `needs_human` reported to the waiting caller;
- authenticated four-eyes approval bound to the exact action and arguments.

## 6. Safety

**The allowlist** (origins, path globs, denied paths, action types) is enforced at three independent points:
1. the pre-action gate;
2. browser request routing, which aborts anything outside the allowlist, whoever initiated it;
3. a post-step location check.

`/__admin` (fault injection) and `/logout` are denied.

**Risk** is the maximum of: the action's base risk, keyword rules on the control's accessible name, keyboard rules,
and the declared risk.
- Enter and Space inherit the risk of the control and of the form's submit button.
- Unknown shortcuts count as irreversible.
- Untargeted key presses are rejected.
- The model can raise risk but **never lower it**: a "Submit" declared `safe` is still irreversible.

**Irreversible actions are blocked until approved:** in discovery by a human, in replay by an explicit caller
approval or an operator. With neither, nothing executes. `on_irreversible: block` overrides even caller approval. I
chose blocking over flagging because a flag on a committed transaction is just an incident report.

**Data.**
- Credentials exist only as `SecretRef` values resolved through the tenant secret store. Password values are masked
  inside perception itself.
- The redactor covers every persisted line, result and DOM copy, in three layers:
  1. **Known values:** secrets, pii/financial inputs and outputs, and values next to PII labels or in PII columns (the
     only reliable way to catch names), plus their HTML-escaped forms.
  2. **Patterns:** SSNs, Luhn-valid card numbers, email, phone, amounts, long numbers, URL tokens.
  3. **Masked screenshots.**
- Failure DOMs are sanitized copies without scripts or form values.
- Discovery logs omit the raw goal and model prose.
- Artifacts hold no recorded values.
- Playwright traces are off, because they embed raw DOM and network bodies.

**Limits.**
- Discovery shows the model unredacted screens, so it belongs on sandbox tenants with synthetic data. Production only
  replays, with no model in the loop. That is a security property of record/replay, not just a cost saving.
- Redaction is best effort beyond the supported layouts.
- Keyword risk rules can miss oddly named commit controls; per-route annotations plus review are the remedy.
- YAML approval and caller flags are developer controls, not production identity or consent.
- Callers receive unmasked outputs and own their logging.

## 7. Cuts

**Built thin, on purpose:**
- a terminal operator plus scripted operators for evidence;
- web only;
- the `env:` secret scheme;
- files as the catalog;
- no workers or queue.

**Left out:**
- desktop and OCR adapters;
- LLM recovery for a failed replay step;
- trace pruning;
- automatic outcome learning (discovery only sees the happy path, so the draft says so and a reviewer adds outcomes);
- reliability scores;
- per-route risk annotations;
- evidence encryption;
- a co-browsing console.

**Next, in order:**
1. Bind approvals to an authenticated identity and the exact arguments.
2. Remove Playwright types above `surface/`.
3. Outcome probing: replay synthetic invalid inputs to propose outcome stanzas for review.
4. Drift aggregation that proposes tenant patches.
5. Multi-run stability scores gating approval.
6. A UIA adapter.

**Stretch goals done:** an agent-facing catalog and `invoke`; the `draft → approved` gate; canonicalization plus
cross-tenant reuse with reviewed overrides.
