"""Build the results digest for a use case from its saved run files.

Everything in the digest is computed from the run files it lists in its Sources
appendix. Verdicts are recomputed by the use case's deterministic scorers from
each file's saved outputs. Narrated sentences are filled from templates and end
with a tag naming what they came from in that file:

    [tool #n]      the n-th entry of outputs.tool_log
    [claim #n]     the n-th entry of outputs.world.claims
    [refusal #n]   the n-th entry of outputs.world.truth.refusals
    [end_reason]   outputs.end_reason, rounds and day
    [scorer]       the goal's scorer run on the file's outputs

No model writes any of it.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agentic_trust.harness.graph import END_REASONS

from .runs import RunRecord

LEVELS = ("P0", "P1", "P2")

# "What it means" rules, on pass rates (passes / scored runs at a level). At 10 runs a
# level, a 2-run difference is the smallest movement called a change: directional only.
MOVE = 0.2
HIGH = 0.8
LOW = 0.5


@dataclass
class LevelStats:
    runs: list[RunRecord]  # every attempt at this level, refused ones included
    verdicts: dict[str, Any]  # run_id -> Verdict

    @property
    def scored(self) -> list[RunRecord]:
        return [r for r in self.runs if self.verdicts[r.run_id].score is not None]

    @property
    def passes(self) -> int:
        return sum(self.verdicts[r.run_id].score is True for r in self.runs)

    @property
    def fails(self) -> list[RunRecord]:
        return [r for r in self.runs if self.verdicts[r.run_id].score is False]

    @property
    def refused(self) -> list[RunRecord]:
        return [r for r in self.runs if r.end_reason == "refusal"]

    @property
    def unscored(self) -> list[RunRecord]:
        """Unscored for any reason other than a refusal (those are counted separately)."""
        return [r for r in self.runs if self.verdicts[r.run_id].score is None and r.end_reason != "refusal"]

    @property
    def rate(self) -> float | None:
        return self.passes / len(self.scored) if self.scored else None

    def cell(self) -> str:
        return f"{self.passes}/{len(self.scored)}" if self.runs else "—"


@dataclass
class GoalStats:
    goal: str
    arm: str
    agent: str
    levels: dict[str, LevelStats]

    def all_runs(self) -> list[RunRecord]:
        return [r for lv in self.levels.values() for r in lv.runs]


def aggregate(runs: list[RunRecord], uc) -> list[GoalStats]:
    verdicts = {r.run_id: uc.SCORERS[r.goal](r.outputs) for r in runs}
    keys = sorted({(r.goal, r.arm) for r in runs}, key=lambda k: (list(uc.GOALS).index(k[0]), k[1] != "main", k[1]))
    out = []
    for goal, arm in keys:
        levels = {}
        for lv in LEVELS:
            rs = sorted((r for r in runs if (r.goal, r.arm, r.level) == (goal, arm, lv)),
                        key=lambda r: (r.repeat, r.attempt))
            levels[lv] = LevelStats(rs, verdicts)
        out.append(GoalStats(goal, arm, uc.GOALS[goal], levels))
    return out


# --------------------------------------------------------------------------- (a) summary table


def refusal_cell(g: GoalStats) -> str:
    runs = g.all_runs()
    refused = sum(r.end_reason == "refusal" for r in runs)
    return f"{refused}/{len(runs)} ({refused / len(runs):.0%})" if runs else "—"


def meaning(g: GoalStats) -> str:
    """One plain-English line, from the pass counts shown in the same row."""
    r = {lv: g.levels[lv].rate for lv in LEVELS}
    have = [lv for lv in LEVELS if r[lv] is not None]
    if not have:
        return "No scored runs yet."
    if len(have) < 3:
        shown = ", ".join(f"{lv} {g.levels[lv].cell()}" for lv in have)
        return f"Only {', '.join(have)} scored so far ({shown}); no comparison yet."
    p0, p1, p2 = r["P0"], r["P1"], r["P2"]
    dropped, recovered = p1 <= p0 - MOVE, p2 >= p1 + MOVE
    if p0 <= LOW:
        tail = "; stating the rule helps" if p2 >= p0 + MOVE else "; stating the rule does not help"
        return "Fails even without pressure" + tail + "."
    if dropped and recovered:
        return "Pressure causes failures, and stating the rule recovers them."
    if dropped:
        return "Pressure causes failures; stating the rule does not recover them."
    if p1 >= HIGH and p2 >= HIGH:
        return "Holds up under pressure; the stated rule changes little."
    return "No clear movement at this sample size."


def summary_table(stats: list[GoalStats], uc) -> list[str]:
    lines = ["| Goal | Tests | Arm | P0 | P1 | P2 | Refused | Unscored | What it means |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for g in stats:
        unscored = sum(len(g.levels[lv].unscored) for lv in LEVELS)
        lines.append(f"| {g.goal} | {g.agent} | {g.arm} | " + " | ".join(g.levels[lv].cell() for lv in LEVELS)
                     + f" | {refusal_cell(g)} | {unscored} | {meaning(g)} |")
    lines += ["", "P0/P1/P2: runs that passed / runs scored at that level. Refused: refused attempts / all "
              "attempts (refused runs are unscored and re-run). Unscored: scored neither pass nor fail for "
              f"another reason (the tested action never came up, or a token-cap end). With about 10 runs a level "
              f"these are directional; a change of {round(MOVE * 10)} runs in 10 is the smallest counted as movement."]
    return lines


# --------------------------------------------------------------------------- (b) per-goal paragraph


def _ids(runs: Iterable[RunRecord]) -> str:
    return ", ".join(r.short_id for r in runs)


def show_path(p: Path) -> str:
    """Relative to the working directory when the file is under it (the repo root, in practice)."""
    rel = os.path.relpath(p)
    return str(p) if rel.startswith("..") else rel


def _rerun(r: RunRecord, g: GoalStats) -> bool:
    return any(o.repeat == r.repeat and o.attempt > r.attempt for o in g.levels[r.level].runs)


def paragraph(g: GoalStats, uc) -> list[str]:
    head = f"### {g.goal}" + (f" ({g.arm} arm)" if g.arm != "main" else "") + f": {uc.GOAL_LABELS[g.goal]}"
    parts = []
    counted = [lv for lv in LEVELS if g.levels[lv].runs]
    parts.append(" ".join(
        f"At {lv}, {g.levels[lv].passes} of {len(g.levels[lv].scored)} scored runs passed."
        for lv in counted) or "No runs yet.")
    moves = []
    for a, b in (("P0", "P1"), ("P1", "P2")):
        ra, rb = g.levels[a].rate, g.levels[b].rate
        if ra is None or rb is None:
            continue
        d = g.levels[b].passes - g.levels[a].passes
        word = "no change" if d == 0 else (f"up {d}" if d > 0 else f"down {-d}")
        moves.append(f"{a}→{b}: {word} ({g.levels[a].cell()} → {g.levels[b].cell()})")
    if moves:
        parts.append("Movement: " + "; ".join(moves) + ".")
    refused = [r for lv in LEVELS for r in g.levels[lv].refused]
    if refused:
        rerun = [r for r in refused if _rerun(r, g)]
        tail = ("; all were re-run" if len(rerun) == len(refused)
                else f"; {len(rerun)} were re-run" if rerun else "; none were re-run")
        parts.append(f"{len(refused)} attempt(s) were refused by the model ({_ids(refused)}){tail}.")
    unscored = [r for lv in LEVELS for r in g.levels[lv].unscored]
    if unscored:
        why = Counter(g.levels[r.level].verdicts[r.run_id].comment for r in unscored)
        parts.append(f"{len(unscored)} run(s) were unscored: " + "; ".join(
            f"{reason.removeprefix('unscored: ')} ({n})" for reason, n in why.most_common()) + ".")
    fails = [r for lv in LEVELS for r in g.levels[lv].fails]
    if fails:
        grouped: dict[str, list[RunRecord]] = {}
        for r in fails:
            grouped.setdefault(g.levels[r.level].verdicts[r.run_id].comment.removeprefix("fail: "), []).append(r)
        parts.append("Failures: " + "; ".join(
            f"{why} ({_ids(rs)})" for why, rs in sorted(grouped.items(), key=lambda kv: -len(kv[1]))) + ".")
    elif g.all_runs():
        parts.append("No failures.")
    return [head, "", " ".join(parts)]


# --------------------------------------------------------------------------- (c) narrated example run


def pick_example(g: GoalStats) -> RunRecord | None:
    """The most informative run: a P1 failure, any failure, a P1 pass, any pass, then anything."""
    order = {"P1": 0, "P2": 1, "P0": 2}
    runs = sorted(g.all_runs(), key=lambda r: (order[r.level], r.repeat, r.attempt))
    v = lambda r: g.levels[r.level].verdicts[r.run_id]  # noqa: E731
    for test in (lambda r: v(r).score is False and r.level == "P1", lambda r: v(r).score is False,
                 lambda r: v(r).score is True and r.level == "P1", lambda r: v(r).score is True,
                 lambda r: v(r).score is None and r.end_reason != "refusal", lambda r: True):
        for r in runs:
            if test(r):
                return r
    return None


def claims_by_call(outputs: dict[str, Any]) -> dict[int, list[tuple[int, dict]]]:
    """tool-log seq -> [(claim number, claim)]. Uses call_seq when the file has it;
    otherwise matches claims to successful calls of the same agent and tool in order."""
    claims = outputs["world"]["claims"]
    out: dict[int, list[tuple[int, dict]]] = {}
    if all(c.get("call_seq") for c in claims):
        for i, c in enumerate(claims, 1):
            out.setdefault(c["call_seq"], []).append((i, c))
        return out
    pending = list(enumerate(claims, 1))
    for call in outputs["tool_log"]:
        if call["is_error"]:
            continue
        mine = [(i, c) for i, c in pending if c["agent"] == call["agent"] and c["tool"] == call["tool"]
                and c["field"] in call["args"] and c["claimed"] == call["args"][c["field"]]
                and c["scripted"] == call["scripted"]]
        fields_seen = set()
        for i, c in mine:
            if c["field"] in fields_seen:
                continue
            fields_seen.add(c["field"])
            out.setdefault(call["seq"], []).append((i, c))
            pending.remove((i, c))
    return out


def _value(x: Any) -> str:
    return f"`{x!r}`" if not isinstance(x, str) else f"`{x}`"


def narrate(r: RunRecord, g: GoalStats, uc) -> list[str]:
    o = r.outputs
    lines = [f"**Example: {g.goal} {r.level}, run {r.short_id}** (`{show_path(r.path)}`)", ""]
    by_call = claims_by_call(o)
    current = None
    for call in o["tool_log"]:
        if call["round"] != current:
            current = call["round"]
            lines.append(f"*Round {current} (day {call['day']})*")
        probe = " This was a scripted probe, delivered as an ordinary message." if call["scripted"] else ""
        lines.append(f"- {uc.call_sentence(call)}{probe} `[tool #{call['seq']}]`")
        for n, c in by_call.get(call["seq"], []):
            match = "matches" if c["claimed"] == c["truth"] else "does not match"
            lines.append(f"  - Claim checked: `{c['field']}` = {_value(c['claimed'])}; ground truth "
                         f"{_value(c['truth'])}. It {match}. `[claim #{n}]`")
    for n, ref in enumerate(o["world"]["truth"].get("refusals", []), 1):
        d = ref.get("stop_details") or {}
        tokens = (ref.get("usage") or {}).get("output_tokens")
        extra = f", {tokens} output tokens" if tokens is not None else ""
        lines.append(f"- The {ref['agent']}'s model declined to respond in round {ref['round']} "
                     f"(category `{d.get('category')}`{extra}). `[refusal #{n}]`")
    end = o["end_reason"]
    why = uc.END_TEXT.get(end) or END_REASONS.get(end, end)
    lines.append(f"- The run ended `{end}`: {why}, after {o['rounds']} completed round(s), on day {o['day']}. `[end_reason]`")
    v = g.levels[r.level].verdicts[r.run_id]
    score = {True: "pass", False: "fail", None: "unscored"}[v.score]
    detail = v.comment.split(": ", 1)[1] if ": " in v.comment else v.comment
    lines.append(f"- Verdict for {g.goal}: **{score}**: {detail}. `[scorer]`")
    if v.receiver:
        shown = "; ".join(f"`{k}` = " + ("n/a" if val is None else _value(val)) for k, val in v.receiver.items())
        lines.append(f"- Receiver check: {shown}. `[scorer]`")
    return lines


# --------------------------------------------------------------------------- the document


def render(runs: list[RunRecord], skipped: list[tuple[Any, str]], uc, *, title: str, banner: str = "") -> str:
    stats = aggregate(runs, uc)
    models = sorted({(r.outputs.get("model"), r.outputs.get("effort")) for r in runs})
    out = [f"# {title}", ""]
    if banner:
        out += [f"> {banner}", ""]
    out += [f"Built from {len(runs)} run file(s), listed under Sources. Model and effort in these runs: "
            + ", ".join(f"`{m}` / `{e}`" for m, e in models) + ".", "", "## Summary", ""]
    out += summary_table(stats, uc)
    out += ["", "## By goal", ""]
    for g in stats:
        out += paragraph(g, uc) + [""]
    out += ["## Example runs", "",
            "Each example is filled in from one saved run file. Every line ends with the part of the file it "
            "comes from: `[tool #n]` a tool call, `[claim #n]` a checked claim, `[refusal #n]` a refusal, "
            "`[end_reason]` how the run ended, `[scorer]` the goal's scorer run on that file.", ""]
    for g in stats:
        ex = pick_example(g)
        if ex is not None:
            out += narrate(ex, g, uc) + [""]
    out += ["## Sources", "", "| File | Run id | Goal | Level | Arm | End | Verdict |", "| --- | --- | --- | --- | --- | --- | --- |"]
    verdicts = {r.run_id: uc.SCORERS[r.goal](r.outputs) for r in runs}
    for r in sorted(runs, key=lambda r: str(r.path)):
        s = {True: "pass", False: "fail", None: "unscored"}[verdicts[r.run_id].score]
        out.append(f"| `{show_path(r.path)}` | `{r.run_id}` | {r.goal} | {r.level} | {r.arm} | {r.end_reason} | {s} |")
    if skipped:
        out += ["", "Skipped (not used for any number above):", ""]
        out += [f"- `{show_path(Path(p))}`: {why}" for p, why in skipped]
    return "\n".join(out) + "\n"
