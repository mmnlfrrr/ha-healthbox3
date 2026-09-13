"""The duct model, held to Renson's own answer.

Every expected number in `test_the_model_reproduces_rensons_own_figures`
comes from a single real capture of Renson's installer app on a
three-valve installation: the cloud endpoint behind its "Installation
data / Pressure levels" screen
(`/mylio/api/devices/{id}/measurement-info-condensed-details`). The
inputs are what the *device* publishes locally on the same installation.

That is the whole point of this module: the same three figures, from
local data alone, with no cloud account and no `cmode_pressures` - which
a unit outside a calibration sweep answers entirely zeroed.
"""

from __future__ import annotations

import pytest

from custom_components.healthbox3 import api as api_mod
from custom_components.healthbox3.aeraulic import (
    duct_pressure,
    exhaust_pressure,
    network_pressure,
    room_nominal_flow,
    valve_pressure,
)

# The captured installation: room id -> (valve port, Qnom, duct conductance).
_ROOMS = {1: (1, 38.0, 16.22), 2: (2, 15.0, 4.05), 3: (6, 40.0, 13.53)}
_CONDUCTANCE_OUT = 27.2

# What Renson answered for it, to the digit.
_RENSON_VALVE_PRESSURE = {1: 5.4913427808733, 2: 13.719711271584199, 3: 8.73726964030427}
_RENSON_EXHAUST_PRESSURE = 11.682985311482758
_RENSON_TOTAL_PRESSURE = 25.402696583066955

# The conductances are published to four significant figures, so that is
# all the agreement that can be asked for - not a tolerance on the model.
_TOLERANCE = 0.01


def _room(room_id: int) -> api_mod.Room:
    port, nominal, _ = _ROOMS[room_id]
    return api_mod.Room(
        id=room_id,
        name=f"room{room_id}",
        type="BathRoom",
        parameters={
            "nominal": api_mod.Parameter(value=nominal, unit="m3/h"),
            api_mod.ROOM_PARAM_VALVE: api_mod.Parameter(value=str(port)),
        },
    )


def _healthbox() -> api_mod.HealthboxData:
    return api_mod.HealthboxData(
        device_type="HEALTHBOX3",
        description="Healthbox 3.0",
        serial="serial",
        warranty_number="warranty",
        rooms=[_room(room_id) for room_id in _ROOMS],
    )


def _device() -> api_mod.DeviceTelemetry:
    return api_mod.DeviceTelemetry(
        conductance_out=_CONDUCTANCE_OUT,
        valve_conductance={
            port: conductance for port, _, conductance in _ROOMS.values()
        },
    )


def test_the_model_reproduces_rensons_own_figures():
    """Local conductances in, Renson's installer screen out."""
    healthbox, device = _healthbox(), _device()

    for room in healthbox.rooms:
        assert valve_pressure(room, device) == pytest.approx(
            _RENSON_VALVE_PRESSURE[room.id], abs=_TOLERANCE
        )

    assert exhaust_pressure(healthbox, device) == pytest.approx(
        _RENSON_EXHAUST_PRESSURE, abs=_TOLERANCE
    )
    assert network_pressure(healthbox, device) == pytest.approx(
        _RENSON_TOTAL_PRESSURE, abs=_TOLERANCE
    )


def test_the_total_is_the_exhaust_plus_the_worst_branch_not_their_sum():
    """The branches hang off one collector in parallel, so the fan has to
    satisfy the one that needs the most - the others are throttled down to
    match by their own valves. Summing them would roughly double the
    answer, which is the mistake this pins down.
    """
    healthbox, device = _healthbox(), _device()

    branches = [valve_pressure(room, device) for room in healthbox.rooms]
    exhaust = exhaust_pressure(healthbox, device)
    assert exhaust is not None

    assert network_pressure(healthbox, device) == pytest.approx(
        exhaust + max(b for b in branches if b is not None)
    )
    assert network_pressure(healthbox, device) != pytest.approx(
        exhaust + sum(b for b in branches if b is not None)
    )


def test_an_unmeasured_duct_reports_nothing_rather_than_infinity():
    """A conductance of zero is a duct the device has not measured, not
    one with infinite resistance: dividing by it would raise, and calling
    it infinite pressure would be a fabrication.
    """
    assert duct_pressure(38.0, 0.0) is None
    assert duct_pressure(38.0, -1.0) is None
    assert duct_pressure(38.0, None) is None
    assert duct_pressure(None, 16.22) is None


def test_a_room_without_a_nominal_flow_reports_nothing():
    room = api_mod.Room(id=9, name="room9", type="Toilet", parameters={})
    assert room_nominal_flow(room) is None
    assert valve_pressure(room, _device()) is None


def test_everything_is_unavailable_without_the_duct_model():
    """No `/v1/device` read - no API key, or the fetch failed - means no
    conductances, and these are unavailable rather than guessed.
    """
    healthbox = _healthbox()
    assert valve_pressure(healthbox.rooms[0], None) is None
    assert exhaust_pressure(healthbox, None) is None
    assert network_pressure(healthbox, None) is None


def test_an_installation_reporting_no_nominal_flow_has_no_total():
    """Zero would be a total. This is the absence of one."""
    healthbox = api_mod.HealthboxData(
        device_type="HEALTHBOX3",
        description="Healthbox 3.0",
        serial="serial",
        warranty_number="warranty",
        rooms=[api_mod.Room(id=9, name="room9", type="Toilet", parameters={})],
    )
    assert exhaust_pressure(healthbox, _device()) is None
    assert network_pressure(healthbox, _device()) is None


def test_the_model_reproduces_the_devices_own_published_pressures(
    v1_device_raw, v2_data
):
    """The strongest evidence there is that nothing is lost by computing.

    A device that *does* fill in `cmode_pressures` - the seven-valve unit
    this fixture came from - publishes numbers that are this same model,
    evaluated at nominal flow, to seven significant figures. The exhaust
    matches to the last digit it publishes.

    So `cmode_pressures` was never a separate measurement to be traded
    away for a calculation: it is the device precomputing what it could
    have left us to work out, and zeroing it when it has nothing. An
    installation that had real values keeps reading the same quantity it
    always did - same entity, same unique id, same history, no step in
    the graph.
    """
    device = api_mod._parse_device(v1_device_raw)
    assert device.pressure_exhaust is not None

    assert exhaust_pressure(v2_data, device) == pytest.approx(
        device.pressure_exhaust, rel=1e-12
    )
    assert network_pressure(v2_data, device) == pytest.approx(
        device.pressure_total, rel=1e-12
    )
    for room in v2_data.rooms:
        assert valve_pressure(room, device) == pytest.approx(
            device.valve_pressure[api_mod.room_valve_port(room)], rel=1e-12
        )
