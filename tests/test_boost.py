"""Tests for boost control: per-room and boost-all fan entities."""

from __future__ import annotations
from homeassistant.helpers import entity_registry as er

import copy
import logging
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.healthbox3 import api as api_mod
from custom_components.healthbox3.const import DOMAIN
from custom_components.healthbox3.const import BOOST_LEVEL_MAX
from custom_components.healthbox3.coordinator import _level_ceiling
from custom_components.healthbox3.fan import (
    _level_to_percentage,
    _percentage_to_level,
    _preset_mode_for_timeout,
    _timeout_for_preset_mode,
)

from .conftest import setup_integration

# The device name ("Healthbox 3.0" in both fixtures) becomes an entity_id
# slug prefix once has_entity_name groups every entity under one device.
# Slug de l'''appareil principal. Les entités de pièce n'''en dépendent plus :
# chaque pièce est son propre appareil, donc leur identifiant commence
# directement par le nom de la pièce (voir entity.py).
_PREFIX = "healthbox_global"
_ROOM1_ENTITY = "fan.healthbox_toilet_boost"
_ALL_ENTITY = f"fan.{_PREFIX}_boost_all"


def _boost(
    enable: bool,
    *,
    level: float = 100.0,
    timeout: int = 900,
    remaining: int = 0,
    default_level: float = 100.0,
    default_timeout: int = 900,
) -> api_mod.BoostStatus:
    return api_mod.BoostStatus(
        enable=enable,
        level=level,
        timeout=timeout,
        remaining=remaining,
        default_level=default_level,
        default_timeout=default_timeout,
    )


# --- pure rescale/preset function tests ---


@pytest.mark.parametrize(
    ("level", "expected_percentage"),
    [
        (10.0, 0),
        (200.0, 100),
        (29.0, 10),
        (105.0, 50),
        (100.0, 47),  # (100-10)/190*100 = 47.368... -> rounds to 47
    ],
)
def test_level_to_percentage(level, expected_percentage):
    assert _level_to_percentage(level, BOOST_LEVEL_MAX) == expected_percentage


@pytest.mark.parametrize(
    ("percentage", "expected_level"),
    [
        (0, 10.0),
        (100, 200.0),
        (10, 29.0),
        (50, 105.0),
    ],
)
def test_percentage_to_level(percentage, expected_level):
    assert _percentage_to_level(percentage, BOOST_LEVEL_MAX) == pytest.approx(
        expected_level
    )


@pytest.mark.parametrize(
    ("timeout", "expected_preset"),
    [
        (300, "5 min"),
        (600, "10 min"),
        (900, "15 min"),
        (1800, "30 min"),
        (2700, "45 min"),
        (3600, "1 hour"),
        (7200, "2 hours"),
        (14400, "4 hours"),
        (400, "5 min"),  # nearest to 300, not 600
        (1000, "15 min"),  # nearest to 900, not 1800
        (2000, "30 min"),  # nearest to 1800, not 2700
        (5000, "1 hour"),  # nearest to 3600, not 7200
        (10000, "2 hours"),  # nearest to 7200, not 14400
    ],
)
def test_preset_mode_for_timeout_snaps_to_nearest(timeout, expected_preset):
    assert _preset_mode_for_timeout(timeout) == expected_preset


@pytest.mark.parametrize(
    ("preset", "expected_timeout"),
    [
        ("5 min", 300),
        ("10 min", 600),
        ("15 min", 900),
        ("30 min", 1800),
        ("45 min", 2700),
        ("1 hour", 3600),
        ("2 hours", 7200),
        ("4 hours", 14400),
    ],
)
def test_timeout_for_preset_mode(preset, expected_timeout):
    assert _timeout_for_preset_mode(preset) == expected_timeout


# --- entity behavior ---


async def test_boost_fan_seeded_from_device_defaults(hass, mock_api_client, v1_data, boost_status):
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    state = hass.states.get(_ROOM1_ENTITY)
    assert state is not None
    assert state.state == "off"
    assert state.attributes["percentage"] == 0  # off, regardless of staged level
    assert state.attributes["preset_mode"] == "15 min"
    assert state.attributes["level"] == "100%"


