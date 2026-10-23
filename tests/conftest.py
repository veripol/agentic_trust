import pytest
from langsmith import tracing_context


@pytest.fixture(autouse=True)
def no_live_tracing():
    """No test sends traces, whatever the environment says; tracing tests opt back in."""
    with tracing_context(enabled=False):
        yield
