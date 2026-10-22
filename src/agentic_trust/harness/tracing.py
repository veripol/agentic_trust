"""LangSmith tracing for a run.

Every span here is a no-op unless tracing is enabled (``LANGSMITH_TRACING=true``
with an API key, or an enclosing ``langsmith.tracing_context(enabled=True)``).
``runner/tracing.py`` turns it on for live runs and points it at the
``agentic-trust`` project.

One run produces one trace:

    <run name>                      chain  config in; end_reason, tool_log, world out
      LangGraph                     chain  (LangGraph's own span)
        <agent>                     chain  one per agent turn (LangGraph node)
          claude-sonnet-5-5         llm    one per model call, with token usage
          <tool name>               tool   one per tool call; probe calls tagged "scripted"

The trace is for people reading runs and for scorers. It does contain ground
truth (the ``world`` snapshot in the root outputs); none of it is ever sent to an
agent.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping

from langsmith import traceable

from .claim import ToolOutcome, ToolSpec, execute_tool
from .models import AgentModel, ModelTurn
from .prompts import prompt_name
from .state import RunConfig, RunState


def run_name(config: RunConfig) -> str:
    """``sec01-vendor/SEC-DEC-1/P1`` for a single-goal run; ``<use case>/<arm>`` otherwise."""
    tested = [(a, config.assignment(a)) for a in config.agents if config.assignment(a).goal]
    if len(tested) == 1:
        agent, a = tested[0]
        return prompt_name(config.use_case, agent, a.goal, a.level)
    cells = ",".join(f"{agent}:{a.goal}/{a.level}" for agent, a in tested)
    return f"{config.use_case}/{config.arm}" + (f"[{cells}]" if cells else "")


def run_tags(config: RunConfig) -> list[str]:
    """Build spec tags: prompt name, use case, arm and each goal ID, plus the config's own tags."""
    tags = [config.use_case, f"arm:{config.arm}"]
    for agent in config.agents:
        a = config.assignment(agent)
        if a.goal:
            tags += [a.goal, prompt_name(config.use_case, agent, a.goal, a.level), f"level:{a.level}"]
    tags += list(config.tags)
    return list(dict.fromkeys(tags))  # dedupe, keep order


def run_metadata(config: RunConfig) -> dict[str, Any]:
    return {
        "run_id": config.run_id,
        "use_case": config.use_case,
        "arm": config.arm,
        "repeat": config.repeat,
        "model": config.model,
        "effort": config.effort,
        "assignments": {a: {"goal": s.goal, "level": s.level} for a, s in config.assignments.items()},
        "days_per_round": config.days_per_round,
        "max_days": config.max_days,
        "max_rounds": config.max_rounds,
        "max_steps_per_turn": config.max_steps_per_turn,
        "max_run_tokens": config.max_run_tokens,
        "recursion_limit": config.recursion_limit,
    }


# --------------------------------------------------------------------------- root span


def traced_run(run: Callable[[], Any], config: RunConfig, prompts: Mapping[str, str]):
    """Run ``run()`` (which returns a RunResult) inside the run's root span."""

    @traceable(
        run_type="chain",
        process_inputs=lambda inputs: {
            "config": run_metadata(config),
            "initial_messages": dict(config.initial_messages),
            "prompts": dict(prompts),
        },
        process_outputs=lambda result: result.outputs(),
    )
    def episode(config: RunConfig, prompts: Mapping[str, str]):
        return run()

    return episode(
        config,
        prompts,
        langsmith_extra={"name": run_name(config), "tags": run_tags(config), "metadata": run_metadata(config)},
    )


# --------------------------------------------------------------------------- model span


def _llm_inputs(inputs: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "agent": inputs["agent"],
        "system": inputs["system"],
        "tools": [t["name"] for t in inputs["tools"]],
        "messages": copy.deepcopy(list(inputs["messages"])),  # history keeps growing after this call
        "tool_choice": inputs.get("tool_choice"),
    }


def _llm_outputs(turn: ModelTurn) -> dict[str, Any]:
    u = turn.usage
    return {
        "content": turn.content,
        "stop_reason": turn.stop_reason,
        "stop_details": turn.stop_details,
        "request_id": turn.request_id,
        # LangSmith reads usage_metadata for token counts and cost; its input_tokens
        # is the total prompt size, cached or not.
        "usage_metadata": {
            "input_tokens": u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens,
            "output_tokens": u.output_tokens,
            "total_tokens": u.total_tokens,
            "input_token_details": {
                "cache_read": u.cache_read_input_tokens,
                "cache_creation": u.cache_creation_input_tokens,
            },
        },
    }


@traceable(run_type="llm", process_inputs=_llm_inputs, process_outputs=_llm_outputs)
def _complete(model: AgentModel, *, agent, system, tools, messages, tool_choice=None) -> ModelTurn:
    return model.complete(system=system, tools=tools, messages=messages, tool_choice=tool_choice)


def traced_complete(model: AgentModel, *, agent: str, system: str, tools, messages, tool_choice=None) -> ModelTurn:
    s = model.settings
    return _complete(
        model,
        agent=agent,
        system=system,
        tools=tools,
        messages=messages,
        tool_choice=tool_choice,
        langsmith_extra={
            "name": s.model,
            "metadata": {"agent": agent, "ls_provider": "anthropic", "ls_model_name": s.model, "effort": s.effort},
        },
    )


# --------------------------------------------------------------------------- tool span


@traceable(
    run_type="tool",
    process_inputs=lambda i: {"agent": i["agent"], "args": copy.deepcopy(dict(i["args"])), "scripted": i["scripted"]},
    process_outputs=lambda o: {"result": o.result, "is_error": o.is_error},
)
def _tool(state: RunState, agent: str, spec: ToolSpec, args: Mapping[str, Any], scripted: bool) -> ToolOutcome:
    return execute_tool(state, agent, spec, args, scripted=scripted)


def traced_tool(state: RunState, agent: str, spec: ToolSpec, args: Mapping[str, Any], *, scripted: bool = False) -> ToolOutcome:
    return _tool(
        state,
        agent,
        spec,
        args,
        scripted,
        langsmith_extra={
            "name": spec.name,
            "tags": ["scripted"] if scripted else [],
            "metadata": {"agent": agent, "round": state.round, "day": state.day, "scripted": scripted},
        },
    )
