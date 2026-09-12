"""Sensor platform for the Renson Healthbox 3 integration.

Only the platform's entry point lives here. The entities themselves are
split by what they describe, because that is the line along which they
differ: a room sensor exists only if that particular room reports the
reading behind it, and has to be buildable again for a room that appears
later (see `sensor_room`), while a unit sensor is created once, from a
fixed list, and gated only on whether the API key is active
(`sensor_unit`).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SENSOR_TYPE_GLOBAL_AQI
from .coordinator import Healthbox3ConfigEntry, wifi_reported
from .entity import Healthbox3Entity, async_setup_rooms, async_setup_when
from .sensor_room import _room_sensors
from .sensor_unit import (
    DEVICE_SENSOR_META,
    Healthbox3ConnectionTypeSensor,
    Healthbox3DeviceErrorsSensor,
    Healthbox3DeviceSensor,
    Healthbox3EnergySensor,
    Healthbox3GlobalAqiLevelSensor,
    Healthbox3GlobalAqiSensor,
    Healthbox3GlobalVentilationLevelSensor,
    Healthbox3WifiStatusSensor,
)

# Entities only read from the coordinator; the coordinator itself
# serializes the actual device polling, so there's nothing for per-entity
# parallel updates to limit.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Healthbox3ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Healthbox 3 sensors from a config entry."""
    coordinator = entry.runtime_data
    serial = coordinator.data.healthbox.serial

    entities: list[Healthbox3Entity] = []

    if any(s.type == SENSOR_TYPE_GLOBAL_AQI for s in coordinator.data.healthbox.global_sensors):
        entities.append(Healthbox3GlobalAqiSensor(coordinator, serial))
        entities.append(Healthbox3GlobalAqiLevelSensor(coordinator, serial))

    if coordinator.use_v2:
        entities.append(Healthbox3GlobalVentilationLevelSensor(coordinator, serial))
        entities.append(Healthbox3ConnectionTypeSensor(coordinator, serial))
        entities.append(Healthbox3DeviceErrorsSensor(coordinator, serial))
        entities.append(Healthbox3EnergySensor(coordinator, serial))
        entities.extend(
            Healthbox3DeviceSensor(coordinator, serial, meta)
            for meta in DEVICE_SENSOR_META
        )

    async_add_entities(entities)

    if coordinator.use_v2:
        # Only on a unit that isn't wired - see wifi_reported. Added
        # through async_setup_when rather than in the list above because
        # the answer comes from the device and may not be in yet.
        async_setup_when(
            entry,
            async_add_entities,
            wifi_reported,
            lambda: [Healthbox3WifiStatusSensor(coordinator, serial)],
        )

    async_setup_rooms(
        entry,
        async_add_entities,
        lambda room: _room_sensors(coordinator, serial, room),
    )
