"""Enums for Nest Protect."""
from enum import StrEnum, unique
import logging

_LOGGER = logging.getLogger(__name__)


@unique
class BucketType(StrEnum):
    """Bucket types."""

    KRYPTONITE = "kryptonite"
    TOPAZ = "topaz"
    WHERE = "where"

    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value):  # type: ignore
        _LOGGER.warning(f"Unsupported value {value} has been returned for {cls}")

        return cls.UNKNOWN