async def test_boost_fan_reports_the_level_it_is_actually_running_at(
    hass, mock_api_client, v1_data
):
    """The running level, not the one staged for next time.

    A boost can be started from anywhere - Renson's app, the device's own
    web UI, this integration's all-rooms fan - and then the two differ.
    Reporting the staged one is how a boost running at 200% showed as
    47% here: 100% staged, rescaled onto a 10-200 range.

    The staged level is deliberately set to something else below, so that
    a regression cannot pass by coincidence.
    """
    active = _boost(True, level=200.0, default_level=100.0, remaining=300)
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=active,
    )

    state = hass.states.get(_ROOM1_ENTITY)
    assert state.state == "on"
    # 200 on a 10-200 scale is the top of it.
    assert state.attributes["percentage"] == 100
    assert state.attributes["level"] == "200%"
    assert state.attributes["remaining"] == 300


async def test_boost_fan_falls_back_to_the_staged_level_when_off(
    hass, mock_api_client, v1_data
):
    """Nothing is running, so there is no running level to report - the
    staged one is the only answer, and `percentage` is 0 either way.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=_boost(False, level=200.0, default_level=105.0),
    )

    state = hass.states.get(_ROOM1_ENTITY)
    assert state.state == "off"
    assert state.attributes["percentage"] == 0
    assert state.attributes["level"] == "105%"


async def test_boost_fan_turn_on_uses_current_settings(hass, mock_api_client, v1_data, boost_status):
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": _ROOM1_ENTITY}, blocking=True
    )

    mock_api_client.async_set_boost.assert_awaited_once_with(
        1, enable=True, level=100.0, timeout=900
    )


async def test_boost_fan_set_percentage_zero_turns_off(hass, mock_api_client, v1_data):
    active = _boost(True, remaining=300)
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=active,
    )

    await hass.services.async_call(
        "fan",
        "set_percentage",
        {"entity_id": _ROOM1_ENTITY, "percentage": 0},
        blocking=True,
    )

    mock_api_client.async_set_boost.assert_awaited_once_with(
        1, enable=False, level=100.0, timeout=900
    )


async def test_boost_fan_set_percentage_while_off_turns_on_at_new_level(
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

    await hass.services.async_call(
        "fan",
        "set_percentage",
        {"entity_id": _ROOM1_ENTITY, "percentage": 75},
        blocking=True,
    )

    mock_api_client.async_set_boost.assert_awaited_once_with(
        1, enable=True, level=_percentage_to_level(75, BOOST_LEVEL_MAX), timeout=900
    )


async def test_boost_fan_set_percentage_while_active_restarts_and_logs(
    hass, mock_api_client, v1_data, caplog
):
    active = _boost(True, level=100.0, timeout=900, remaining=279)
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=active,
    )

    with caplog.at_level(logging.INFO, logger="custom_components.healthbox3.fan"):
        await hass.services.async_call(
            "fan",
            "set_percentage",
            {"entity_id": _ROOM1_ENTITY, "percentage": 75},
            blocking=True,
        )

    mock_api_client.async_set_boost.assert_awaited_once_with(
        1, enable=True, level=_percentage_to_level(75, BOOST_LEVEL_MAX), timeout=900
    )
    assert any("Restarting active boost" in r.message for r in caplog.records)


async def test_boost_fan_set_preset_mode_while_inactive_only_stages(
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

    await hass.services.async_call(
        "fan",
        "set_preset_mode",
        {"entity_id": _ROOM1_ENTITY, "preset_mode": "1 hour"},
        blocking=True,
    )

    mock_api_client.async_set_boost.assert_not_awaited()
    assert hass.states.get(_ROOM1_ENTITY).attributes["preset_mode"] == "1 hour"


async def test_boost_fan_set_preset_mode_while_active_restarts(hass, mock_api_client, v1_data):
    active = _boost(True, level=100.0, timeout=900, remaining=300)
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=active,
    )

    await hass.services.async_call(
        "fan",
        "set_preset_mode",
        {"entity_id": _ROOM1_ENTITY, "preset_mode": "1 hour"},
        blocking=True,
    )

    mock_api_client.async_set_boost.assert_awaited_once_with(
        1, enable=True, level=100.0, timeout=3600
    )


@pytest.mark.parametrize(
    ("preset_mode", "expected_timeout"),
    [
        ("5 min", 300),
        ("4 hours", 14400),
    ],
)
async def test_boost_fan_set_preset_mode_while_active_restarts_new_presets(
    hass, mock_api_client, v1_data, caplog, preset_mode, expected_timeout
):
    active = _boost(True, level=100.0, timeout=900, remaining=300)
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=active,
    )

    with caplog.at_level(logging.INFO, logger="custom_components.healthbox3.fan"):
        await hass.services.async_call(
            "fan",
            "set_preset_mode",
            {"entity_id": _ROOM1_ENTITY, "preset_mode": preset_mode},
            blocking=True,
        )

    mock_api_client.async_set_boost.assert_awaited_once_with(
        1, enable=True, level=100.0, timeout=expected_timeout
    )
    assert any("Restarting active boost" in r.message for r in caplog.records)


async def test_boost_fan_restores_percentage_and_preset_mode_across_restart(
    hass, mock_api_client, v1_data, boost_status
):
    mock_restore_cache(
        hass,
        [State(_ROOM1_ENTITY, "off", {"percentage": 50, "preset_mode": "1 hour"})],
    )

    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    state = hass.states.get(_ROOM1_ENTITY)
    assert state.attributes["preset_mode"] == "1 hour"
    assert state.attributes["level"] == "105%"  # restored 50% -> level 105.0

    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": _ROOM1_ENTITY}, blocking=True
    )
    mock_api_client.async_set_boost.assert_awaited_once_with(
        1, enable=True, level=105.0, timeout=3600
    )


async def test_boost_fan_guards_against_a_room_removed_from_the_device(
    hass, mock_api_client, v2_data, boost_status
):
    """Confirmed on real hardware: acting on an unknown room id returns a bare
    500 with an empty body. The boost fan must check the room still exists
    before ever making that call.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )
    coordinator = entry.runtime_data

    shrunk = copy.deepcopy(v2_data)
    shrunk.rooms = [r for r in shrunk.rooms if r.id != 1]
    coordinator.data.healthbox = shrunk
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    entity_id = "fan.healthbox_toilet_boost"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "fan", "turn_on", {"entity_id": entity_id}, blocking=True
        )


