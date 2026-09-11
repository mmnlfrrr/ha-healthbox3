"""Shared base entity for the Renson Healthbox 3 integration.

The unit is registered as one device, and **each ventilated room as its own
device** linked back to it via `via_device`.

That split is what makes areas usable. Home Assistant assigns areas per
device, so with every entity under a single device the only way to put the
kitchen's sensors in the Kitchen area is to move them one at a time - and
entity IDs that were auto-generated before such a move keep whatever prefix
they were given, which is how an installation ends up with entity IDs like
`sensor.bathroom_vmc_kitchen_duct_conductance`. One device per room means
one action per room instead, and every sensor in it follows.

It also shortens the entity names themselves: the room no longer has to be
repeated inside each entity's name to tell rooms apart, because the device
already carries it.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import Healthbox3DataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class RoomRef:
    """Identifies the room an entity belongs to, for its own device entry."""

    id: int
    name: str


class Healthbox3Entity(CoordinatorEntity[Healthbox3DataUpdateCoordinator]):
    """Base entity, attached either to the unit or to one of its rooms."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: Healthbox3DataUpdateCoordinator,
        serial: str,
        *,
        room: RoomRef | None = None,
    ) -> None:
        """Initialize the entity.

        Pass `room` for anything scoped to a single room; leave it out for
        anything describing the unit as a whole.
        """
        super().__init__(coordinator)
        self._serial = serial
        self._attr_device_info = (
            _unit_device(coordinator, serial)
            if room is None
            else _room_device(serial, room)
        )


def _unit_device(
    coordinator: Healthbox3DataUpdateCoordinator, serial: str
) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        manufacturer="Renson",
        model="Healthbox 3.0",
        name=coordinator.data.healthbox.description,
        serial_number=serial,
    )


def _room_device(serial: str, room: RoomRef) -> DeviceInfo:
    """Build the device entry for one room.

    `via_device` points at the unit, so rooms nest under it in the UI rather
    than looking like three unrelated appliances. The unit's own device entry
    is always created first: the binary sensor platform is set up before every
    other one (see PLATFORMS in __init__.py) and always adds at least the
    advanced-access entity, which is unit-scoped.

    No `serial_number`: only the unit has one. The model is "Air valve",
    which is what a Renson "room" physically is from the unit's side - one
    collector port with a motorised valve on it (`"type": "air valve"` in
    the device's own actuator list).
    """
    return DeviceInfo(
        identifiers={(DOMAIN, f"{serial}_room{room.id}")},
        manufacturer="Renson",
        model="Air valve",
        name=room.name,
        via_device=(DOMAIN, serial),
    )


def room_exists(coordinator: Healthbox3DataUpdateCoordinator, room_id: int) -> bool:
    """Return whether the device currently reports a room with this id.

    Confirmed on real hardware: acting on an unknown room id returns a bare
    500 with an empty body, indistinguishable from "device is broken" - so
    entities that act on a specific room id check this first rather than
    ever sending that request.
    """
    return any(r.id == room_id for r in coordinator.data.healthbox.rooms)
