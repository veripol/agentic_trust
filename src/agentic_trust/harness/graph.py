"""The shared graph shape, the deterministic router and the agent turn loop.

Agents never talk to each other directly. On its turn an agent reads its
unread wire messages, then runs a plain ReAct loop over its own tools (model
call -> tool calls -> model call ...) until it stops calling tools or hits
``max_steps_per_turn``. Some tools post messages to other agents; those are
read on the recipient's next turn.

A deterministic ``Router`` decides who goes next and when a round is complete;
the harness advances the clock at each round end (docs/clock.md). LangGraph runs
the loop: one node per agent, a conditional edge from every node to the next
agent or END. Every run ends with exactly one reason from ``END_REASONS``.

Probes are injected by the harness as ordinary wire messages, indistinguishable
to the receiver from real ones. A scripted tool call runs through the sender's
real tool, so it has exactly the effects (and wire message) the real call would.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Protocol, TypedDict

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from .claim import ToolCrash, ToolSpec
from .models import AgentModel, Usage
from .state import RunConfig, RunState, World
from .tracing import traced_complete, traced_run, traced_tool

# --------------------------------------------------------------------------- end reasons

# Why a run ended. Scenario tools end a run with their own natural-end reason
# (e.g. "agreed"); the harness ends it with one of these.
END_REASONS = {
    "deadline": "the round played at or past max_days finished (docs/clock.md)",
    "round_cap": "max_rounds rounds completed",
    "quiescent": "a full round passed in which no agent had anything to act on",
    "recursion_limit": "LangGraph's recursion limit (one superstep per agent turn) was hit",
    "token_cap": "the per-run token backstop (max_run_tokens) was hit",
    "refusal": "the model declined a request (stop_reason 'refusal')",
    "tool_error": "a tool raised an unexpected error (a bug); details in world truth tool_errors",
    "max_tokens": "a model response hit max_tokens",
    "router_end": "the scenario router ended the run",
    "completed": "the graph finished without any other reason being set",
}

# A run that ends this way says nothing about the criterion: a model declining, or
# a runaway run stopped by the token backstop, is not the agent failing it.
# Scorers must return unscored, never fail.
UNSCORED_END_REASONS = frozenset({"refusal", "token_cap", "tool_error"})


# --------------------------------------------------------------------------- scenario shape


@dataclass(frozen=True)
class AgentSpec:
    name: str
    tools: tuple[ToolSpec, ...]

    def __post_init__(self) -> None:
        names = [t.name for t in self.tools]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.name}: duplicate tool names")

    def tool(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    def definitions(self) -> list[dict[str, Any]]:
        """Tool definitions in a fixed order, so the cached prefix never changes."""
        return [t.definition() for t in self.tools]


@dataclass(frozen=True)
class RouteDecision:
    next_agent: str | None  # None = end the run
    round_complete: bool = False


class Router(Protocol):
    def first(self, state: RunState) -> str: ...

    def next(self, state: RunState, just_ran: str) -> RouteDecision: ...


@dataclass(frozen=True)
class RoundRobinRouter:
    """Agents take turns in a fixed order; a round is complete when the order wraps."""

    order: tuple[str, ...]

    def first(self, state: RunState) -> str:
        return self.order[0]

    def next(self, state: RunState, just_ran: str) -> RouteDecision:
        i = self.order.index(just_ran)
        return RouteDecision(self.order[(i + 1) % len(self.order)], round_complete=i == len(self.order) - 1)


@dataclass(frozen=True)
class Scenario:
    """Everything the harness needs from a scenario folder."""

    use_case: str
    agents: Mapping[str, AgentSpec]
    router: Router
    init_world: Callable[[RunConfig], World]
    seed: Callable[[RunState], None] | None = None  # seeded starts: prior messages, state
    on_round_end: Callable[[RunState], None] | None = None  # time-based effects (deadlines)


# --------------------------------------------------------------------------- probes


@dataclass(frozen=True)
class ScriptedCall:
    """The harness makes ``sender`` call ``tool`` with ``args``, through the real tool."""

    sender: str
    tool: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class ScriptedMessage:
    sender: str
    recipients: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class DelayedResult:
    """A tool result that arrives later, e.g. a delayed de-anonymisation assessment."""

    recipient: str
    tool: str
    result: Any


@dataclass(frozen=True)
class PostRunQuestion:
    """Asked after the run ends; the answer is recorded, no tools may be used."""

    text: str


Effect = ScriptedCall | ScriptedMessage | DelayedResult | PostRunQuestion


@dataclass(frozen=True)
class Probe:
    """A scripted event, keyed to a goal.

    ``when``:
    - ``before_first_turn``: fires as ``agent``'s first turn starts, so the
      agent sees it in its first inbox.
    - ``after_tool``: fires after ``agent``'s first successful, unscripted call
      to ``tool`` whose arguments include every item in ``match``.
    - ``after_run``: fires once the run has ended; ``effect`` must be a PostRunQuestion.
    Each probe fires at most once.
    """

    goal: str
    when: Literal["before_first_turn", "after_tool", "after_run"]
    agent: str
    effect: Effect
    tool: str | None = None
    match: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.when == "after_tool" and not self.tool:
            raise ValueError("after_tool probes need a tool")
        if (self.when == "after_run") != isinstance(self.effect, PostRunQuestion):
            raise ValueError("PostRunQuestion goes with after_run, and only there")


# --------------------------------------------------------------------------- run result


@dataclass
class RunResult:
    config: RunConfig
    end_reason: str
    tool_log: list[dict[str, Any]]
    world: dict[str, Any]
    wire: list[dict[str, Any]]
    transcripts: dict[str, list[dict[str, Any]]]
    prompts: dict[str, str]
    usage: Usage
    usage_by_agent: dict[str, Usage]
    day: int
    rounds: int

    @property
    def unscored(self) -> bool:
        """True when every goal in this run must be scored unscored (see UNSCORED_END_REASONS)."""
        return self.end_reason in UNSCORED_END_REASONS

    def outputs(self) -> dict[str, Any]:
        """What a LangSmith run returns and evaluators read (build spec: tool_log and world)."""
        return {
            "end_reason": self.end_reason,
            "unscored": self.unscored,
            "model": self.config.model,
            "effort": self.config.effort,
            "tool_log": self.tool_log,
            "world": self.world,
            "day": self.day,
            "rounds": self.rounds,
            "usage": self.usage.to_dict(),
        }


# --------------------------------------------------------------------------- the episode


class _Episode:
    def __init__(
        self,
        config: RunConfig,
        scenario: Scenario,
        prompts: Mapping[str, str],
        model: AgentModel,
    ) -> None:
        missing = set(config.agents) - set(scenario.agents)
        if missing:
            raise ValueError(f"scenario {scenario.use_case} has no agents {sorted(missing)}")
        if set(prompts) != set(config.agents):
            raise ValueError("need exactly one system prompt per agent in the run")
        settings = model.settings
        if (settings.model, settings.effort) != (config.model, config.effort):
            raise ValueError(
                f"model settings {settings.model}/{settings.effort} do not match the run config "
                f"{config.model}/{config.effort}; every run in the study must use the same model and effort"
            )
        self.config = config
        self.scenario = scenario
        self.prompts = dict(prompts)
        self.model = model
        self.state = RunState(config=config, world=scenario.init_world(config))
        self.history: dict[str, list[dict[str, Any]]] = {a: [] for a in config.agents}
        self.usage = Usage()
        self.usage_by_agent = {a: Usage() for a in config.agents}
        self.fired: set[int] = set()
        self.active_this_round = False
        for probe in config.probes:
            for name in _probe_agents(probe):
                if name not in config.agents:
                    raise ValueError(f"probe {probe.goal} names agent {name!r} not in this run")

    # -- graph -------------------------------------------------------------------------

    def run(self) -> RunResult:
        if self.scenario.seed:
            self.scenario.seed(self.state)

        class GraphState(TypedDict):
            next: str

        graph = StateGraph(GraphState)
        for agent in self.config.agents:
            graph.add_node(agent, self._node(agent))
        targets = {a: a for a in self.config.agents} | {END: END}
        route = lambda s: s["next"]  # noqa: E731 - the decision was made inside the node
        graph.add_conditional_edges(START, route, targets)
        for agent in self.config.agents:
            graph.add_conditional_edges(agent, route, targets)
        app = graph.compile()

        first = self.scenario.router.first(self.state)
        try:
            app.invoke({"next": first}, {"recursion_limit": self.config.recursion_limit})
        except GraphRecursionError:
            self.state.end("recursion_limit")
        self.state.end("completed")  # no-op if a reason was already set

        if self.state.end_reason != "refusal":
            self._post_run_probes()
        return self._result()

    def _node(self, agent: str):
        def node(_: dict) -> dict:
            self.agent_turn(agent)
            return {"next": self._route_after(agent)}

        return node

    def _route_after(self, agent: str) -> str:
        if self.state.ended:
            return END
        decision = self.scenario.router.next(self.state, agent)
        if decision.round_complete:
            # The deadline round is the first round played at day >= max_days. It runs
            # in full (so an "on deadline" action gets its turn) and the run ends after
            # it, without advancing the day past the deadline (docs/clock.md, rule 5).
            max_days = self.config.max_days
            deadline_round = max_days is not None and self.state.day >= max_days
            if not deadline_round:
                self.state.clock.end_round()
            self.state.round += 1
            if self.scenario.on_round_end:
                self.scenario.on_round_end(self.state)
            if not self.active_this_round:
                self.state.end("quiescent")  # a full round with nothing to do
            self.active_this_round = False
            if deadline_round:
                self.state.end("deadline")
            if self.state.round >= self.config.max_rounds:
                self.state.end("round_cap")
        if decision.next_agent is None:
            self.state.end("router_end")
        return END if self.state.ended else decision.next_agent

    # -- one agent turn ------------------------------------------------------------------

    def agent_turn(self, agent: str) -> None:
        state = self.state
        first_turn = state.turns.get(agent, 0) == 0
        if first_turn:
            self._fire(lambda p: p.when == "before_first_turn" and p.agent == agent)

        inbox = state.wire.unread(agent)
        parts = []
        if first_turn and agent in self.config.initial_messages:
            parts.append(self.config.initial_messages[agent])
        parts.extend(m.render() for m in inbox)
        if not parts:
            return  # nothing new to act on; the turn is skipped and costs nothing
        if inbox:
            state.wire.mark_read(agent, inbox[-1].seq)
        state.turns[agent] = state.turns.get(agent, 0) + 1
        self.active_this_round = True

        history = self.history[agent]
        history.append({"role": "user", "content": "\n\n".join(parts)})
        spec = self.scenario.agents[agent]
        tools = spec.definitions()

        for _ in range(self.config.max_steps_per_turn):
            turn = traced_complete(self.model, agent=agent, system=self.prompts[agent], tools=tools, messages=history)
            self._count(agent, turn.usage)
            history.append({"role": "assistant", "content": turn.content})
            if turn.stop_reason == "refusal":
                state.world.setdefault("refusals", []).append({
                    "agent": agent, "round": state.round, "stop_reason": turn.stop_reason,
                    "stop_details": turn.stop_details, "content": turn.content,
                    "usage": turn.usage.to_dict(), "model": turn.model, "request_id": turn.request_id,
                })
                state.end("refusal")
                return
            if turn.stop_reason == "max_tokens":
                state.end("max_tokens")
                return
            if not turn.tool_uses:
                return

            results = []
            for use in turn.tool_uses:
                results.append(self._call(agent, spec, use))
            history.append({"role": "user", "content": results})
            if state.ended:
                return
            cap = self.config.max_run_tokens
            if cap is not None and self.usage.capped_tokens >= cap:
                state.end("token_cap")
                return
        # max_steps_per_turn reached: the turn ends; history stays valid (last entry is tool results)

    def _call(self, agent: str, spec: AgentSpec, use: dict[str, Any]) -> dict[str, Any]:
        tool = spec.tool(use["name"])
        if tool is None:
            result, is_error = {"error": f"unknown tool {use['name']!r}"}, True
        else:
            try:
                outcome = traced_tool(self.state, agent, tool, use.get("input") or {})
                result, is_error = outcome.result, outcome.is_error
                if not is_error:
                    args = use.get("input") or {}
                    self._fire(
                        lambda p: p.when == "after_tool"
                        and p.agent == agent
                        and p.tool == tool.name
                        and all(args.get(k) == v for k, v in p.match.items())
                    )
            except ToolCrash as crash:
                self.state.world.setdefault("tool_errors", []).append({
                    "agent": agent, "tool": crash.tool, "round": self.state.round, "args": use.get("input"),
                    "error": f"{type(crash.exc).__name__}: {crash.exc}",
                    "traceback": "".join(traceback.format_exception(crash.exc)),
                })
                self.state.end("tool_error")
                result, is_error = {"error": f"internal tool error in {crash.tool}"}, True
        block = {
            "type": "tool_result",
            "tool_use_id": use["id"],
            "content": json.dumps(result, sort_keys=True, ensure_ascii=False),
        }
        if is_error:
            block["is_error"] = True
        return block

    def _count(self, agent: str, usage: Usage) -> None:
        self.usage.add(usage)
        self.usage_by_agent[agent].add(usage)

    # -- probes --------------------------------------------------------------------------

    def _fire(self, predicate: Callable[[Probe], bool]) -> None:
        for i, probe in enumerate(self.config.probes):
            if i in self.fired or not predicate(probe):
                continue
            self.fired.add(i)
            self.state.world.setdefault("probes_fired", []).append(
                {"goal": probe.goal, "when": probe.when, "round": self.state.round, "day": self.state.day}
            )
            self._apply(probe.effect)

    def _apply(self, effect: Effect) -> None:
        state = self.state
        if isinstance(effect, ScriptedCall):
            spec = self.scenario.agents[effect.sender].tool(effect.tool)
            if spec is None:
                raise ValueError(f"probe calls {effect.tool!r}, which {effect.sender} does not have")
            args = dict(effect.args)
            if "ticket_id" in spec.input_schema.get("required", []) and "ticket_id" not in args:
                if "ticket_id" not in state.ids:
                    raise RuntimeError(f"probe needs the run's ticket_id for {effect.tool}, but none exists yet")
                args["ticket_id"] = state.ids["ticket_id"]  # reuse the run's ticket id
            outcome = traced_tool(state, effect.sender, spec, args, scripted=True)
            if outcome.is_error:
                raise RuntimeError(f"scripted {effect.tool} call failed: {outcome.result}")
        elif isinstance(effect, ScriptedMessage):
            state.post(sender=effect.sender, recipients=effect.recipients, content=effect.text, scripted=True)
        elif isinstance(effect, DelayedResult):
            state.post(sender=effect.tool, recipients=effect.recipient, content=effect.result, kind="result", scripted=True)

    def _post_run_probes(self) -> None:
        for i, probe in enumerate(self.config.probes):
            if probe.when != "after_run" or i in self.fired:
                continue
            agent = probe.agent
            if not self.history[agent]:
                continue  # the agent never acted, so there is nothing to ask about
            self.fired.add(i)
            history = self.history[agent]
            history.append({"role": "user", "content": probe.effect.text})
            turn = traced_complete(
                self.model,
                agent=agent,
                system=self.prompts[agent],
                tools=self.scenario.agents[agent].definitions(),
                messages=history,
                tool_choice={"type": "none"},
            )
            self._count(agent, turn.usage)
            history.append({"role": "assistant", "content": turn.content})
            self.state.world.setdefault("post_run_answers", {})[probe.goal] = {
                "agent": agent,
                "question": probe.effect.text,
                "answer": turn.text,
                "stop_reason": turn.stop_reason,
            }

    def _result(self) -> RunResult:
        s = self.state
        return RunResult(
            config=self.config,
            end_reason=s.end_reason,
            tool_log=[r.to_dict() for r in s.tool_log],
            world=s.world.snapshot(),
            wire=[m.to_dict() for m in s.wire.messages],
            transcripts={a: list(h) for a, h in self.history.items()},
            prompts=dict(self.prompts),
            usage=self.usage,
            usage_by_agent=self.usage_by_agent,
            day=s.day,
            rounds=s.round,
        )


def _probe_agents(probe: Probe) -> list[str]:
    names = [probe.agent]
    e = probe.effect
    if isinstance(e, ScriptedCall):
        names.append(e.sender)
    elif isinstance(e, ScriptedMessage):
        names += [e.sender, *e.recipients]
    elif isinstance(e, DelayedResult):
        names.append(e.recipient)
    return names


def run_episode(
    config: RunConfig,
    scenario: Scenario,
    prompts: Mapping[str, str],
    model: AgentModel,
) -> RunResult:
    """Run one configured episode end to end and return its trace."""
    episode = _Episode(config, scenario, prompts, model)  # validates before anything is traced
    return traced_run(episode.run, config, prompts)


__all__ = [
    "END_REASONS",
    "UNSCORED_END_REASONS",
    "AgentSpec",
    "DelayedResult",
    "PostRunQuestion",
    "Probe",
    "RoundRobinRouter",
    "RouteDecision",
    "Router",
    "RunResult",
    "Scenario",
    "ScriptedCall",
    "ScriptedMessage",
    "run_episode",
]
