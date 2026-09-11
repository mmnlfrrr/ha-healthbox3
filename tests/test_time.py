"""Entity state tests for the time platform (silent schedule)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.const import STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er

from custom_components.healthbox3 import api as api_mod
from custom_components.healthbox3.const import DOMAIN

from .conftest import setup_integration

# The device name ("Healthbox 3.0" in both fixtures) becomes an entity_id
# slug prefix once has_entity_name groups every entity under one device.
_PREFIX = "healthbox_3_0"


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


async def test_the_whole_week_is_read_not_just_monday(real_decision):
    """A real device was found with Sunday's silent window starting at
    10:00 and every other day's at 22:00.

    Only `monday` used to be read, on the assumption that the device
    always holds one window repeated seven times - so the one day that
    differed was the one day Home Assistant could not show, and nothing
    anywhere said so.
    """
    silent = real_decision.silent

    assert silent.start_time == "22:00:00"  # Monday, the reference day
    assert silent.per_day["sunday"].start_time == "10:00:00"
    assert silent.uniform is False
    assert silent.diverging_days == ["sunday"]


async def test_a_uniform_week_reads_as_uniform(device_decision):
    """The common case - every day the same - must not start reporting a
    divergence that isn't there.
    """
    assert device_decision.silent.uniform is True
    assert device_decision.silent.diverging_days == []


async def test_a_day_is_read_by_its_flag_not_its_position(decision_raw):
    """Nothing promises the two entries arrive in a fixed order, and
    reading them positionally would silently swap start and stop the day a
    firmware sends them the other way round - a silent period inverted,
    with both entities still looking perfectly plausible.
    """
    raw = {**decision_raw["silent"]}
    raw["monday"] = list(reversed(raw["monday"]))

    silent = api_mod._parse_silent(raw)

    assert silent.start_time == "22:00:00"
    assert silent.stop_time == "08:00:00"


@pytest.mark.parametrize(
    "broken",
    [
        [],
        [{"silent": True, "time": "22:00:00"}],  # no stop entry
        "not a list",
        None,
        [{"time": "22:00:00"}, {"time": "08:00:00"}],  # no flags
    ],
)
async def test_a_day_the_device_sends_badly_is_skipped(decision_raw, broken):
    """A malformed day must not take the decision fetch down with it: that
    fetch also carries demand control, minimum ventilation and Breeze, so
    raising here would cost far more than the schedule it came from.
    """
    raw = {**decision_raw["silent"], "sunday": broken}

    silent = api_mod._parse_silent(raw)

    assert "sunday" not in silent.per_day
    assert silent.start_time == "22:00:00"  # Monday still read
    assert silent.uniform is True  # nothing left that disagrees


async def test_a_missing_monday_leaves_the_rest_working(decision_raw):
    """Monday is the reference day, so losing it costs the displayed times
    - but not Silent's enable/reduction, nor anything else in the same
    response.
    """
    raw = {**decision_raw["silent"], "monday": []}

    silent = api_mod._parse_silent(raw)

    assert silent.start_time == ""  # reads as unknown on the entity
    assert silent.reduction == 5.0
    assert silent.per_day["sunday"].start_time == "22:00:00"


async def test_silent_times_carry_the_week_in_their_attributes(
    hass, mock_api_client, v2_data, boost_status, real_decision
):
    """Monday's value alone is a half-truth on a device whose days differ.

    The attributes are what makes the difference visible at all, since the
    entity itself can only ever show one time.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        decision=real_decision,
    )

    state = _state(hass, v2_data.serial, "silent_start_time")
    assert state.state == "22:00:00"
    assert state.attributes["uniform"] is False
    assert state.attributes["schedule"]["sunday"] == "10:00:00"
    assert state.attributes["schedule"]["monday"] == "22:00:00"

    stop = _state(hass, v2_data.serial, "silent_stop_time")
    assert stop.attributes["uniform"] is False
    # Every day stops at the same time here - only the start diverges.
    assert set(stop.attributes["schedule"].values()) == {"08:00:00"}


async def test_setting_a_time_says_it_overwrites_the_other_days(
    hass, mock_api_client, v2_data, boost_status, real_decision, caplog
):
    """The device has no "just this day" write - a schedule write sends all
    seven. On a device whose days differ that silently destroys the odd one
    out, so it is at least said out loud.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        decision=real_decision,
    )

    await hass.services.async_call(
        "time",
        "set_value",
        {
            "entity_id": f"time.{_PREFIX}_silent_start_time",
            "time": "23:30:00",
        },
        blocking=True,
    )

    mock_api_client.async_set_silent_schedule.assert_called_once_with(
        start_time="23:30:00", stop_time="08:00:00"
    )
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and "silent schedule" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "sunday" in warnings[0]
