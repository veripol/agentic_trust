# agentic_trust
Sentient Futures project 2026

Do AI agents follow UX trust criteria when they serve principals with conflicting
goals and face pressure to cut corners? See `docs/PROJECT_CONTEXT.md`.

## Setup

**Use a clean virtual environment.** Don't install into Anaconda's `base`
environment or any other shared environment. The HTTP stack is pinned (see
`pyproject.toml`), and a shared environment can shadow those pins. The first smoke
test failed this way: an old `brotlicffi` in Anaconda base crashed the Anthropic
client before any request was charged (`TypeError: Decompressor.decompress() got
an unexpected keyword argument 'output_buffer_limit'`).

Requires Python 3.11 or newer.

```bash
conda deactivate                      # if conda is active; leave base entirely
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.lock      # exact versions the tests run on (includes pytest)
pip install --no-deps -e .
```

`pip install -e ".[dev]"` also works. It honours the pins in `pyproject.toml` but
leaves the other dependencies free to float.

Check the environment, then the tests:

```bash
python -m agentic_trust.runner.preflight   # pinned versions, venv check; no network
pytest
```

Every paid run repeats the preflight check and stops before calling any API if it fails.

## Keys

Copy `.env.example` to `.env` and add `ANTHROPIC_API_KEY` and `LANGSMITH_API_KEY`.
`.env` is gitignored. To confirm that tracing reaches the `agentic-trust` project
(no model calls):

```bash
python -m agentic_trust.runner.tracing
```

## Running

```bash
python -m agentic_trust.runner.smoke_sec01           # SEC-DEC-1 at P1, 2 runs
python -m agentic_trust.runner.cell SEC-DEC-1 P1     # one cell, 10 runs by default
```

`--repeats N` is the number of completed runs a cell aims for. A run the model
refuses is unscored and re-run under a new run id, up to 2×N attempts in total.
Every attempt is saved, and each cell writes `cell.json` with its refusal rate
and appends that rate to `docs/lab_log.md`.

Each run prints the estimated cost and the project's total spend so far, then asks
before spending. Total spend is capped at $100. Every model call is recorded in
`spend_ledger.jsonl`. Commit that file after runs, so the cap still counts this
spend after a fresh checkout.

## Sweep

```bash
python -m agentic_trust.runner.sweep --plan   # cells, runs to go, cost estimate; no keys, no spend
python -m agentic_trust.runner.sweep          # run it: asks once, then writes the digest
python -m agentic_trust.runner.sweep --budget 20   # this invocation spends at most $20
```

`--budget USD` caps what one invocation spends. The $100 project ceiling still
applies on top. Before each run, the sweep reserves the estimated cost of a long
run for that cell and stops cleanly if it would not fit. Every model call is also
checked first against the most it could cost. A stop therefore never overshoots
and never leaves a run half-paid. A PARTIAL digest is written; rerun with a new
`--budget` to continue.

The SEC-01 sweep runs 25 cells: one shared P0 cell, then goal by goal. The shared
cell scores every goal that has no probe or arm change. Probe goals and control
arms get P0 cells of their own. Every goal and arm has P1 and P2 cells. Each cell
targets 10 completed runs, with refused runs re-run. The sweep prints a running
tally after every run and stops cleanly at the $100 ceiling. It is resumable:
rerun the same command and it skips complete cells and continues partial ones from
their saved run files, so nothing is paid twice. Raw runs go to `runs/sweep/sec01/`
(gitignored). The digest goes to `docs/results/sec01.md` (tracked; commit it).

## Results

After a use case's sweep, write its digest from the saved run files (no API calls):

```bash
python -m agentic_trust.report sec01                                # runs/ -> docs/results/sec01.md
python -m agentic_trust.report sec01 --runs runs/smoke --sample     # -> docs/results/_sample_sec01.md
```

The digest has a summary table (passes as N/scored at P0/P1/P2, refusal rate,
unscored count), a paragraph per goal, and one narrated example run per goal. It
recomputes every verdict from the saved files. Every narrated line is tagged with
the tool call, claim, refusal or end reason it came from, and a Sources table lists
every file used. Incomplete files are listed as skipped and never counted.

## Updating the pins

After changing dependencies in `pyproject.toml`, regenerate the lock file and run
the tests:

```bash
uv pip compile pyproject.toml --extra dev --python-version 3.11 --universal -o requirements.lock
```
