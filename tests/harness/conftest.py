import pytest

from .toy import toy_scenario


@pytest.fixture
def scenario():
    return toy_scenario()
