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

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.healthbox3.const import DOMAIN

from .conftest import setup_integration


def _state(hass, platform: str, serial: str, unique_id_suffix: str):
    """Return the state of the entity with this unique_id, or None."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        platform, DOMAIN, f"{serial}_{unique_id_suffix}"
    )
    return hass.states.get(entity_id) if entity_id is not None else None


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
