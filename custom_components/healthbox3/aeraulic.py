"""The device's own duct model, evaluated locally.

The Healthbox solves `Q = C * sqrt(dP)` over its calibrated duct network -
conductance C per branch, airflow Q, pressure drop dP. Rearranged, a
branch's pressure at a given flow is `dP = (Q / C) ** 2`, and that is all
this module does.

Why compute it rather than read it: the device does publish pressures, in
`/v1/device`'s `cmode_pressures`, but a real unit - freshly
recommissioned and running normally - answers that block entirely zeroed,
while the conductances beside it stay populated (see api.py's
`_reported_pressure`). So the pressures are reconstructed from the half
that is always there.

This is not a reimplementation of Renson's arithmetic - it is Renson's
arithmetic, checked against their own answer. Renson's installer app
shows exactly these three figures on its "Installation data / Pressure
levels" screen, and that screen is served by a cloud endpoint
(`/mylio/api/devices/{id}/measurement-info-condensed-details`) whose
numbers this reproduces from purely local data. On a three-valve
installation, against that capture:

    valve pressure  5.4913427808733     vs  (38 / 16.22) ** 2
                   13.719711271584199   vs  (15 / 4.05)  ** 2
                    8.73726964030427    vs  (40 / 13.53) ** 2
    exhaust        11.682985311482758   vs  (93 / 27.2)  ** 2
    total          25.402696583066955   vs  exhaust + max(valve)

agreeing to every digit the conductances are published to.

And the device settles it outright. A unit that *does* fill in
`cmode_pressures` publishes, for all seven of its valves plus the
exhaust, exactly what this computes - to the last digit, once `c_ai` is
carried alongside `c_ij` (see `series_conductance`). Those numbers were
never a separate measurement: they are the device precomputing this same
model at nominal flow. Nothing is traded away by computing them, which
is why the entities built on this keep their identity and their history.

Two consequences worth stating plainly, since they change what these
numbers mean:

- They are the pressures **at nominal flow**, not right now. A room
  throttled to its minimum is not currently seeing its valve pressure.
  That is also true of Renson's own screen: these are commissioning
  figures, describing the ducts, not live telemetry.
- They move only when the ducts do - or when a recommissioning rewrites
  a nominal flow. That is what makes them worth watching: a sustained
  drift means a duct is fouling, not that the weather changed.
"""

from __future__ import annotations

from .api import DeviceTelemetry, HealthboxData, Room, as_float, room_valve_port


def room_nominal_flow(room: Room) -> float | None:
    """Return a room's nominal (rated reference) flow rate in m3/h."""
    param = room.parameters.get("nominal")
    return as_float(param.value) if param is not None else None


def duct_pressure(flow: float | None, conductance: float | None) -> float | None:
    """Return `(flow / conductance) ** 2`, or None if either is unusable.

    A conductance of zero or less is not a duct with infinite resistance,
    it is a duct the device has not measured - dividing by it would raise,
    and treating it as infinite pressure would be a fabrication.
    """
    if flow is None or conductance is None or conductance <= 0:
        return None
    return (flow / conductance) ** 2


def series_conductance(*conductances: float | None) -> float | None:
    """Combine conductances in series: `1 / C**2 = sum(1 / Ci**2)`.

    The device's own `HB3FlowControl::SerieC`. A branch is not one
    resistance but two in series - the duct (`c_ij`) and whatever else
    the collector entry's `c_ai` stands for - and leaving the second out
    is what separated this model from the device's own published answer
    by exactly `(Q / c_ai) ** 2` on every valve of a seven-valve unit.

    `c_ai` is 10000 on hardware seen so far, which contributes about
    9 micro-pascals at 30 m3/h. Kept anyway: it costs one term, and
    matching the device exactly is worth more than the rounding it saves.

    A missing or non-positive conductance is skipped rather than treated
    as zero resistance - see `duct_pressure`. All of them missing gives
    None, not infinity.
    """
    usable = [c for c in conductances if c is not None and c > 0]
    if not usable:
        return None
    return sum(c**-2 for c in usable) ** -0.5


def valve_pressure(room: Room, device: DeviceTelemetry | None) -> float | None:
    """Return the pressure drop across one room's branch at nominal flow."""
    if device is None:
        return None
    port = room_valve_port(room)
    if port is None:
        return None
    return duct_pressure(
        room_nominal_flow(room),
        series_conductance(
            device.valve_conductance.get(port),
            device.valve_inlet_conductance.get(port),
        ),
    )


def _total_nominal_flow(healthbox: HealthboxData) -> float | None:
    """Return the sum of every room's nominal flow, or None if none report one.

    None rather than 0.0 for an installation that reports no nominal
    anywhere: zero would be a total, and this is the absence of one.
    """
    flows = [
        flow
        for flow in (room_nominal_flow(room) for room in healthbox.rooms)
        if flow is not None
    ]
    return sum(flows) if flows else None


def exhaust_pressure(
    healthbox: HealthboxData, device: DeviceTelemetry | None
) -> float | None:
    """Return the pressure drop across the exhaust duct at nominal flow.

    The exhaust carries the whole installation, so its flow is the sum of
    every room's nominal and its conductance is the model's `c_out`.
    """
    if device is None:
        return None
    return duct_pressure(_total_nominal_flow(healthbox), device.conductance_out)


def network_pressure(
    healthbox: HealthboxData, device: DeviceTelemetry | None
) -> float | None:
    """Return the total pressure the fan has to provide at nominal flow.

    The exhaust duct plus the single worst branch - not the sum of the
    branches. They are in parallel off one collector, so the fan has to
    satisfy the one that needs the most; the others are throttled down to
    match by their own valves.

    Confirmed against Renson's own figure to six decimal places on a real
    installation: 11.682985 (exhaust) + 13.719711 (the toilet, the worst
    of three) = 25.402697 (their total).
    """
    if device is None:
        return None
    exhaust = exhaust_pressure(healthbox, device)
    if exhaust is None:
        return None
    branches = [
        pressure
        for pressure in (valve_pressure(room, device) for room in healthbox.rooms)
        if pressure is not None
    ]
    if not branches:
        return None
    return exhaust + max(branches)
