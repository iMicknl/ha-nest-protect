"""Data models for Nest thermostats."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum, unique


@unique
class TemperatureSource(StrEnum):
    """Which temperature a thermostat controls on."""

    BACKPLATE = "backplate"
    SINGLE_SENSOR = "single_sensor"
    MULTI_SENSOR = "multi_sensor"


@dataclass
class ThermostatState:
    """Read-only thermostat state surfaced to the HA entities."""

    resource_id: str
    serial_number: str
    model: str
    name: str | None = None
    software_version: str | None = None
    location: str | None = None
    # The thermostat's own sensor.
    backplate_temperature: float | None = None
    # What the thermostat controls on, which follows the selected Nest
    # Temperature Sensors. This is the temperature Google's SDM API reports.
    current_temperature: float | None = None
    current_humidity: float | None = None
    temperature_source: TemperatureSource | None = None
    # Resource ids of the temperature sensors currently controlled on.
    active_sensors: list[str] = field(default_factory=list)
