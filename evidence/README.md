# Verified demonstration evidence

All application data is synthetic. Model discovery is genuine; simulated operators are
explicitly labelled. The approved lookup capability is derived from the current successful
lookup run below, not from the hand-authored test fixture.

## Current end-to-end runs

| Evidence | What it establishes |
| --- | --- |
| [Lookup discovery](discovery/lookup-discovery-20261108-003201-3d3c/) | Gemini drives the UI, reads the balance, verifies the final screen and compiles a draft. |
| [Open-account discovery](discovery/open-account-discovery-20261108-003207-3960/) | Gemini completes the form; policy stops Submit for approval; a simulated supervisor approves on the same live session. |
| [Discovered lookup](artifacts/lookup-discovered.yaml) | Original typed artifact, including model and run provenance. |
| [Reviewed lookup](artifacts/lookup-reviewed.yaml) and [review diff](artifacts/lookup-review.diff) | Five-digit input validation, business outcomes, and removal of positional CSS fallbacks. |
| [Replay matrix](replays/README.md) | Fifteen asserted scenarios using the reviewed, model-discovered lookup. |
| [Approval replay checks](approval-replays/verification.json) | Draft gate and approval gate block with no UI steps or commits; approved replay commits exactly once for a different synthetic member. |
| [Agent-facing invocation](invocation.json) | The actual `cuflow invoke` CLI returns the expected typed output without a model call. |
| [Final verification](verification.json) | Local deliverable checks, genuine provenance linkage, replay count and pytest results. |

Each discovery folder has `events.jsonl`, redacted `llm/*.json`, masked `screens/`,
`trace.json`, `result.json` and, on success, `artifact.yaml`. Each replay folder has a
structured result and event log, with screenshot/DOM attachments where appropriate.

The lookup's five actions are followed by a model `done` call. The open-account artifact
has nine actions, with Submit recorded as irreversible and non-idempotent. Approval is
simulated; this does not claim an authenticated independent human authorized a real account.

Latest full validation: **82 tests passed** on 2026-11-08, plus 15 lookup replay scenarios,
three approval replay checks and the agent-facing CLI invocation.

## Reproduce and verify

From the repository root, after setup:

```bash
pytest -q --junitxml=runs/pytest.xml
python scripts/make_evidence.py
python scripts/make_approval_evidence.py evidence/discovery/open-account-discovery-20261108-003207-3960
python scripts/verify_submission.py --require-live --test-report runs/pytest.xml
```

The replay generator starts isolated local servers and checks expected statuses, output
values against synthetic records, recovery paths, intervention, four fallback drift events
without the Summit patch and zero with it. A failing check prevents replacement of the
published replay evidence. Its [verification record](replays/verification.json) includes a
SHA-256 of the exact capability consumed.

The test suite includes regressions for Space/Enter approval bypasses, implicit keyboard
focus, strict amount parsing, PII in failure DOMs, discovery/operator log redaction, two-screen
navigation cycles, reserved event fields, and token disappearance after manual recovery.
The shipped catalog and Summit configuration are exercised directly.

## Earlier attempts retained for transparency

- [Provider billing failure](discovery/lookup-discovery-20261108-002552-a4ef/): HTTP 402,
  `MODEL_ERROR`, zero model decisions. This is not successful discovery.
- [Earlier lookup success](discovery/lookup-discovery-20261108-002817-6f12/): genuine discovery
  before the transient-token checkpoint improvement; not the source of the current catalog.
- [Earlier account-opening failure](discovery/open-account-discovery-20261108-002847-e24e/):
  the model alternated between two menus until the step budget expired. Cycle detection was
  added, the task wording clarified to look up the member first, and the later genuine run
  above succeeded. The failed run remains unchanged.

## Evidence boundaries

Logs and DOMs mask observed identifiers, names, addresses, credentials, amounts and transient
URL tokens. Raw goal text and model prose are omitted from persisted discovery prompts;
exact original prompts therefore cannot be reconstructed from these redacted files.
Screenshots mask member data and amounts; UI labels, synthetic account suffixes and demo
operator identity remain visible. Representative final, failure and intervention screenshots
were visually inspected in addition to automated text checks.

These checks demonstrate a local synthetic prototype, not production authorization,
universal PII detection or a statistical reliability claim. Repository visibility and email
submission are separate from the local verification script.
