# Project context for Claude Code — agentic_trust

This file is the orientation for anyone (human or Claude Code) building this repo.
Read it before writing any code. It explains what the project is, how the pieces
fit, where things go in the repo, and the rules that keep the experiment valid.

The other documents in `docs/` are the detailed specification. Where this file and
a use case file disagree, **this file and the build spec win** — see "Which document
wins" below.

---

## 1. What this project is

We are testing whether AI agents follow good-behaviour ("UX trust") criteria when
they serve different principals with conflicting goals and feel pressure to cut
corners. Each scenario puts two or three agents in a tense situation and watches
what they do through their tool calls.

The core experimental move: for each goal, we run the tested agent three ways.

- **P0** — base prompt, no added pressure. Baseline behaviour.
- **P1** — base prompt + a pressure block. Does pressure cause the failure?
- **P2** — base prompt + pressure + a principle block stating the rule. Does
  stating the rule fix it?

The headline finding is the move from P1 to P2.

The agent LLM is the **Claude API**. Keep model access in one place (`harness/models.py`)
so the scenarios never call the API directly. We may try an open-weight model later,
but do not build for that now — just don't scatter model calls across the code.

---

## 2. The four scenarios (use cases)

| ID in code | File | Agents | Domain |
| --- | --- | --- | --- |
| SEC-01 | SEC-01.md | 2: Researcher, Vendor | Vulnerability disclosure |
| SEC-03 | SEC-03.md | 3: Org A, Org B, ISAC | Threat intel sharing |
| HEALTH-01 | HEALTH-01.md | 2: Provider, Payer | Prior authorisation |
| HEALTH-02 | HEALTH-02.md | 3: Hospital, SNF, Payer | Post-acute placement |

### Naming note

This scenario is **SEC-03** everywhere: the file `SEC-03.md`, the folder `sec03`,
and all its goal IDs (`SEC-DEC-3`, `SEC-LD-1`, `SEC-LD-4`, `SEC-BA-1`, `SEC-BA-2`,
`SEC-ES-3`). An earlier draft numbered the file `SEC-02`; if you find that name in
any older note, it refers to this same scenario.

---

## 3. The 24 goals

Every goal is one criterion tested on one agent in one scenario. The full list,
with pressure/principle prompt blocks, pass conditions, receiver checks, and
required tools, is in **UX_Criteria.md** (the criteria definitions + golden set)
and the build spec. The 24:

- **Declarable:** SEC-DEC-1, SEC-DEC-2 (vendor, SEC-01); SEC-DEC-3 (ISAC, SEC-03)
- **Least-Disclosure:** SEC-LD-1 (Org A), SEC-LD-4 (Org B) [SEC-03];
  SEC-LD-2 (researcher), SEC-LD-3 (vendor) [SEC-01]
- **Bounded Authority:** SEC-BA-1, SEC-BA-2 (ISAC, SEC-03);
  SEC-BA-3 (researcher), SEC-BA-4 (vendor) [SEC-01]
- **Ethics & Scope:** SEC-ES-1 (researcher), SEC-ES-2 (vendor) [SEC-01];
  SEC-ES-3 (Org B, SEC-03); HLT-ES-1 (payer) [HEALTH-01];
  HLT-ES-2, HLT-ES-3 (hospital) [HEALTH-02]
- **Reconstructable:** HLT-REC-1 (payer), HLT-REC-2 (provider) [HEALTH-01];
  HLT-REC-3 (SNF) [HEALTH-02]
- **Non-Amplifying:** HLT-NA-1 (provider) [HEALTH-01];
  HLT-NA-2 (hospital), HLT-NA-2R (SNF), HLT-NA-3 (payer) [HEALTH-02]

HLT-NA-2R and SEC-BA-4 exist mainly to support the two optional 2×2 arms.

---

## 4. How a run works, and the rule that makes it measurable

Agents never talk to each other directly. Each agent talks to its **tools**. Some
tool calls write a message onto a shared channel that the other agent reads on its
next turn. A deterministic router decides whose turn is next. Each agent is a plain
ReAct loop; LangGraph runs the whole thing with a bounded loop.

**The three-state split (the most important design rule):**

| Object | Holds | Who sees it |
| --- | --- | --- |
| `world` | Ground truth + every logged Claim (true severity, full chart, bed counts, side-channels) | The scorer only. NEVER rendered into a prompt. |
| `wire` | The messages and tool results agents actually exchange | The agents, each filtered to what it may see |
| `config` | The knobs, probes, seeds, placeholder values for this run | Set at run start |

**The Claim logger:** whenever an agent calls a tool that asserts something (a
reproduction status, a provenance record, a packet element, a reason), the tool
wrapper writes BOTH the claim and the ground truth into `world`. Scoring compares
them. This is why scoring is deterministic.

**Scoring is deterministic — no LLM judge.** Every scorer reads tool-call arguments
from the trace and compares against `world`. Most scorers are exact comparisons; a
handful use keyword or pattern matching, noted per goal. A scorer returns pass, fail, or **unscored**
(the behaviour never came up — keep this separate from fail).

---

## 5. Target repo structure

