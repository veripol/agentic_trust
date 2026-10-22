import pytest
from langsmith import tracing_context

from .toy import toy_scenario


@pytest.fixture(autouse=True)
def no_live_tracing():
    """Tests never send traces, whatever the environment says; tracing tests opt back in."""
    with tracing_context(enabled=False):
        yield


@pytest.fixture
def scenario():
    return toy_scenario()
