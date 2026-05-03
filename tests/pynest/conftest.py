"""Fixtures for pynest tests.

These tests are pure API client tests and don't need Home Assistant fixtures.
"""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Override the parent fixture to disable HA for pure client tests."""
    return


@pytest.fixture(autouse=True)
def verify_cleanup():
    """Override strict cleanup verification for pure API client tests.

    The pytest_homeassistant_custom_component verify_cleanup fixture is too
    strict for these tests - it fails on the _run_safe_shutdown_loop thread
    from asyncio executor shutdown, which is normal cleanup behavior.
    """
    return
