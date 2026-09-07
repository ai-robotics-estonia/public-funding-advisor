"""Shared pytest fixtures for the backend suite."""

import pytest

from backend import rate_limit


@pytest.fixture(autouse=True)
def clear_rate_limit():
    """Each test starts with a clean rate-limit bucket."""
    rate_limit.reset()
    yield
    rate_limit.reset()
