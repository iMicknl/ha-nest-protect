"""Enums for Nest Protect."""
from enum import StrEnum, unique
import logging

_LOGGER = logging.getLogger(__name__)


@unique
class BucketType(StrEnum):
    """Bucket types."""

    BUCKETS = "buckets"
    DEVICE = "device"
    KRYPTONITE = "kryptonite"  # Temperature Sensors
    QUARTZ = "quartz"
    RCS_SETTINGS = "rcs_settings"
    SHARED = "shared"
    STRUCTURE = "structure"  # General
    TOPAZ = "topaz"  # Nest Protect
    TRACK = "track"
    WHERE = "where"  # Areas

    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value):  # type: ignore
        _LOGGER.warning(f"Unsupported value {value} has been returned for {cls}")

        return cls.UNKNOWN


@unique
class Environment(StrEnum):
    """Bucket types."""

    FIELDTEST = "fieldtest"
    PRODUCTION = "production"
