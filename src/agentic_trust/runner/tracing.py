"""Turn on LangSmith tracing for live runs, and check that it reaches the project.

Tracing only; datasets, evaluators and the sweep loop come later (runner/).

Settings are read from the environment, loaded from .env if present:

    LANGSMITH_API_KEY    required to send traces
    LANGSMITH_PROJECT    defaults to "agentic-trust"
    LANGSMITH_TRACING    set to "true" by configure() when a key is present
    LANGSMITH_ENDPOINT   optional; only needed for a non-default region

Check the wiring without spending any model tokens:

    python -m agentic_trust.runner.tracing

It sends one small trace to the project, reads it back and prints its URL.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from dataclasses import dataclass

PROJECT = "agentic-trust"


@dataclass(frozen=True)
class TracingStatus:
    enabled: bool
    project: str
    api_key_present: bool
    endpoint: str

    def describe(self) -> str:
        if not self.api_key_present:
            return "tracing OFF: LANGSMITH_API_KEY is not set (add it to .env)"
        state = "ON" if self.enabled else "OFF (LANGSMITH_TRACING is not true)"
        return f"tracing {state}: project {self.project!r} at {self.endpoint}"


def configure(env_file: str | None = ".env") -> TracingStatus:
    """Load .env, default the project to agentic-trust, and enable tracing if a key is set."""
    if env_file:
        from dotenv import load_dotenv

        load_dotenv(env_file)
    os.environ.setdefault("LANGSMITH_PROJECT", PROJECT)
    key = bool(os.environ.get("LANGSMITH_API_KEY"))
    if key:
        os.environ.setdefault("LANGSMITH_TRACING", "true")
    return status()


def status() -> TracingStatus:
    """What the environment says. (An enclosing tracing_context can still override it.)"""
    flag = os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING_V2") or ""
    return TracingStatus(
        enabled=flag.strip().lower() == "true",
        project=os.environ.get("LANGSMITH_PROJECT", PROJECT),
        api_key_present=bool(os.environ.get("LANGSMITH_API_KEY")),
        endpoint=os.environ.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"),
    )


def flush() -> None:
    """Send any buffered trace data. Call before a script exits."""
    from langsmith import Client

    Client().flush()
    try:
        from langchain_core.tracers.langchain import wait_for_all_tracers

        wait_for_all_tracers()  # LangGraph's own spans
    except ImportError:
        pass


def check(timeout_s: float = 30.0) -> str:
    """Send one tiny trace to the project, read it back, and return its URL. No model calls."""
    from langsmith import Client, traceable

    st = status()
    if not (st.api_key_present and st.enabled):
        raise RuntimeError(st.describe())
    client = Client()
    run_id = uuid.uuid4()

    @traceable(run_type="chain", name="tracing-check", client=client, project_name=st.project)
    def ping(note: str) -> dict:
        return {"ok": True, "note": note}

    ping("agentic-trust tracing check", langsmith_extra={"run_id": run_id, "tags": ["tracing-check"]})
    client.flush()

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            run = client.read_run(run_id)
            return client.get_run_url(run=run, project_name=st.project)
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1.0)


def main() -> int:
    st = configure()
    print(st.describe())
    if not (st.api_key_present and st.enabled):
        return 1
    try:
        url = check()
    except Exception as exc:  # report, don't hide: the point is to see what's wrong
        print(f"check FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"check OK: trace visible in {st.project!r}\n{url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
