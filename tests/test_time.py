"""Entity state tests for the time platform (silent schedule)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.const import STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er

from custom_components.healthbox3.const import DOMAIN

from .conftest import setup_integration

# The device name ("Healthbox 3.0" in both fixtures) becomes an entity_id
# slug prefix once has_entity_name groups every entity under one device.
_PREFIX = "healthbox"


def _state(hass, serial: str, suffix: str):
    """Return a time entity's state, resolved through the registry."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("time", DOMAIN, f"{serial}_{suffix}")
    return hass.states.get(entity_id) if entity_id is not None else None


async def test_silent_start_time_reports_state(
    hass, mock_api_client, v2_data, boost_status, device_decision
):
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        decision=device_decision,
    )

    state = hass.states.get(f"time.{_PREFIX}_silent_start_time")
    assert state is not None
    assert state.state == "22:00:00"  # the fixture's silent:true entry


async def test_silent_stop_time_reports_state(
    hass, mock_api_client, v2_data, boost_status, device_decision
):
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        decision=device_decision,
    )

    state = hass.states.get(f"time.{_PREFIX}_silent_stop_time")
    assert state is not None
    assert state.state == "08:00:00"  # the fixture's silent:false entry


async def test_silent_times_not_created_without_api_key(
    hass, mock_api_client, v1_data, boost_status
):
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    assert hass.states.get(f"time.{_PREFIX}_silent_start_time") is None
    assert hass.states.get(f"time.{_PREFIX}_silent_stop_time") is None


async def test_silent_start_time_set_value_sends_current_stop_time(
    hass, mock_api_client, v2_data, boost_status, device_decision
):
    """Setting only the start time must still send the schedule's current
    (unchanged) stop time alongside it - the wire format has no
    "just the start" write.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        decision=device_decision,
    )

    await hass.services.async_call(
        "time",
        "set_value",
        {"entity_id": f"time.{_PREFIX}_silent_start_time", "time": "21:30:00"},
        blocking=True,
    )

    mock_api_client.async_set_silent_schedule.assert_awaited_once_with(
        start_time="21:30:00", stop_time="08:00:00"
    )


async def test_silent_stop_time_set_value_sends_current_start_time(
    hass, mock_api_client, v2_data, boost_status, device_decision
):
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        decision=device_decision,
    )

    await hass.services.async_call(
        "time",
        "set_value",
        {"entity_id": f"time.{_PREFIX}_silent_stop_time", "time": "06:45:00"},
        blocking=True,
    )

    mock_api_client.async_set_silent_schedule.assert_awaited_once_with(
        start_time="22:00:00", stop_time="06:45:00"
    )


async def test_silent_times_unavailable_when_decision_fetch_failed(
    hass, mock_api_client, v2_data, boost_status, device_decision
):
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        decision=device_decision,
    )
    coordinator = entry.runtime_data

    coordinator.data.decision = None
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert hass.states.get(f"time.{_PREFIX}_silent_start_time").state == "unavailable"
    assert hass.states.get(f"time.{_PREFIX}_silent_stop_time").state == "unavailable"


@pytest.mark.parametrize("bad", [None, 123, "not a time", "25:00:00", ""])
async def test_an_unparseable_schedule_time_reads_unknown(
    hass, mock_api_client, v2_data, boost_status, device_decision, bad
):
    """`fromisoformat` raises on anything that is not "HH:MM:SS", and an
    exception out of `native_value` does not degrade gracefully: Home
    Assistant fails to add the entity at all, leaving no silent-schedule
    control anywhere in the UI and a traceback in the log.
    """
    decision = replace(
        device_decision, silent=replace(device_decision.silent, start_time=bad)
    )
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        decision=decision,
    )

    state = _state(hass, v2_data.serial, "silent_start_time")
    assert state is not None, "the entity must still exist"
    assert state.state == STATE_UNKNOWN
    # Its sibling, whose own value is fine, is unaffected.
    assert _state(hass, v2_data.serial, "silent_stop_time").state != STATE_UNKNOWN
