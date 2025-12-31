"""Fixtures for pynest tests.

These tests are pure API client tests and don't need Home Assistant fixtures.
"""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Override the parent fixture to disable HA for pure client tests."""
    yield
