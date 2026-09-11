"""Entity state tests for the /v1/device telemetry and binary sensors.

Entities are looked up through the entity registry by unique_id rather
than by a hand-written entity_id slug (the style used in
test_entities.py). Both are valid; here the names involved slugify in
ways that are easy to get subtly wrong ("Wi-Fi status", "{room_name} Duct
conductance"), and a test that silently checks the wrong entity_id would
pass by asserting `is None` about an entity that exists under another
name. Resolving by unique_id checks the identity this code actually
controls.
"""

from __future__ import annotations

import copy
import logging
from unittest.mock import patch

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.healthbox3 import api as api_mod
from custom_components.healthbox3.const import DOMAIN, ENERGY_MAX_GAP_SECONDS

from .conftest import setup_integration


def _state(hass, platform: str, serial: str, unique_id_suffix: str):
    """Return the state of the entity with this unique_id, or None."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        platform, DOMAIN, f"{serial}_{unique_id_suffix}"
    )
    return hass.states.get(entity_id) if entity_id is not None else None


def _unit_device_entry(hass, entry, serial):
    """Return the unit's device entry.

    Walks the config entry's own devices rather than calling
    `async_get_device(identifiers=...)`, which Home Assistant deprecated
    once identifiers stopped being unique across config entries.
    """
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if (DOMAIN, serial) in device.identifiers:
            return device
    return None


async def test_device_sensors_report_telemetry(
    hass, mock_api_client, v2_data, boost_status, device_telemetry
):
    """Every device-level reading reaches its entity with the right value."""
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        device=device_telemetry,
    )

    expected = {
        "device_power": 11.2413056807,
        "fan_power": 6.2013056807,
        "fan_airflow": 151.2005690344,
        "fan_speed": 570.0,
        "fan_voltage": 3.36,
        "fan_pressure": 31.4086103698,
        "network_pressure": 61.7595832219,
        "exhaust_pressure": 27.5602313830,
        "outlet_conductance": 43.8113268601,
        "network_leak": 0.5940844877,
    }
    for suffix, value in expected.items():
        state = _state(hass, "sensor", v2_data.serial, suffix)
        assert state is not None, f"{suffix} entity was not created"
        assert float(state.state) == pytest.approx(value), suffix


async def test_device_sensors_unavailable_when_device_fetch_failed(
    hass, mock_api_client, v2_data, boost_status, device_telemetry
):
    """A failed /v1/device poll takes these unavailable, not to zero."""
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        device=device_telemetry,
    )
    coordinator = entry.runtime_data

    coordinator.data.device = None
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert _state(hass, "sensor", v2_data.serial, "device_power").state == "unavailable"


async def test_device_sensors_not_created_without_api_key(
    hass, mock_api_client, v1_data, boost_status
):
    """/v1/device is gated on privileged access like every other
    reverse-engineered endpoint - see the coordinator's fetch docstring.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    assert _state(hass, "sensor", v1_data.serial, "device_power") is None


async def test_room_airflow_rate_and_nominal_report_absolute_values(
    hass, mock_api_client, v2_data, boost_status
):
    """Toilet (room 1): flow_rate=6.0 m3/h against a 30.0 m3/h nominal."""
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    rate = _state(hass, "sensor", v2_data.serial, "room1_airflow_rate")
    nominal = _state(hass, "sensor", v2_data.serial, "room1_nominal_airflow")
    assert float(rate.state) == pytest.approx(6.0)
    assert float(nominal.state) == pytest.approx(30.0)


async def test_room_duct_sensors_map_through_the_valve_port(
    hass, mock_api_client, v2_data, boost_status, device_telemetry
):
    """Room 1 is wired to collector port 1, so it must read port 1's
    figures out of the duct model - which is keyed by port, not room id.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        device=device_telemetry,
    )

    pressure = _state(hass, "sensor", v2_data.serial, "room1_valve_pressure")
    conductance = _state(hass, "sensor", v2_data.serial, "room1_conductance")
    assert float(pressure.state) == pytest.approx(34.1993518389)
    assert float(conductance.state) == pytest.approx(5.1299410471)


async def test_room_duct_sensors_not_created_without_a_valve_parameter(
    hass, mock_api_client, v2_data, boost_status, device_telemetry
):
    """A room with no valve parameter can't be joined to the duct model,
    so it simply gets no duct entities - not unavailable ones.
    """
    stripped = copy.deepcopy(v2_data)
    toilet = next(r for r in stripped.rooms if r.id == 1)
    del toilet.parameters["valve"]

    await setup_integration(
        hass,
        mock_api_client,
        serial=stripped.serial,
        healthbox_data=stripped,
        boost_status=boost_status,
        device=device_telemetry,
    )

    assert _state(hass, "sensor", stripped.serial, "room1_valve_pressure") is None
    assert _state(hass, "sensor", stripped.serial, "room1_conductance") is None
    assert _state(hass, "sensor", stripped.serial, "room1_valve_port") is None


async def test_network_sensors_report_global_info(
    hass, mock_api_client, v2_data, boost_status, firmware_version
):
    """IP, MAC and connection type all come from the one /renson_core/v2/global
    fetch the firmware version already used.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        firmware_version=firmware_version,
    )

    assert _state(hass, "sensor", v2_data.serial, "ip_address").state == "192.0.2.1"
    assert (
        _state(hass, "sensor", v2_data.serial, "mac_address").state
        == "64:1c:10:00:00:01"
    )
    assert (
        _state(hass, "sensor", v2_data.serial, "connection_type").state == "ETHERNET"
    )


