"""Shared base entity for the Renson Healthbox 3 integration.

The unit is registered as one device, and **each ventilated room as its own
device** linked back to it by `via_device_id`.

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

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from homeassistant.core import callback
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Room
from .const import DEVICE_TYPE, DOMAIN, ROOM_DEVICE_NAME, UNIT_DEVICE_NAME
from .coordinator import (
    Healthbox3ConfigEntry,
    Healthbox3DataUpdateCoordinator,
)


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
            unit_device_info(coordinator, serial)
            if room is None
            else _room_device(coordinator, serial, room)
        )


def async_setup_rooms(
    entry: Healthbox3ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[Room], Iterable[Entity]],
) -> None:
    """Add `build(room)`'s entities for every room, now and as rooms appear.

    Rooms are not fixed for the life of a config entry. A vent added in
    Renson's own app shows up in the next `data/current`, and until this
    existed it stayed invisible until someone thought to reload the
    integration - with no hint anywhere that a reload was what was needed.

    Only genuinely new room ids are built, so this is safe to call on
    every coordinator update: rooms that disappear are deliberately not
    torn down here (their entities go unavailable through the normal
    `available` checks, which is the honest state for a vent that has
    stopped being reported), and a room that comes back keeps the entities
    - and therefore the history - it already had.
    """
    coordinator = entry.runtime_data
    known: set[int] = set()

    @callback
    def _add_new_rooms() -> None:
        new = [
            room
            for room in coordinator.data.healthbox.rooms
            if room.id not in known
        ]
        if not new:
            return
        known.update(room.id for room in new)
        async_add_entities(
            entity for room in new for entity in build(room)
        )

    _add_new_rooms()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_rooms))


def async_setup_when(
    entry: Healthbox3ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    ready: Callable[[Healthbox3DataUpdateCoordinator], bool],
    build: Callable[[], Iterable[Entity]],
) -> None:
    """Add `build()`'s entities the first time `ready` holds - now, or later.

    For entities whose existence depends on something the device says
    rather than on something this integration configures: whether the
    unit is on Wi-Fi at all, for instance, which is read from
    `/renson_core/v2/global` and therefore simply unknown if that
    endpoint happens to fail on the poll that set the platform up.

    Deciding once, at setup, would turn one unlucky poll into entities
    missing until somebody thinks to reload the integration - with
    nothing anywhere saying that a reload is what's needed. So an
    undecided answer stays undecided and is asked again on the next poll,
    the same way `async_setup_rooms` treats a room that appears later.

    Entities are added exactly once. The listener is dropped as soon as
    they are, so this costs nothing on every subsequent poll; if the
    answer is "no", it keeps waiting, since the device is free to change
    its mind.
    """
    coordinator = entry.runtime_data

    if ready(coordinator):
        async_add_entities(build())
        return

    unsub: Callable[[], None] | None = None

    @callback
    def _unsubscribe() -> None:
        """Drop the listener, at most once - a coordinator's own remove
        callback raises if it is called a second time, and this is called
        both on success below and on unload.
        """
        nonlocal unsub
        if unsub is None:
            return
        remove, unsub = unsub, None
        remove()

    @callback
    def _add_when_ready() -> None:
        if not ready(coordinator):
            return
        _unsubscribe()
        async_add_entities(build())

    unsub = coordinator.async_add_listener(_add_when_ready)
    entry.async_on_unload(_unsubscribe)


def unit_device_info(
    coordinator: Healthbox3DataUpdateCoordinator, serial: str
) -> DeviceInfo:
    """Build the device entry for the unit itself.

    Public because async_setup_entry creates this device up front, before
    any platform, so that its registry id exists for the rooms to point
    at - see _room_device below.

    `connections`, `configuration_url` and `sw_version` are filled in from
    `/renson_core/v2/global` when it answers. The MAC is what lets Home
    Assistant recognise this unit again after it moves to another
    address; the URL turns the device page into a way into the unit's own
    web interface; the firmware version is what Home Assistant shows in
    the device header and carries into a diagnostics download. All three
    are omitted rather than guessed when the endpoint is unavailable - it
    needs an active API key, so a v1-only install simply doesn't get them.

    The firmware version belongs here rather than on an entity of its
    own: it describes the device, changes only when the device is
    updated, and Home Assistant already has a place for it. Up to 0.3.x
    this integration also published it - along with the IP and the MAC -
    as three diagnostic sensors, which said the same thing twice.

    `model_id` is the device's own product identifier, the string it uses
    for itself everywhere in its API (`device_type` in the package list,
    the product segment of the update endpoints). `model` stays the
    human-readable "Healthbox 3.0" it has always been.
    """
    info = coordinator.data.global_info
    device = DeviceInfo(
        identifiers={(DOMAIN, serial)},
        manufacturer="Renson",
        model="Healthbox 3.0",
        model_id=DEVICE_TYPE,
        name=UNIT_DEVICE_NAME,
        serial_number=serial,
    )
    if info is None:
        return device
    if info.mac:
        device["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(info.mac))}
    if info.ip:
        device["configuration_url"] = f"http://{info.ip}"
    device["sw_version"] = info.firmware_version
    return device


def _room_device(
    coordinator: Healthbox3DataUpdateCoordinator, serial: str, room: RoomRef
) -> DeviceInfo:
    """Build the device entry for one room.

    `via_device_id` points at the unit, so rooms nest under it in the UI
    rather than looking like three unrelated appliances. It wants the unit's
    *registry id*, not its identifiers, which is why async_setup_entry
    creates the unit's device before forwarding any platform and leaves the
    id on the coordinator: by the time any entity exists, a platform has
    been forwarded, so the id is set.

    That replaces the older `via_device=(DOMAIN, serial)`, which Home
    Assistant deprecated. It still worked, but logged a deprecation warning
    naming this integration on every startup, and was due to start raising
    in Home Assistant 2027.8.

    The assert is deliberate rather than a quiet `if`: a missing id would
    mean the rooms silently stop nesting, which looks like a cosmetic
    regression and would go unnoticed. Home Assistant catches this, logs it
    against the entity, and the rooms' entities are visibly absent.

    No `serial_number`: only the unit has one. The model is "Air valve",
    which is what a Renson "room" physically is from the unit's side - one
    collector port with a motorised valve on it (`"type": "air valve"` in
    the device's own actuator list).
    """
    assert coordinator.unit_device_id is not None, (
        "the unit's device must be created before any room entity exists"
    )
    return DeviceInfo(
        identifiers={(DOMAIN, f"{serial}_room{room.id}")},
        manufacturer="Renson",
        model="Air valve",
        name=ROOM_DEVICE_NAME.format(room=room.name),
        via_device_id=coordinator.unit_device_id,
    )


def room_exists(coordinator: Healthbox3DataUpdateCoordinator, room_id: int) -> bool:
    """Return whether the device currently reports a room with this id.

    Confirmed on real hardware: acting on an unknown room id returns a bare
    500 with an empty body, indistinguishable from "device is broken" - so
    entities that act on a specific room id check this first rather than
    ever sending that request.
    """
    return any(r.id == room_id for r in coordinator.data.healthbox.rooms)
