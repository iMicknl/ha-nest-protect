"""Enums for Nest Protect."""

from enum import StrEnum, unique
import logging

_LOGGER = logging.getLogger(__name__)


@unique
class BucketType(StrEnum):
    """Bucket types."""

    BUCKETS = "buckets"
    DELAYED_TOPAZ = "delayed_topaz"
    DEMAND_RESPONSE = "demand_response"
    DEVICE = "device"
    DEVICE_ALERT_DIALOG = "device_alert_dialog"
    GEOFENCE_INFO = "geofence_info"
    KRYPTONITE = "kryptonite"  # Temperature Sensors
    LINK = "link"
    MESSAGE = "message"
    MESSAGE_CENTER = "message_center"
    METADATA = "metadata"
    OCCUPANCY = "occupancy"
    QUARTZ = "quartz"
    RCS_SETTINGS = "rcs_settings"
    SAFETY = "safety"
    SAFETY_SUMMARY = "safety_summary"
    SCHEDULE = "schedule"
    SHARED = "shared"
    STRUCTURE = "structure"  # General
    STRUCTURE_HISTORY = "structure_history"
    STRUCTURE_METADATA = "structure_metadata"
    TOPAZ = "topaz"  # Nest Protect
    TOPAZ_RESOURCE = "topaz_resource"
    TRACK = "track"
    TRIP = "trip"
    TUNEUPS = "tuneups"
    USER = "user"
    USER_ALERT_DIALOG = "user_alert_dialog"
    USER_SETTINGS = "user_settings"
    WIDGET_TRACK = "widget_track"
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
