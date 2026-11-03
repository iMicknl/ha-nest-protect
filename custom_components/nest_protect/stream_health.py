"""Track whether the Nest subscriber is still receiving updates."""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, LOGGER, STREAM_STALE_AFTER_SECONDS


class StreamHealth:
    """Mark the subscriber stream stale when Nest stops answering long-polls.

    A deadline timer is used instead of instrumenting every failure path, so
    the stream also goes stale when the subscriber task stops altogether.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize."""
        self._hass = hass
        self.signal = f"{DOMAIN}_stream_health_{entry_id}"
        self.available = True
        self._cancel_stale_timer: CALLBACK_TYPE | None = None

    @callback
    def async_record_response(self) -> None:
        """Record that Nest answered a subscribe request."""
        self.async_stop()
        self._cancel_stale_timer = async_call_later(
            self._hass, STREAM_STALE_AFTER_SECONDS, self._async_mark_stale
        )
        if not self.available:
            self.available = True
            LOGGER.info("Updates from Nest resumed; entities are available again")
            async_dispatcher_send(self._hass, self.signal)

    @callback
    def async_stop(self) -> None:
        """Cancel the pending stale check."""
        if self._cancel_stale_timer:
            self._cancel_stale_timer()
            self._cancel_stale_timer = None

    @callback
    def async_mark_unavailable(self, reason: str) -> None:
        """Mark the stream stale now instead of waiting for the deadline."""
        self.async_stop()
        self._async_set_unavailable(reason)

    @callback
    def _async_mark_stale(self, _now: datetime) -> None:
        self._cancel_stale_timer = None
        self._async_set_unavailable(
            f"No updates from Nest for {STREAM_STALE_AFTER_SECONDS // 60} minutes"
        )

    @callback
    def _async_set_unavailable(self, reason: str) -> None:
        if not self.available:
            return
        self.available = False
        LOGGER.info("%s; marking entities unavailable", reason)
        async_dispatcher_send(self._hass, self.signal)
