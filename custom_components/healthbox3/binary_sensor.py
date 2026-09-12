"""Binary sensor platform for the Renson Healthbox 3 integration.

Deliberately small. Two states that already have an entity elsewhere are
NOT duplicated here:

- a room's boost being active is already the on/off state of that room's
  fan entity (see fan.py), and automations can trigger on a fan's state
  just as easily as on a binary sensor's;
- the device's error list already has a sensor carrying the count and the
  latest error's details as attributes.

What's added is the one thing neither of those gives you: a plain boolean
with `device_class: problem`, which is what a "notify me if the unit
faults" automation actually wants, plus two connectivity flags that had no
representation at all.
"""

from __future__ import annotations

from typing import override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import INTERFACE_TYPE_WIFI
from .coordinator import Healthbox3ConfigEntry, Healthbox3DataUpdateCoordinator
from .entity import Healthbox3Entity

# Entities only read from the coordinator; see sensor.py.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Healthbox3ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Healthbox 3 binary sensors from a config entry."""
    coordinator = entry.runtime_data
    serial = coordinator.data.healthbox.serial

    # Added unconditionally, unlike most v2-gated entities: reporting that
    # privileged access is currently OFF is the entire point of it, so
    # gating its creation on that access being ON would be self-defeating.
    entities: list[Healthbox3Entity] = [
        Healthbox3AdvancedApiBinarySensor(coordinator, serial)
    ]

    if coordinator.use_v2:
        entities.append(Healthbox3ProblemBinarySensor(coordinator, serial))
        entities.append(Healthbox3InternetBinarySensor(coordinator, serial))

    async_add_entities(entities)


class Healthbox3ProblemBinarySensor(Healthbox3Entity, BinarySensorEntity):
    """Whether the device is currently reporting any error.

    The companion of the device errors sensor: that one carries the count
    and the latest error's code/description/severity as attributes, this
    one is the boolean an automation can trigger on directly.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "device_problem"

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_device_problem"

    @property
    @override
    def is_on(self) -> bool:
        """Return whether any device error is currently reported."""
        return bool(self.coordinator.data.errors)


class Healthbox3AdvancedApiBinarySensor(Healthbox3Entity, BinarySensorEntity):
    """Whether the API key is currently granting privileged (v2) access.

    Worth surfacing because losing it is silent and its symptoms are
    confusing: the device validates a key against Renson's servers, so a
    unit that is up and reachable on the LAN but has no internet access
    can end up with the key no longer active - at which point every
    per-room air-quality entity goes unavailable while the device itself
    looks perfectly healthy. This entity is what turns that into an
    obvious answer instead of a hunt.

    Reads the coordinator's own fallback flag rather than polling
    `/v2/api/api_key/status` on every refresh: that flag is already
    maintained from real request outcomes (see
    `_async_handle_key_invalid`), so this costs no extra request.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "advanced_api"

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_advanced_api"

    @property
    @override
    def is_on(self) -> bool:
        """Return whether privileged v2 access is currently in use."""
        return self.coordinator.use_v2


class Healthbox3InternetBinarySensor(Healthbox3Entity, BinarySensorEntity):
    """Whether the device reports having internet access.

    Not the same as being reachable: this integration talks to the unit
    purely over the LAN and keeps working without it. It matters because
    it's the precondition for the device validating its API key - so when
    the advanced-access sensor above goes off, this is the first place to
    look.

    Only ever reported on a unit attached over Wi-Fi. The figure comes
    from `/renson_core/v1/wifi/client/status`, which describes the Wi-Fi
    *client* - so on a unit wired over Ethernet it answers "no connection"
    about a radio that is simply switched off, saying nothing whatsoever
    about the cable. Shown as-is, that read as a fault on a device that is
    not only fine but demonstrably online, since a validated API key
    requires exactly the internet access it was denying. Unavailable is
    the honest answer: the device offers no way to know. `Connection type`
    sits beside it and says ETHERNET, which is the explanation.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "internet_connection"

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_internet_connection"

    @property
    @override
    def available(self) -> bool:
        """Return whether this unit is on Wi-Fi and reported its state."""
        info = self.coordinator.data.global_info
        wifi = self.coordinator.data.wifi
        return (
            super().available
            and info is not None
            and (info.interface_type or "").upper() == INTERFACE_TYPE_WIFI
            and wifi is not None
            and wifi.internet_connection is not None
        )

    @property
    @override
    def is_on(self) -> bool | None:
        """Return whether the device reports internet access."""
        wifi = self.coordinator.data.wifi
        return wifi.internet_connection if wifi is not None else None