async def test_boost_all_fan_reflects_all_rooms_enabled(hass, mock_api_client, v1_data):
    statuses = {room.id: _boost(False) for room in v1_data.rooms}

    async def _get_boost(room_id: int) -> api_mod.BoostStatus:
        return statuses[room_id]

    mock_api_client.async_get_boost = AsyncMock(side_effect=_get_boost)

    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
    )
    coordinator = entry.runtime_data

    assert hass.states.get(_ALL_ENTITY).state == "off"

    for room_id in statuses:
        statuses[room_id] = _boost(True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_ALL_ENTITY).state == "on"

    some_room_id = next(iter(statuses))
    statuses[some_room_id] = _boost(False)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_ALL_ENTITY).state == "off"


async def test_boost_all_fan_turn_on_calls_every_room_with_shared_params(
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

    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": _ALL_ENTITY}, blocking=True
    )

    room_ids = {room.id for room in v1_data.rooms}
    called_ids = {
        call.args[0] for call in mock_api_client.async_set_boost.await_args_list
    }
    assert called_ids == room_ids
    for call in mock_api_client.async_set_boost.await_args_list:
        assert call.kwargs == {"enable": True, "level": 100.0, "timeout": 900}


async def test_boost_all_fan_set_percentage_pushes_every_room(
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

    await hass.services.async_call(
        "fan",
        "set_percentage",
        {"entity_id": _ALL_ENTITY, "percentage": 75},
        blocking=True,
    )

    room_ids = {room.id for room in v1_data.rooms}
    called_ids = {
        call.args[0] for call in mock_api_client.async_set_boost.await_args_list
    }
    assert called_ids == room_ids
    for call in mock_api_client.async_set_boost.await_args_list:
        assert call.kwargs == {
            "enable": True,
            "level": _percentage_to_level(75, BOOST_LEVEL_MAX),
            "timeout": 900,
        }


async def test_boost_all_fan_turn_on_raises_if_any_room_fails(
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

    failing_room_id = v1_data.rooms[0].id

    async def _set_boost(room_id: int, *, enable: bool, level: float, timeout: int):
        if room_id == failing_room_id:
            raise api_mod.Healthbox3ConnectionError("boom")

    mock_api_client.async_set_boost = AsyncMock(side_effect=_set_boost)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "fan", "turn_on", {"entity_id": _ALL_ENTITY}, blocking=True
        )


# --- per-room boost scale ---


@pytest.mark.parametrize(
    ("default_level", "expected"),
    [
        (None, BOOST_LEVEL_MAX),  # nothing reported: the app's own range
        (100.0, BOOST_LEVEL_MAX),  # inside it: unchanged
        (200.0, BOOST_LEVEL_MAX),
        (270.0, 270.0),  # a kitchen commissioned to a regulatory rate
    ],
)
def test_level_ceiling(default_level, expected):
    """A room's ceiling widens to its own stored default, never narrows."""
    assert _level_ceiling(default_level) == expected


@pytest.mark.parametrize(
    ("level", "maximum", "expected_percentage"),
    [
        (270.0, 270.0, 100),  # the whole point: reachable at all
        (140.0, 270.0, 50),
        (200.0, 270.0, 73),
        (200.0, BOOST_LEVEL_MAX, 100),  # a room inside the app's range
    ],
)
def test_level_to_percentage_scales_per_room(level, maximum, expected_percentage):
    assert _level_to_percentage(level, maximum) == expected_percentage


async def test_a_room_that_stores_more_than_the_app_offers_keeps_its_own_rate(
    hass, mock_api_client, v1_data
):
    """Confirmed on real hardware: a kitchen whose `default_level` is 270%
    - the French hygro B peak extraction rate, 135 m3/h of a 50 m3/h
    nominal, written in at commissioning.

    Clamped to the app's 200%, Home Assistant staged 200 and asked that
    kitchen for 100 m3/h: a regulatory figure quietly rewritten, with
    every entity still looking perfectly plausible.
    """
    statuses = {room.id: _boost(False) for room in v1_data.rooms}
    statuses[1] = _boost(False, default_level=270.0)

    async def _get_boost(room_id: int) -> api_mod.BoostStatus:
        return statuses[room_id]

    mock_api_client.async_get_boost = AsyncMock(side_effect=_get_boost)

    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
    )
    coordinator = entry.runtime_data

    assert coordinator.level_max(1) == 270.0
    # Seeded from the device, not rounded down to what the app offers.
    assert coordinator.boost_params[1].level == 270.0

    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": _ROOM1_ENTITY, "percentage": 100}, blocking=True
    )

    mock_api_client.async_set_boost.assert_awaited_once_with(
        1, enable=True, level=270.0, timeout=900
    )


