"""Tests for the diagnostics platform."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.helpers import device_registry as dr

from custom_components.healthbox3 import api as api_mod
from custom_components.healthbox3.const import DOMAIN
from custom_components.healthbox3.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)

from .conftest import setup_integration


async def test_diagnostics_redacts_sensitive_fields(
    hass, mock_api_client, v2_data, boost_status
):
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry_data"]["api_key"] == "**REDACTED**"
    assert diagnostics["entry_data"]["host"] == "**REDACTED**"
    assert diagnostics["healthbox"]["serial"] == "**REDACTED**"
    assert diagnostics["healthbox"]["warranty_number"] == "**REDACTED**"

    room1 = next(r for r in diagnostics["healthbox"]["rooms"] if r["id"] == 1)
    assert room1["parameters"]["valve_warranty"] == "**REDACTED**"


async def test_diagnostics_includes_boost_and_use_v2(
    hass, mock_api_client, v1_data, boost_status
):
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["use_v2"] is False
    assert diagnostics["boost"]["1"]["enable"] is False
    assert diagnostics["boost_params"]["1"]["level"] == 100.0
    assert diagnostics["boost_all_params"] == {"level": 100.0, "timeout": 900}


async def test_diagnostics_dumps_every_endpoint_the_coordinator_holds(
    hass, mock_api_client, v2_data, boost_status, device_telemetry, wifi_status
):
    """A bug report about a wrong reading is usually a bug report about one
    of the reverse-engineered endpoints.

    The dump used to carry `healthbox` and `boost` only, which left out
    decision, Breeze, per-room decisions, global info, errors, telemetry and
    Wi-Fi - the whole half of the integration most likely to be wrong, and
    the half nobody can inspect from outside.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        device=device_telemetry,
        wifi=wifi_status,
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    for key in (
        "decision",
        "breeze",
        "room_decisions",
        "global_info",
        "errors",
        "device",
        "wifi",
        "entry_options",
        "scan_interval_seconds",
    ):
        assert key in diagnostics, key

    assert diagnostics["device"]["fan"]["rpm"] is not None
    assert diagnostics["wifi"] is not None


async def test_diagnostics_redacts_the_lowercase_mac_and_ip(
    hass, mock_api_client, v2_data, boost_status
):
    """`async_redact_data` matches keys exactly, and this device's address
    arrives under two spellings: `MAC`/`IP` from the raw discovery payload,
    `mac`/`ip` from `/renson_core/v2/global`.

    Only the uppercase pair was listed, so the moment global info joined the
    dump the device's real MAC and IP would have gone into it in clear -
    which is exactly what a diagnostics file gets pasted into a public
    issue.
    """
    mock_api_client.async_get_global = AsyncMock(
        return_value=api_mod.GlobalInfo(
            firmware_version="2.6.9",
            mac="64:1c:10:00:00:01",
            ip="192.0.2.1",
        )
    )
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["global_info"]["mac"] == "**REDACTED**"
    assert diagnostics["global_info"]["ip"] == "**REDACTED**"
    assert diagnostics["global_info"]["firmware_version"] == "2.6.9"


def _device(hass, entry, serial: str, suffix: str = "") -> dr.DeviceEntry:
    """Return the registry entry for the unit, or for one of its rooms.

    `async_get_device_by_identifier` rather than `async_get_device`: the
    latter is deprecated now that identifiers are only unique within a
    config entry, and raises outright under the test frame helper.
    """
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{serial}{suffix}"), entry.entry_id
    )
    assert device is not None, suffix or "unit"
    return device


async def test_a_rooms_diagnostics_describe_that_room_only(
    hass, mock_api_client, v2_data, boost_status
):
    """With a device per room, "this room reads wrong" is the shape most
    bug reports take - and the entry's dump answers it with every other
    room attached, seven rooms' worth of readings to find the one being
    asked about.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    diagnostics = await async_get_device_diagnostics(
        hass, entry, _device(hass, entry, v2_data.serial, "_room1")
    )

    assert diagnostics["room"]["id"] == 1
    assert diagnostics["boost"]["enable"] is False
    assert diagnostics["boost_params"]["level"] == 100.0
    assert diagnostics["boost_level_max"] == 200.0
    # No other room, and nothing describing the unit as a whole.
    assert "healthbox" not in diagnostics
    assert "wifi" not in diagnostics


async def test_the_units_diagnostics_leave_the_rooms_out(
    hass, mock_api_client, v2_data, boost_status, device_telemetry, wifi_status
):
    """The unit's device is not a room: it gets what describes the
    appliance as a whole, without the per-room noise that has its own
    button one page down.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        device=device_telemetry,
        wifi=wifi_status,
    )

    diagnostics = await async_get_device_diagnostics(
        hass, entry, _device(hass, entry, v2_data.serial)
    )

    assert diagnostics["device"]["fan"]["rpm"] is not None
    assert diagnostics["wifi"] is not None
    assert diagnostics["boost_all_params"] == {"level": 100.0, "timeout": 900}
    assert "rooms" not in diagnostics["healthbox"]
    assert "boost" not in diagnostics


async def test_device_diagnostics_redact_the_same_fields(
    hass, mock_api_client, v2_data, boost_status
):
    """The narrower dump is not a looser one: a device page's download is
    pasted into an issue exactly like the entry's.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    unit = await async_get_device_diagnostics(
        hass, entry, _device(hass, entry, v2_data.serial)
    )
    room = await async_get_device_diagnostics(
        hass, entry, _device(hass, entry, v2_data.serial, "_room1")
    )

    assert unit["healthbox"]["serial"] == "**REDACTED**"
    assert unit["healthbox"]["warranty_number"] == "**REDACTED**"
    assert room["room"]["parameters"]["valve_warranty"] == "**REDACTED**"


async def test_a_room_the_device_stopped_reporting_says_so(
    hass, mock_api_client, v2_data, boost_status
):
    """A vent that has been removed leaves its device behind until someone
    deletes it, and "why is this unavailable" is precisely the report that
    device's diagnostics button exists for.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )
    device = _device(hass, entry, v2_data.serial, "_room1")

    coordinator = entry.runtime_data
    coordinator.data.healthbox.rooms = [
        room for room in v2_data.rooms if room.id != 1
    ]

    diagnostics = await async_get_device_diagnostics(hass, entry, device)

    assert diagnostics == {"room_id": 1, "room": None, "reported_by_device": False}
