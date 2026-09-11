"""System health for the Renson Healthbox 3 integration.

What this page answers is the first round of every support exchange -
"it stopped working", "I have no CO2 sensor" - without the user having
to produce a diagnostics file first: is the device answering, what
firmware is it on, is privileged (v2) access active, how often are we
asking it, did the last ask succeed, how many rooms came back.

Nothing collected here identifies the device or its owner. No host, no
IP, no MAC, and no serial - not even indirectly through the config
entry's title, which is `Healthbox 3 (<serial>)`. Diagnostics is a file
the user deliberately attaches; this page sits in Settings and gets
pasted into forum threads as a screenshot, so the leak fixed in
diagnostics.py would be worse here. The values below are the ones that
help and carry nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components import system_health
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback

from .const import API_V2_API_KEY_STATUS, DOMAIN
from .coordinator import Healthbox3ConfigEntry

_UNKNOWN = "unknown"


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)


def _status_url(entry: Healthbox3ConfigEntry) -> str:
    """Return the URL used to test that the device answers.

    `/v2/api/api_key/status` is the very first request setup makes (see
    __init__.py), and it needs no key to answer - so "reachable" here
    means the same thing as "setup would get past its first step", not
    just "something on that IP accepted a TCP connection".
    """
    return f"http://{entry.data[CONF_HOST]}{API_V2_API_KEY_STATUS}"


def _firmware_version(entry: Healthbox3ConfigEntry) -> str:
    global_info = entry.runtime_data.data.global_info
    return global_info.firmware_version if global_info else _UNKNOWN


def _poll_interval(entry: Healthbox3ConfigEntry) -> int | str:
    interval = entry.runtime_data.update_interval
    return int(interval.total_seconds()) if interval else _UNKNOWN


# Read in this order on the page, so it goes from "is it there at all"
# down to what it answered.
_VALUES: dict[str, Callable[[Healthbox3ConfigEntry], Any]] = {
    "firmware_version": _firmware_version,
    "api_key_active": lambda entry: entry.runtime_data.use_v2,
    "poll_interval_seconds": _poll_interval,
    "last_poll_successful": lambda entry: entry.runtime_data.last_update_success,
    "rooms": lambda entry: len(entry.runtime_data.data.healthbox.rooms),
}


def _render(value: Any) -> Any:
    """Render a value for the page, spelling booleans out.

    A row reading "no" says what it means on its own; `false` next to a
    label like "API key active" reads like a field that failed to load.
    """
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


def _render_reachability(result: str | dict[str, str]) -> str:
    """Flatten a reachability result to one line.

    Only needed when there is more than one device: with a single one the
    raw result is handed to the frontend, which knows the `{"type":
    "failed", ...}` shape and translates it. Summarised into a string it
    loses that, so spell the failure out here instead.
    """
    if isinstance(result, dict):
        return f"failed ({result.get('error', _UNKNOWN)})"
    return result


async def _async_reachability_summary(
    hass: HomeAssistant, entries: list[Healthbox3ConfigEntry]
) -> str:
    results = [
        _render_reachability(
            await system_health.async_check_can_reach_url(hass, _status_url(entry))
        )
        for entry in entries
    ]
    return _summarize(results)


def _summarize(values: list[Any]) -> str:
    """Join one value per device onto a single row.

    System health labels are translated per key, so several devices
    cannot each have their own labelled row - they share one, numbered in
    setup order. An ordinal rather than the entry's title: the title
    carries the serial (see this module's docstring).
    """
    return "; ".join(
        f"#{index}: {_render(value)}" for index, value in enumerate(values, start=1)
    )


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get info for the info page."""
    entries: list[Healthbox3ConfigEntry] = hass.config_entries.async_loaded_entries(
        DOMAIN
    )
    if not entries:
        # Configured but not loaded (failed setup, or disabled): there is
        # no coordinator to read, and an empty page is honest where a page
        # of "unknown" would look like the device answering badly.
        return {}

    if len(entries) == 1:
        entry = entries[0]
        return {
            # Not awaited: the frontend renders and translates the result
            # itself, including the failure shape.
            "can_reach_device": system_health.async_check_can_reach_url(
                hass, _status_url(entry)
            ),
            **{key: _render(value(entry)) for key, value in _VALUES.items()},
        }

    return {
        "can_reach_device": await _async_reachability_summary(hass, entries),
        **{
            key: _summarize([value(entry) for entry in entries])
            for key, value in _VALUES.items()
        },
    }