async def test_only_the_room_that_differs_changes_scale(
    hass, mock_api_client, v1_data
):
    """The reason this is per-room rather than one wider global range: a
    percentage already written into an automation for any other room has
    to keep meaning what it meant.
    """
    statuses = {room.id: _boost(False) for room in v1_data.rooms}
    statuses[1] = _boost(False, default_level=270.0)

    async def _get_boost(room_id: int) -> api_mod.BoostStatus:
        return statuses[room_id]

    mock_api_client.async_get_boost = AsyncMock(side_effect=_get_boost)

    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
    )
    coordinator = entry.runtime_data

    assert coordinator.level_max(2) == BOOST_LEVEL_MAX

    await hass.services.async_call(
        "fan",
        "turn_on",
        {"entity_id": "fan.healthbox_bathroom_boost", "percentage": 75},
        blocking=True,
    )

    mock_api_client.async_set_boost.assert_awaited_once_with(
        2, enable=True, level=_percentage_to_level(75, BOOST_LEVEL_MAX), timeout=900
    )


async def test_the_all_rooms_fan_keeps_the_apps_range(hass, mock_api_client, v1_data):
    """`Boost all` sends one level to every room at once, and a level above
    the app's range is only known to be accepted by the one room that
    stores it as its own default. Widening it here would push an untested
    figure at every other room.
    """
    statuses = {room.id: _boost(False) for room in v1_data.rooms}
    statuses[1] = _boost(False, default_level=270.0)

    async def _get_boost(room_id: int) -> api_mod.BoostStatus:
        return statuses[room_id]

    mock_api_client.async_get_boost = AsyncMock(side_effect=_get_boost)

    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
    )

    assert entry.runtime_data.level_max(None) == BOOST_LEVEL_MAX
    assert hass.states.get(_ALL_ENTITY).attributes["level_max"] == "200%"