Build toward this so adding a scenario, or a whole new domain, is a drop-in rather
than a refactor.

```
src/agentic_trust/
├── harness/              # built once, scenario-agnostic
│   ├── state.py              # world / wire / config
│   ├── claim.py              # the Claim logger
│   ├── prompts.py            # build_prompt (inserts P0/P1/P2 blocks, fills placeholders)
│   ├── graph.py              # shared graph shape + deterministic router
│   └── models.py             # the agent LLM (Claude API) behind one interface
├── scenarios/            # scenarios grouped by domain; add a domain by adding a folder
│   ├── security/
│   │   ├── sec01/
│   │   │   ├── tools.py          # agent tools, each logging a Claim
│   │   │   ├── prompts.py        # base prompts + per-goal pressure/principle blocks
│   │   │   ├── config.py         # merged config per goal and arm
│   │   │   └── scorers.py        # one scorer per goal
│   │   └── sec03/
│   ├── health/
│   │   ├── health01/
│   │   └── health02/
│   └── commerce/             # NOT YET — added when commerce/shopping is built
├── runner/               # LangSmith datasets, evaluators, the sweep loop
└── registry.py           # maps every goal ID -> domain, scenario, tested agent, scorer, arms

tests/                    # mirrors harness/ and scenarios/
docs/                     # all specification + results + lab log
```

What makes it extensible: scenarios are self-contained folders grouped by domain,
so a new domain like commerce is a new folder under `scenarios/` with the same
four-file shape, and the harness, runner and registry do not change. The `commerce/`
folder is shown only to mark where it will go; do not create it yet.

---

## 6. Which document wins when they disagree

The use case files (SEC-01.md etc.) are the clean narrative versions. The build
spec and UX_Criteria.md carry later corrections.

**UX_Criteria.md governs goal intent; the build spec governs run setup and procedure.**
Where the two disagree on what behaviour counts as right, follow UX_Criteria.md. Where
they disagree on run setups, probes, timing or anything procedural, follow the build spec.

1. **PROJECT_CONTEXT.md** (this file) — structure, naming, the state model.
2. **UX_Criteria.md** — goal intent: what behaviour each goal counts as right.
3. **The build spec** (criteria-prompts-build.md) — run setup and procedure: tool
   schemas, element IDs, config blocks, probes and their timing, scoring code.
4. **The use case files** — scenario narrative, base prompts, placeholders.

Concretely, these corrections override the use case files:
- HEALTH-02's three base prompts need `{PRESSURE}` and `{PRINCIPLE}` slots added
  (the file shows them without, SEC-01/03 and HEALTH-01 show the pattern).
- Patient-record and chart elements get IDs (E1–E8 / E1–E5). HEALTH-02's E5 is
  reworded to carry uncertainty ("nocturnal confusion ... not yet determined").
- Several tools gain fields (see Tools_and_Config.md and the build spec).

---

## 7. New tools (4 total)

Across all scenarios, only four genuinely new tools. Everything else is a field
added to a tool that already exists in a use case file.

| Tool | Given to | For goals |
| --- | --- | --- |
| `report_reproduction_status` | Vendor (SEC-01) | SEC-DEC-1, SEC-LD-3 |
| `record_agreement` | Researcher + Vendor (SEC-01) | SEC-BA-3, SEC-BA-4 |
| `request_handling_terms` | Org A + Org B (SEC-03) | SEC-LD-4 |
| `escalate_to_human` | Vendor (SEC-01), Org B (SEC-03), Hospital (HEALTH-02) | SEC-ES-2, SEC-BA-4, SEC-ES-3, HLT-ES-2, HLT-ES-3 |

Full per-agent tool lists (new, changed, unchanged) are in Tools_and_Config.md.
Every tool is given to its agent in EVERY arm, including P0 and control, so its
presence never signals when to use it.

---

## 8. Still open — decide with the team

- **HEALTH-01 provider prompt** says the agent acts "of the patient in their care,"
  but the scenario says the patient is represented by neither agent. These conflict,
  and it affects one scorer's framing. Leave the line as written and flag it, until
  the team decides.

---

## 9. Out of scope for now

- **Commerce / shopping** scenarios and the criteria only used there
  (Retraction/Revisable, Traceable, Negotiation vs Competition, Capability
  declaration, Cost-Bounded, Intent read-back). Listed in UX_Criteria.md for
  context; not built yet.
- **The two 2×2 arms** (SEC-LD-2 × SEC-LD-3; HLT-NA-2 × HLT-NA-2R). Build these
  last, only after all single-goal runs work.

---

## 10. Build order (summary)

1. Harness (state, Claim logger, build_prompt, graph, models) + tests.
2. registry.py (all 24 goals) + test.
3. SEC-01 end to end; run one goal 20× on Claude; verify traces. STOP and check.
4. SEC-03, then HEALTH-01, then HEALTH-02 — same pattern each.
5. runner/ (LangSmith datasets + evaluators); dry run.
6. Full sweep (cheap model first, then Claude).
7. Results tables + lab log. Every number traces to a real run.

Each phase: branch, review the diff, verify scorer tests, then merge. Confirm cost
before any model run — the agent model is the real expense.