async def test_network_sensors_unavailable_when_global_fetch_failed(
    hass, mock_api_client, v2_data, boost_status, firmware_version
):
    """The endpoint needs an API key and can fail on its own; when it does
    these go unavailable rather than reporting a stale address.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        firmware_version=firmware_version,
    )
    coordinator = entry.runtime_data

    coordinator.data.global_info = None
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    for suffix in ("ip_address", "mac_address", "connection_type"):
        assert _state(hass, "sensor", v2_data.serial, suffix).state == "unavailable"


async def test_unit_device_carries_mac_connection_and_configuration_url(
    hass, mock_api_client, v2_data, boost_status, firmware_version
):
    """The MAC is what lets Home Assistant recognise the unit across an
    address change, and the URL turns the device page into a way into the
    unit's own web interface.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        firmware_version=firmware_version,
    )

    device = _unit_device_entry(hass, entry, v2_data.serial)
    assert device is not None
    assert (dr.CONNECTION_NETWORK_MAC, "64:1c:10:00:00:01") in device.connections
    assert device.configuration_url == "http://192.0.2.1"


async def test_unit_device_omits_network_details_without_global_info(
    hass, mock_api_client, v1_data, boost_status
):
    """A v1-only install can't reach the endpoint at all, so the device
    entry goes without rather than carrying a guess.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    device = _unit_device_entry(hass, entry, v1_data.serial)
    assert device is not None
    assert device.connections == set()
    assert device.configuration_url is None


async def test_room_symbol_prefers_the_device_icon_over_the_room_type(
    hass, mock_api_client, v2_data, boost_status
):
    """The fixture unit ships a room whose type and icon disagree - type
    "BedRoom", icon "StudioFlat". Renson's own UIs draw the icon, so a
    dashboard templating a picture off the type would show the wrong one
    on exactly that kind of room. Room 3 is that room; room 5 is a BedRoom
    with a blank icon, which is where the type is all there is.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    assert _state(hass, "sensor", v2_data.serial, "room3_symbol").state == "StudioFlat"
    assert _state(hass, "sensor", v2_data.serial, "room5_symbol").state == "BedRoom"


async def test_room_symbol_sensor_carries_rensons_pictogram(
    hass, mock_api_client, v2_data, boost_status
):
    """The whole point of the symbol sensor: each room draws its own
    Renson pictogram, with no per-install configuration.

    Room 3's icon is "StudioFlat", which has no shippable drawing (see
    zone_icons.py) and is substituted; room 4 is a LivingRoom, which maps
    straight through.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    assert (
        _state(hass, "sensor", v2_data.serial, "room4_symbol").attributes["icon"]
        == "healthbox:living"
    )
    assert (
        _state(hass, "sensor", v2_data.serial, "room3_symbol").attributes["icon"]
        == "healthbox:bed"
    )


async def test_room_symbol_sensor_falls_back_for_an_unknown_symbol(
    hass, mock_api_client, v2_data, boost_status
):
    """A value outside the vocabulary draws the generic house rather than
    being approximated onto the nearest room type.
    """
    odd = copy.deepcopy(v2_data)
    next(r for r in odd.rooms if r.id == 1).parameters["icon"] = api_mod.Parameter(
        value="WineCellar"
    )

    await setup_integration(
        hass,
        mock_api_client,
        serial=odd.serial,
        healthbox_data=odd,
        boost_status=boost_status,
    )

    state = _state(hass, "sensor", odd.serial, "room1_symbol")
    assert state.state == "WineCellar"
    assert state.attributes["icon"] == "healthbox:house"


async def test_legislation_code_sensor_created_only_for_rooms_that_report_one(
    hass, mock_api_client, v2_data, boost_status
):
    """The fixture unit reports no legislation_code at all, and a real
    capture does - so the entity has to follow the parameter rather than
    exist for every room. A blank code counts as absent: real units carry
    blank parameters as readily as missing ones.
    """
    coded = copy.deepcopy(v2_data)
    next(r for r in coded.rooms if r.id == 1).parameters["legislation_code"] = (
        api_mod.Parameter(value="C22")
    )
    next(r for r in coded.rooms if r.id == 2).parameters["legislation_code"] = (
        api_mod.Parameter(value="   ")
    )

    await setup_integration(
        hass,
        mock_api_client,
        serial=coded.serial,
        healthbox_data=coded,
        boost_status=boost_status,
    )

    assert _state(hass, "sensor", coded.serial, "room1_legislation_code").state == "C22"
    assert _state(hass, "sensor", coded.serial, "room2_legislation_code") is None
    assert _state(hass, "sensor", coded.serial, "room3_legislation_code") is None


async def test_valve_port_sensor_reports_the_wired_port_not_the_room_id(
    hass, mock_api_client, v2_data, boost_status, device_telemetry
):
    """Room id and collector port happen to be equal on the fixture unit,
    so the port is rewired here - otherwise the test would pass just as
    well against a sensor that echoed the room id.
    """
    rewired = copy.deepcopy(v2_data)
    next(r for r in rewired.rooms if r.id == 1).parameters["valve"].value = "6"

    await setup_integration(
        hass,
        mock_api_client,
        serial=rewired.serial,
        healthbox_data=rewired,
        boost_status=boost_status,
        device=device_telemetry,
    )

    assert _state(hass, "sensor", rewired.serial, "room1_valve_port").state == "6"


async def test_valve_port_sensor_does_not_need_the_duct_model(
    hass, mock_api_client, v2_data, boost_status
):
    """Unlike pressure and conductance, the port comes from data/current,
    not from /v1/device - so it stays readable on a unit whose duct model
    isn't reported (uncalibrated, or the endpoint unreachable).
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        device=None,
    )

    assert _state(hass, "sensor", v2_data.serial, "room1_valve_port").state == "1"
    assert (
        _state(hass, "sensor", v2_data.serial, "room1_valve_pressure").state
        == "unavailable"
    )