async def test_a_fan_says_what_its_full_slider_asks_for(hass, mock_api_client, v1_data):
    """100% is not the same figure on every room now, so a slider that did
    not say which one would be unreadable.
    """
    statuses = {room.id: _boost(True) for room in v1_data.rooms}
    statuses[1] = _boost(True, default_level=270.0)

    async def _get_boost(room_id: int) -> api_mod.BoostStatus:
        return statuses[room_id]

    mock_api_client.async_get_boost = AsyncMock(side_effect=_get_boost)

    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
    )

    assert hass.states.get(_ROOM1_ENTITY).attributes["level_max"] == "270%"
    assert hass.states.get("fan.healthbox_bathroom_boost").attributes["level_max"] == "200%"


async def test_a_reconfigured_room_follows_its_new_ceiling(
    hass, mock_api_client, v1_data
):
    """The ceiling describes the device's configuration, not this
    integration's memory of it: a room re-commissioned at the unit must not
    keep answering to the range it had when Home Assistant first saw it.
    """
    statuses = {room.id: _boost(False) for room in v1_data.rooms}

    async def _get_boost(room_id: int) -> api_mod.BoostStatus:
        return statuses[room_id]

    mock_api_client.async_get_boost = AsyncMock(side_effect=_get_boost)

    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
    )
    coordinator = entry.runtime_data
    assert coordinator.level_max(1) == BOOST_LEVEL_MAX

    statuses[1] = _boost(False, default_level=270.0)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.level_max(1) == 270.0


async def test_boost_end_sensor_counts_down_and_holds_steady(
    hass, mock_api_client, v1_data
):
    """The device counts down in seconds; Home Assistant renders a
    countdown from a timestamp. So the end time is what is published -
    a tile then shows "in 5 minutes" and keeps ticking between polls.

    It must also hold still. Recomputing `now + remaining` every poll
    lands a second or two apart each time even while nothing changes, and
    republishing that would fill the recorder with jitter.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=_boost(True, remaining=300),
    )

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{v1_data.serial}_room1_boost_end"
    )
    assert entity_id is not None
    first = hass.states.get(entity_id).state
    assert first not in ("unknown", "unavailable")

    # A poll later the device says one second less; the end time is the
    # same moment and must not move.
    mock_api_client.async_get_boost = AsyncMock(
        return_value=_boost(True, remaining=299)
    )
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == first


async def test_boost_end_sensor_is_unknown_not_unavailable_when_idle(
    hass, mock_api_client, v1_data
):
    """There is no end time for something that is not going to end - but
    that is not a fault, and must not be reported as one.

    `unavailable` means this integration cannot get the data, and Home
    Assistant puts an error marker against it in the entity list. The
    device answered perfectly here; it said there is no boost. That is
    `unknown`.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=_boost(False, remaining=0),
    )

    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{v1_data.serial}_room1_boost_end"
    )
    assert hass.states.get(entity_id).state == "unknown"
