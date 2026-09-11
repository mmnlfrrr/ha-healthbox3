"""Diagnostics support for the Renson Healthbox 3 integration."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant

from .coordinator import Healthbox3ConfigEntry

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
