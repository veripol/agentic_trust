import os

import pytest
from langsmith import tracing_context


@pytest.fixture(autouse=True)
def no_live_tracing():
    """No test sends traces or reads real keys, whatever the environment says. Code under
    test (tracing.configure) may set LANGSMITH_*/ANTHROPIC_* variables directly, which
    monkeypatch would not undo, so they are restored here after every test."""
    keep = {k: v for k, v in os.environ.items() if k.startswith(("LANGSMITH_", "LANGCHAIN_", "ANTHROPIC_"))}
    with tracing_context(enabled=False):
        yield
    for k in [k for k in os.environ if k.startswith(("LANGSMITH_", "LANGCHAIN_", "ANTHROPIC_"))]:
        if k not in keep:
            del os.environ[k]
    os.environ.update(keep)
