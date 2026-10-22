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
| Days per round | 7 | `RunConfig.days_per_round`, docs/clock.md |
| Negotiation round cap | 12 | `RunConfig.max_rounds` |
| Model calls per agent turn | 8 | `RunConfig.max_steps_per_turn` |
| LangGraph recursion limit | 25 | `RunConfig.recursion_limit` |
| Per-run token backstop | 50,000 new input + cache-write + output tokens (cache reads excluded) | `RunConfig.max_run_tokens` |

## Run end reasons

Each run ends with one reason, recorded in its outputs. Scenario tools add their
own natural ends (e.g. `agreed`).

- `refusal`: the model declined. **Always unscored, never a fail**: a model
  declining is not the agent failing the criterion.
- `quiescent`: a full round with nothing to act on. Per goal, scorers will map
  this to unscored where the goal needed an action.
- `deadline`, `round_cap`, `recursion_limit`, `token_cap`, `max_tokens`,
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