async def test_wifi_status_sensor_reports_state_and_ssid_attribute(
    hass, mock_api_client, v2_data, boost_status, wifi_status
):
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        wifi=wifi_status,
    )

    state = _state(hass, "sensor", v2_data.serial, "wifi_status")
    assert state.state == "connected"
    assert state.attributes["ssid"] == "REDACTED"


async def test_problem_binary_sensor_follows_the_error_list(
    hass, mock_api_client, v2_data, boost_status, device_errors
):
    """On while any error is reported, back off once the list clears."""
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        errors=device_errors,
    )
    coordinator = entry.runtime_data

    # The error fixture carries two entries (real hardware has only ever
    # been seen reporting an empty list, so those are hand-written examples).
    assert device_errors
    assert _state(hass, "binary_sensor", v2_data.serial, "device_problem").state == "on"

    coordinator.data.errors = []
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert _state(hass, "binary_sensor", v2_data.serial, "device_problem").state == "off"


async def test_internet_binary_sensor_reports_device_connectivity(
    hass, mock_api_client, v2_data, boost_status, wifi_status
):
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        wifi=wifi_status,
    )

    state = _state(hass, "binary_sensor", v2_data.serial, "internet_connection")
    assert state.state == "on"


async def test_advanced_api_binary_sensor_exists_without_a_key(
    hass, mock_api_client, v1_data, boost_status
):
    """Reporting that privileged access is OFF is the point of this entity,
    so unlike every other v2-gated entity it must still be created.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    state = _state(hass, "binary_sensor", v1_data.serial, "advanced_api")
    assert state is not None
    assert state.state == "off"


async def test_advanced_api_binary_sensor_on_with_a_valid_key(
    hass, mock_api_client, v2_data, boost_status
):
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    assert _state(hass, "binary_sensor", v2_data.serial, "advanced_api").state == "on"


async def _refresh_with_power(hass, entry, watts: float | None) -> None:
    """Push one coordinator update carrying this whole-device power reading."""
    coordinator = entry.runtime_data
    if coordinator.data.device is not None:
        coordinator.data.device.power = watts
    coordinator.async_update_listeners()
    await hass.async_block_till_done()


async def test_energy_sensor_integrates_power_over_time(
    hass, mock_api_client, v2_data, boost_status, device_telemetry, device_decision
):
    """Constant 3600 W for 15 minutes must read as 0.9 kWh.

    15 minutes because that is the gap cap: a longer interval is treated as
    an outage and deliberately skipped, which the test below covers.
    Elapsed time comes from a monotonic clock inside the entity, so the test
    patches that rather than trying to make real time pass.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        device=device_telemetry,
        decision=device_decision,
    )
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    clock = [1000.0]
    with patch(
        "custom_components.healthbox3.sensor.monotonic", side_effect=lambda: clock[0]
    ):
        await _refresh_with_power(hass, entry, 3600.0)  # anchor, adds nothing
        clock[0] += ENERGY_MAX_GAP_SECONDS
        await _refresh_with_power(hass, entry, 3600.0)

    state = _state(hass, "sensor", v2_data.serial, "energy")
    assert float(state.state) == pytest.approx(0.9)


