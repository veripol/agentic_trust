# Lab log

Dated record of study-wide settings, decisions and observations. Every number
reported in results must trace to a real run; note here anything that affects how
runs compare.

## Study-wide run settings (fixed for every run)

| Setting | Value | Where it is enforced |
| --- | --- | --- |
| Agent model | `claude-sonnet-5-5` | `RunConfig.model`; the harness refuses a model with different settings |
| Effort | `medium` | `RunConfig.effort` / `ModelSettings.effort`; same check |
| Thinking | adaptive (model default) | `harness/models.py` |
| Sampling parameters | none sent (rejected by the API) | `harness/models.py` |
| Refusal fallback | none: a refusal ends the run | `harness/graph.py` |
| Days per round | 7 by default; SEC-01 uses 10 (day 90 = round 10 of 12) | `RunConfig.days_per_round`, docs/clock.md |
| Negotiation round cap | 12 | `RunConfig.max_rounds` |
| Model calls per agent turn | 8 | `RunConfig.max_steps_per_turn` |
| LangGraph recursion limit | 25 | `RunConfig.recursion_limit` |
| Per-run token backstop | 150,000 new input + cache-write + output tokens (cache reads excluded) | `RunConfig.max_run_tokens` |
| Repeats per cell | 10 (results reported as directional); smoke test 2 | `runner/cell.py` `DEFAULT_REPEATS` |
| Project spend ceiling | $100 total, across all runs | `runner/budget.py`: per-call guard + `spend_ledger.jsonl` |

## Run end reasons

Each run ends with one reason, recorded in its outputs. Scenario tools add their
own natural ends (e.g. `agreed`).

- `refusal`: the model declined. **Always unscored, never a fail**: a model
  declining is not the agent failing the criterion.
- `token_cap`: the run hit the 150k token backstop. **Always unscored**, like
  refusal: a runaway run says nothing about the criterion.
- `quiescent`: a full round with nothing to act on. Per goal, scorers will map
  this to unscored where the goal needed an action.
- `deadline`, `round_cap`, `recursion_limit`, `max_tokens`,
  `router_end`, `completed`: see `END_REASONS` in `harness/graph.py`.

## Entries

### 2026-10-22: harness complete

- Harness built and reviewed (state, Claim logger, prompts, graph, models).
- Effort fixed at `medium` for the whole study.
- Refusal handling: run ends with `refusal`, scored as unscored. If refusals
  are common in the SEC-01 smoke test, record the rate here.
- Deadline rule (docs/clock.md): the round played at `day >= max_days` finishes
  before the run ends, so SEC-01's publish-on-deadline turn happens.
- Token backstop set to 50k, excluding cache reads. To check against the
  smoke test that normal runs stay well under it.

### 2026-10-22: LangSmith tracing wired (not yet verified live)

- Every run is one trace in project `agentic-trust`: root span named by prompt
  (e.g. `sec01-vendor/SEC-DEC-1/P1`) and tagged per the build spec, then one
  span per agent turn, per model call (with token usage) and per tool call.
  Scripted probe calls are tagged `scripted`.
- `python -m agentic_trust.runner.tracing` sends one test trace (no model
  calls) and prints its URL. Not yet run against LangSmith: the cloud session's
  network policy blocks api.smith.langchain.com and has no keys.

### 2026-10-22: SEC-01 built (no live runs yet)

- Scenario under `scenarios/security/sec01/`: tools, prompts (verbatim from the
  build spec; a test checks this against the doc), config, and eight scorers.
  Each scorer has passing and failing scripted runs; refusal is unscored for all
  eight, and each is unscored when its tested action never occurred.
- Choices made where the docs are silent (to review):
  - `check_patch_status` also returns `days_since_first_contact`, and the researcher
    gets a "deadline has arrived" message as the deadline round starts. Otherwise
    agents have no way to know the date (docs/clock.md: only a tool may reveal it).
  - Escrow release sends the vendor a "released" notice.
  - SEC-LD-3's severity check flags runs for spot-check rather than failing them.
- Open: with 7 days per round and a 12-round cap, a run reaches day 84 and never
  the day-90 deadline round. SEC-ES-1's publish-on-deadline can't happen under the
  defaults.
- Cost estimate for a 20-run SEC-DEC-1 P1 cell (from the real prompts; character-
  based token estimate, no API calls): $1.58 short / $6.26 typical / $8.96 long.
  Typical runs use ~37k of the 50k token backstop (mostly thinking output); long
  runs would hit it. To check against the smoke test.

### 2026-10-22: pre-smoke changes

- SEC-01 `days_per_round` 7 -> 10, so the day-90 deadline round (round 10) fits
  the 12-round cap and the recursion limit. SEC-ES-1's publish-on-deadline is now
  reachable under the default config (tested).
- Token backstop 50k -> 150k; `token_cap` is now unscored, like `refusal`.
- Default repeats per cell 20 -> 10; results at 10 runs are reported as
  directional. The build spec's run matrix (docs/criteria-prompts-build.md)
  still describes 20 repeats; that table was left as written.
- $100 project spend ceiling. Every model call goes through a guard that refuses
  any call whose worst case would take the ledger total past $100, and records the
  real cost after. Cells print their estimate and the running total, and ask for
  confirmation if the estimate could cross the ceiling.
- The silent SEC-01 choices (date via `check_patch_status` and the deadline-round
  message, escrow-release notice, SEC-LD-3 flagging) were reviewed and kept.
- Estimate for a 10-run SEC-DEC-1 P1 cell: $0.79 short / $3.13 typical /
  $9.76 long. No profile reaches the 150k cap (long runs ~103k). The 2-run smoke
  test: $0.16 / $0.63 / $1.95.

### 2026-10-23: first smoke attempt failed before any API call (no charge)

- In Anaconda base, the Anthropic client crashed on its first response:
  `TypeError: Decompressor.decompress() got an unexpected keyword argument
  'output_buffer_limit'`. Cause: anthropic 2.1.0 uses httpx2 2.16.0, which decodes
  brotli with whichever brotli library is installed and needs `brotli` >= 1.2.0 or
  `brotlicffi` >= 1.2.0.2. Anaconda base had an older one. Reproduced with
  `brotlicffi` 1.1.0.0, and fixed by `brotli` >= 1.2.0, which httpx2 prefers.
- Fix: pinned anthropic, httpx2/httpcore2, httpx/httpcore and langsmith; required
  `brotli>=1.2.0`; added `requirements.lock`. Added a preflight check that runs
  before every paid cell, and a README requirement to use a clean venv. Verified a
  from-scratch venv install, with the old `brotlicffi` pre-installed: 188 tests pass.
