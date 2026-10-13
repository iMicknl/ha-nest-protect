"""Tests for which entities are enabled when first registered."""

import pytest

from custom_components.nest_protect.binary_sensor import BINARY_SENSOR_DESCRIPTIONS
from custom_components.nest_protect.sensor import SENSOR_DESCRIPTIONS

DISABLED_BY_DEFAULT = {
    "component_speaker_test_passed",
    "component_smoke_test_passed",
    "component_co_test_passed",
    "component_wifi_test_passed",
    "component_led_test_passed",
    "component_pir_test_passed",
    "component_buzzer_test_passed",
    "component_hum_test_passed",
    "removed_from_base",
    "last_audio_self_test_end_utc_secs",
    "latest_manual_test_end_utc_secs",
}

ENABLED_BY_DEFAULT = {
    "smoke_status",
    "co_status",
    "heat_status",
    "battery_level",
    "battery_health_state",
    "replace_by_date_utc_secs",
    "current_temperature",
    "is_online",
    "line_power_present",
    "auto_away",
}

DESCRIPTIONS = [*BINARY_SENSOR_DESCRIPTIONS, *SENSOR_DESCRIPTIONS]


@pytest.mark.parametrize("key", sorted(DISABLED_BY_DEFAULT))
def test_rarely_used_diagnostics_disabled_by_default(key):
    """Self-test and base-removal diagnostics start disabled."""
    matches = [d for d in DESCRIPTIONS if d.key == key]
    assert matches
    assert all(not d.entity_registry_enabled_default for d in matches)


@pytest.mark.parametrize("key", sorted(ENABLED_BY_DEFAULT))
def test_core_entities_enabled_by_default(key):
    """Alarm, power and health entities stay enabled."""
    matches = [d for d in DESCRIPTIONS if d.key == key]
    assert matches
    assert all(d.entity_registry_enabled_default for d in matches)
