# cuflow — reusable computer-use capabilities

An LLM operates a legacy back-office UI once. The successful trace becomes a typed,
reviewable capability. Subsequent invocations replay that capability without model calls.

The target is **MockCore Teller**, a local synthetic banking app with framesets, layout
tables, inferred labels, per-render URL tokens, two tenant variants and injectable faults.
No real bank systems or customer data are used.

- [Design and trade-offs](REPORT.md)
- [Evidence and verification](evidence/README.md)
- [Discovered artifact](evidence/artifacts/lookup-discovered.yaml),
  [reviewed artifact](evidence/artifacts/lookup-reviewed.yaml),
  [review diff](evidence/artifacts/lookup-review.diff)

## Setup

Tested on Python 3.13. Python 3.11+ is required. Run commands from the repository root.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lock.txt
pip install --no-deps -e .
playwright install chromium
cp .env.example .env
```

The lock file records the tested dependencies. On Linux, Chromium may also need
`playwright install --with-deps chromium`.

Only **discovery** needs API access. Put a funded `GOOGLE_API_KEY` in `.env`, or use
`ANTHROPIC_API_KEY` with `--provider claude`. The CLI normally preserves shell environment
values; `scripts/discover_demo.py --prefer-dotenv` explicitly prefers the selected key in
`.env`. Keys are never printed or included in the repository. An HTTP 402 is a provider
billing failure and is recorded as `MODEL_ERROR`; it does not count as successful discovery.

Keep the synthetic demo credentials in `.env.example` for these demos. In separate terminals:

```bash
source .venv/bin/activate
python -m mockcore --tenant harbor --port 8765
```

```bash
source .venv/bin/activate
python -m mockcore --tenant summit --port 8766
```

The UI is at `http://127.0.0.1:8765` (user `teller01`, demo password in `.env.example`).
Bind these synthetic demo servers to localhost only.

## Replay the supplied capability — no model access needed

The approved `mockcore.member.get_savings_balance@1.1.0` was reviewed from the genuine
Gemini discovery in `evidence/discovery/`. The hand-authored test fixture is separate.

```bash
cuflow show mockcore.member.get_savings_balance --tenant harbor
cuflow replay mockcore.member.get_savings_balance --tenant harbor -p member_id=10051
cuflow replay mockcore.member.get_savings_balance --tenant harbor -p member_id=99999
cuflow replay mockcore.member.get_savings_balance --tenant harbor -p member_id=12AB
cuflow replay mockcore.member.get_savings_balance --tenant summit -p member_id=10042
```

These demonstrate a different member with reordered account rows, `MEMBER_NOT_FOUND`,
input validation before UI interaction, and the same capability on a tenant with four
reviewed label overrides. Bad-input commands deliberately exit nonzero.

The agent-facing interface returns JSON:

```bash
cuflow catalog --tenant harbor
cuflow invoke mockcore__member__get_savings_balance --tenant harbor --args '{"member_id":"10051"}'
```

The caller receives real synthetic output values; persisted evidence masks those values.
Treat caller stdout appropriately if redirecting it to your own logs.

## Discover a goal, then replay its actual artifact

This helper uses the real configured model and starts an isolated local MockCore instance.
It does not provide a scripted-model fallback. Every attempt has its own evidence directory.

```bash
mkdir -p runs
python scripts/discover_demo.py --scenario lookup --prefer-dotenv > runs/lookup-discovery.json
```

After that command succeeds, replay its actual draft against the Harbor server above:

```bash
RUN_DIR=$(python -c 'import json; print(json.load(open("runs/lookup-discovery.json"))["evidence"])')
cuflow replay "$RUN_DIR/artifact.yaml" --tenant harbor --allow-draft -p member_id=10051
```

The draft covers the observed happy path. Inspect it, then apply the narrowly scoped demo
review: five-digit member validation, two business outcomes, removal of positional CSS
fallbacks, and the Summit label patch. This preserves model/run provenance and writes a diff.

```bash
python scripts/review_demo.py "$RUN_DIR" --reviewer "$USER"
python scripts/make_evidence.py
```

For an arbitrary goal, use the underlying CLI. `--dry-run` saves the draft in the run's
evidence directory without publishing it into the catalog:

```bash
cuflow discover --tenant harbor --goal "Look up member 10042 and read their current Share Savings balance" --dry-run
```

## Human takeover and irreversible actions

Inject the unknown survey interstitial, then attach a person to the same live browser:

```bash
curl -fsS "http://127.0.0.1:8765/__admin/faults?set=survey_popup"
cuflow replay mockcore.member.get_savings_balance --tenant harbor -p member_id=10042 --headed --operator terminal
```

Type `claim` in the terminal, click **Not Now** in the browser, then type `resume`.
The control transitions and manual actions are recorded. A reproducible simulated operator
is also available; its identity is explicitly prefixed `simulated:`:

```bash
cuflow replay mockcore.member.get_savings_balance --tenant harbor -p member_id=10042 --operator scripted:examples/operator_dismiss_survey.json
curl -fsS "http://127.0.0.1:8765/__admin/reset"
```

The second genuine discovery demo opens a synthetic sub-account and uses a **simulated**
supervisor to exercise approval transfer on that same session:

```bash
python scripts/discover_demo.py --scenario open-account --prefer-dotenv
```

Its draft remains unapproved. Replaying an irreversible artifact also requires either
explicit caller approval (`--approve`) or an attached operator. Clicking Submit and pressing
Enter/Space on it pass through the same risk gate. Keyboard actions require an explicit
target; implicit focus is rejected. A policy configured to `block` cannot be overridden by
caller approval. These gates are application logic, not a production authorization service.

## Tests and evidence

```bash
pytest -q --junitxml=runs/pytest.xml
python scripts/make_evidence.py
python scripts/verify_submission.py --require-live --test-report runs/pytest.xml
```

Tests use scripted model decisions and local HTTP servers; they make no external model
calls. The evidence generator runs 15 replay scenarios on isolated ephemeral ports and
checks outputs, business/error codes, recovery paths, intervention, drift and text redaction.
It replaces the published replay evidence only after all checks pass. The final verifier
requires successful genuine discovery, a matching reviewed artifact, and its replay evidence.
It does not make model calls or publish the repository.

Regression tests cover keyboard approval bypasses, malformed numeric outputs, PII in
failure DOMs, discovery logs, operator inputs, and the actual shipped tenant override.
Screenshots are also inspected manually. See [evidence/README.md](evidence/README.md) for the
recorded verification results and limitations.

## Code map

`discovery.py` owns observe/decide/act; `compiler.py` and `targets.py` compile traces;
`schema.py` defines the contract; `replay.py` and `conditions.py` execute it; `policy.py`,
`control.py`, `redaction.py` and `evidence.py` handle risk, ownership and audit records.
`surface/web.py` and `perception.js` implement the current browser adapter. The proposed
surface seam and remaining Playwright coupling are described honestly in `REPORT.md`.