async def test_energy_sensor_uses_the_average_of_both_readings(
    hass, mock_api_client, v2_data, boost_status, device_telemetry, device_decision
):
    """Trapezoidal, not left-hand: 0 W then 7200 W averages 3600 W, so the
    same 15 minutes yields the same 0.9 kWh as a steady 3600 W above. A
    left-hand sum would have scored this interval at 0.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        device=device_telemetry,
        decision=device_decision,
    )
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    clock = [1000.0]
    with patch(
        "custom_components.healthbox3.sensor.monotonic", side_effect=lambda: clock[0]
    ):
        await _refresh_with_power(hass, entry, 0.0)
        clock[0] += ENERGY_MAX_GAP_SECONDS
        await _refresh_with_power(hass, entry, 7200.0)

    state = _state(hass, "sensor", v2_data.serial, "energy")
    assert float(state.state) == pytest.approx(0.9)


async def test_energy_sensor_skips_gaps_longer_than_the_cap(
    hass, mock_api_client, v2_data, boost_status, device_telemetry, device_decision
):
    """A long gap means the unit was unreachable - don't invent the energy."""
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        device=device_telemetry,
        decision=device_decision,
    )
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    clock = [1000.0]
    with patch(
        "custom_components.healthbox3.sensor.monotonic", side_effect=lambda: clock[0]
    ):
        await _refresh_with_power(hass, entry, 3600.0)
        clock[0] += ENERGY_MAX_GAP_SECONDS + 1
        await _refresh_with_power(hass, entry, 3600.0)

    state = _state(hass, "sensor", v2_data.serial, "energy")
    assert float(state.state) == pytest.approx(0.0)


async def test_energy_sensor_never_pairs_across_an_outage(
    hass, mock_api_client, v2_data, boost_status, device_telemetry, device_decision
):
    """A reading that goes missing drops the anchor, so the next one can't be
    paired with a stale value from before the outage.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        device=device_telemetry,
        decision=device_decision,
    )
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    clock = [1000.0]
    with patch(
        "custom_components.healthbox3.sensor.monotonic", side_effect=lambda: clock[0]
    ):
        await _refresh_with_power(hass, entry, 3600.0)
        clock[0] += 60.0
        await _refresh_with_power(hass, entry, None)  # unreadable
        clock[0] += 60.0
        await _refresh_with_power(hass, entry, 3600.0)  # re-anchors only

    state = _state(hass, "sensor", v2_data.serial, "energy")
    assert float(state.state) == pytest.approx(0.0)


async def test_energy_sensor_is_an_energy_dashboard_total(
    hass, mock_api_client, v2_data, boost_status, device_telemetry
):
    """The Energy dashboard only accepts kWh with state_class total_increasing."""
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        device=device_telemetry,
    )

    state = _state(hass, "sensor", v2_data.serial, "energy")
    assert state.attributes["device_class"] == "energy"
    assert state.attributes["state_class"] == "total_increasing"
    assert state.attributes["unit_of_measurement"] == "kWh"


async def test_every_room_nests_under_the_unit(
    hass, mock_api_client, v2_data, boost_status
):
    """The whole point of one device per room is that they hang off the unit.

    Nothing asserted this until the link moved from `via_device` (a pair of
    identifiers) to `via_device_id` (the unit's registry id). The distinction
    matters: a wrong or missing id does not raise, it just leaves the rooms
    standing next to the unit as unrelated appliances, which reads as a
    cosmetic quirk rather than a bug.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    unit = _unit_device_entry(hass, entry, v2_data.serial)
    assert unit is not None

    registry = dr.async_get(hass)
    rooms = [
        device
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        if device.id != unit.id
    ]
    assert len(rooms) == len(v2_data.rooms)
    assert {device.via_device_id for device in rooms} == {unit.id}


async def test_setup_logs_no_deprecation_warning(
    hass, caplog, mock_api_client, v2_data, boost_status
):
    """Home Assistant reports deprecated API use against the integration by
    name, in the user's own log, with a link to this project's issue tracker
    and the version it will start failing in.

    That is a user-visible defect even while everything still works, and it
    is invisible to every other test here - `via_device` produced three of
    these on every single startup and the suite stayed green. Scoped to this
    integration's own reports so an unrelated warning elsewhere in Home
    Assistant does not fail it.
    """
    caplog.set_level(logging.WARNING)
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    reports = [
        record.getMessage()
        for record in caplog.records
        if f"custom integration '{DOMAIN}'" in record.getMessage()
        and "deprecated" in record.getMessage()
    ]
    assert not reports
