"""Time platform for the Renson Healthbox 3 integration (silent schedule)."""

from __future__ import annotations

import datetime
import logging
from typing import Any, override

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import SilentSettings
from .coordinator import Healthbox3ConfigEntry, Healthbox3DataUpdateCoordinator
from .entity import Healthbox3Entity

_LOGGER = logging.getLogger(__name__)

# Device-wide settings, changed rarely; nothing to throttle.
PARALLEL_UPDATES = 0


def _parse_time(value: object) -> datetime.time | None:
    """Return `value` as a time of day, or None if it is not one.

    The wire format is "HH:MM:SS" and real hardware has only ever sent
    that, but `datetime.time.fromisoformat` raises on anything else -
    TypeError on a non-string, ValueError on a malformed one - and an
    exception raised from `native_value` does not degrade gracefully: Home
    Assistant fails to add the entity at all, with a traceback in the log
    and no silent-schedule control anywhere in the UI.

    "Unknown" is the honest reading for a value the device sent that this
    integration cannot make sense of, and it leaves the rest of the
    platform working.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.time.fromisoformat(value)
    except ValueError:
        _LOGGER.debug("Ignoring unparseable silent schedule time %r", value)
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Healthbox3ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Healthbox 3 times from a config entry.

    Only available with an active API key - see const.py's API_V1_DECISION
    comment for why that's treated as required rather than assumed optional.
    """
    coordinator = entry.runtime_data
    if not coordinator.use_v2:
        return

    serial = coordinator.data.healthbox.serial
    async_add_entities(
        [
            Healthbox3SilentStartTime(coordinator, serial),
            Healthbox3SilentStopTime(coordinator, serial),
        ]
    )


class Healthbox3SilentTime(Healthbox3Entity, TimeEntity):
    """One edge - start or stop - of the device's silent schedule.

    The device holds seven independent windows, one per weekday, but
    offers no way to write just one; a schedule write sends all seven (see
    `async_set_silent_schedule`), which is also how the Renson app
    presents the setting. So these two entities show Monday's window as
    the reference day, and carry the other six in `schedule` for the case
    the device disagrees with itself - a real unit was found with Sunday
    starting at 10:00 and every other day at 22:00.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _edge: str  # the DaySchedule field this entity reads, per weekday

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str, key: str
    ) -> None:
        """Initialize the time entity."""
        super().__init__(coordinator, serial)
        self._attr_translation_key = key
        self._attr_unique_id = f"{serial}_{key}"

    @property
    def _silent(self) -> SilentSettings | None:
        decision = self.coordinator.data.decision
        return decision.silent if decision is not None else None

    @property
    @override
    def available(self) -> bool:
        """Return whether the device's decision data is known."""
        return super().available and self.coordinator.data.decision is not None

    @property
    @override
    def native_value(self) -> datetime.time | None:
        """Return this edge of Monday's window - see the class docstring."""
        silent = self._silent
        if silent is None:
            return None
        return _parse_time(getattr(silent, self._edge))

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return this edge per weekday, and whether they all agree.

        Without this, a device whose days differ is shown as Monday's
        value flat, with the one day that differs being the one day the
        user cannot see - and a write silently flattens it.
        """
        silent = self._silent
        if silent is None:
            return {}
        return {
            "uniform": silent.uniform,
            "schedule": {
                day: getattr(schedule, self._edge)
                for day, schedule in silent.per_day.items()
            },
        }

    async def _async_write(self, *, start_time: str, stop_time: str) -> None:
        """Send a schedule write, saying so first if it flattens the week."""
        silent = self._silent
        assert silent is not None  # HA only calls this when `available` is True
        if diverging := silent.diverging_days:
            _LOGGER.warning(
                "Setting the Healthbox silent schedule applies one window to "
                "every weekday; the device currently has a different one on "
                "%s, which this overwrites",
                ", ".join(diverging),
            )
        await self.coordinator.client.async_set_silent_schedule(
            start_time=start_time, stop_time=stop_time
        )
        await self.coordinator.async_request_refresh()


class Healthbox3SilentStartTime(Healthbox3SilentTime):
    """The time of day the silent schedule starts."""

    _edge = "start_time"

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the time entity."""
        super().__init__(coordinator, serial, "silent_start_time")

    @override
    async def async_set_value(self, value: datetime.time) -> None:
        """Set the silent schedule's start time, keeping the stop time."""
        silent = self._silent
        assert silent is not None  # HA only calls this when `available` is True
        await self._async_write(
            start_time=value.isoformat(), stop_time=silent.stop_time
        )


class Healthbox3SilentStopTime(Healthbox3SilentTime):
    """The time of day the silent schedule stops."""

    _edge = "stop_time"

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the time entity."""
        super().__init__(coordinator, serial, "silent_stop_time")

    @override
    async def async_set_value(self, value: datetime.time) -> None:
        """Set the silent schedule's stop time, keeping the start time."""
        silent = self._silent
        assert silent is not None  # HA only calls this when `available` is True
        await self._async_write(
            start_time=silent.start_time, stop_time=value.isoformat()
        )
