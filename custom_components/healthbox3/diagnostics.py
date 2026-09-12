"""Diagnostics support for the Renson Healthbox 3 integration."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .api import Room
from .const import DOMAIN
from .coordinator import Healthbox3ConfigEntry, Healthbox3DataUpdateCoordinator

# Anything that could identify this device or its owner: the config entry's
# own host/key, the device's serial/warranty numbers (top-level and
# per-valve), the MAC and IP (which arrive both from discovery and from
# `/renson_core/v2/global`, in different spellings - hence both cases
# below), and the handful of global parameters that can carry a
# serial-embedded device label or a home address - confirmed possible per
# the Renson API docs, even though this author's own device doesn't have
# them set.
#
# `async_redact_data` matches keys exactly, so a key that appears in more
# than one spelling has to be listed in each: `GlobalInfo` carries `mac`
# and `ip`, the raw discovery payload carries `MAC` and `IP`. Missing one
# does not warn, it just publishes the value - which is how an address
# ends up in a bug report.
TO_REDACT = {
    CONF_API_KEY,
    CONF_HOST,
    "serial",
    "warranty_number",
    "warranty",
    "valve_warranty",
    "device name",
    "street",
    "postal code",
    "city",
    "MAC",
    "IP",
    "mac",
    "ip",
}


def _dump(value: Any) -> Any:
    """Return `value` as something JSON-serialisable, or None.

    The coordinator holds most of its state as dataclasses, and every one
    of them is optional - a v1-only install has no decision data, no
    telemetry, no Wi-Fi status. Rather than repeating that check per field,
    anything that isn't a dataclass (including None) is passed through
    as-is.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: Healthbox3ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Everything the coordinator holds, not a selection of it. A bug report
    about a wrong reading is usually a bug report about one of the
    reverse-engineered endpoints, and leaving those out of the dump meant
    the one thing needed to diagnose it was the one thing missing.
    """
    coordinator = entry.runtime_data
    data = coordinator.data
    diagnostics: dict[str, Any] = {
        "entry_data": dict(entry.data),
        "entry_options": dict(entry.options),
        "use_v2": coordinator.use_v2,
        "scan_interval_seconds": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval is not None
            else None
        ),
        "healthbox": _dump(data.healthbox),
        "boost": {
            str(room_id): asdict(status) for room_id, status in data.boost.items()
        },
        "decision": _dump(data.decision),
        "breeze": _dump(data.breeze),
        "room_decisions": {
            str(room_id): asdict(decision)
            for room_id, decision in data.room_decisions.items()
        },
        "global_info": _dump(data.global_info),
        "errors": [asdict(error) for error in data.errors],
        "device": _dump(data.device),
        "wifi": _dump(data.wifi),
        "boost_params": {
            str(room_id): asdict(params)
            for room_id, params in coordinator.boost_params.items()
        },
        "boost_all_params": asdict(coordinator.boost_all_params),
    }
    return async_redact_data(diagnostics, TO_REDACT)


def _room_id(device: DeviceEntry, serial: str) -> int | None:
    """Return the room a device stands for, or None for the unit itself.

    A room's device is identified as `<serial>_room<id>` and the unit's as
    just `<serial>` - see entity.py. Anything else (a device left behind
    by an older version, say) reads as the unit, which is the safe way
    round: the unit's dump is the one that describes the whole install.
    """
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        _, separator, suffix = identifier.partition(f"{serial}_room")
        if separator and suffix.isdigit():
            return int(suffix)
    return None


def _room_diagnostics(
    coordinator: Healthbox3DataUpdateCoordinator, room: Room
) -> dict[str, Any]:
    """Return everything known about one room."""
    data = coordinator.data
    boost = data.boost.get(room.id)
    decision = data.room_decisions.get(room.id)
    params = coordinator.boost_params.get(room.id)
    return {
        "room": asdict(room),
        "boost": asdict(boost) if boost is not None else None,
        "boost_params": asdict(params) if params is not None else None,
        "boost_level_max": coordinator.level_max(room.id),
        "room_decision": asdict(decision) if decision is not None else None,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: Healthbox3ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for one device.

    With a device per room, "this room reads wrong" is the shape most bug
    reports take, and the config entry's dump answers it by including
    every other room too - seven rooms' worth of readings to find the one
    being asked about. This narrows it to the room whose page the button
    was pressed on.

    The unit's own device is not a room, so it gets what describes the
    appliance as a whole: the decision settings, telemetry, Wi-Fi, errors
    and identity, without the per-room noise. The full dump is still one
    click away, on the integration entry rather than the device.
    """
    coordinator = entry.runtime_data
    data = coordinator.data
    serial = data.healthbox.serial

    room_id = _room_id(device, serial)
    if room_id is not None:
        room = next((r for r in data.healthbox.rooms if r.id == room_id), None)
        diagnostics: dict[str, Any] = (
            _room_diagnostics(coordinator, room)
            if room is not None
            # A device for a room the unit has stopped reporting: saying
            # so is the answer, and it is usually the bug being reported.
            else {"room_id": room_id, "room": None, "reported_by_device": False}
        )
    else:
        diagnostics = {
            "use_v2": coordinator.use_v2,
            "healthbox": {
                key: value
                for key, value in asdict(data.healthbox).items()
                if key != "rooms"
            },
            "decision": _dump(data.decision),
            "breeze": _dump(data.breeze),
            "global_info": _dump(data.global_info),
            "errors": [asdict(error) for error in data.errors],
            "device": _dump(data.device),
            "wifi": _dump(data.wifi),
            "boost_all_params": asdict(coordinator.boost_all_params),
        }

    return async_redact_data(diagnostics, TO_REDACT)
