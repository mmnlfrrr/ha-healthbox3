"""Per-room sensors for the Renson Healthbox 3 integration.

Everything here is scoped to a single ventilated room, and everything here
is conditional: a room gets the temperature sensor only if it has a
temperature sensor, the duct-model pair only if it is wired to a collector
port, and so on. `_room_sensors` is the one place that decides which, and
it runs both at setup and for any room that appears later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    PERCENTAGE,
    UnitOfPressure,
    UnitOfRatio,
    UnitOfTemperature,
    UnitOfVolumeFlowRate,
)

from .api import (
    AQI_QUALIFICATION_LEVELS,
    DeviceTelemetry,
    Room,
    Sensor,
    as_float,
    categorize_aqi_quality,
    room_legislation_code,
    room_symbol,
    room_valve_port,
)
from .aeraulic import room_nominal_flow, valve_pressure
from .const import (
    AIRFLOW_DISPLAY_PRECISION,
    CONDUCTANCE_UNIT,
    SENSOR_TYPE_AQI,
    SENSOR_TYPE_CO2,
    SENSOR_TYPE_HUMIDITY,
    SENSOR_TYPE_TEMPERATURE,
    SENSOR_TYPE_VOC,
)
from .coordinator import Healthbox3DataUpdateCoordinator
from .entity import Healthbox3Entity, RoomRef
from .icon_set import icon_name
from .zone_icons import FALLBACK_ICON, ROOM_SYMBOL_TO_ICON

from homeassistant.util import dt as dt_util


@dataclass(frozen=True, kw_only=True)
class RoomSensorMeta:
    """Presentation metadata for a room sensor type.

    `parameter_keys` lists candidate parameter names in priority order: the
    real device (firmware 2.6.9) and the Renson PDF examples disagree on
    which sub-key holds a VOC sensor's headline value (`concentration` vs
    `voc_calc_embedded`), so we try known candidates instead of hardcoding
    a single one.
    """

    translation_key: str
    parameter_keys: tuple[str, ...]
    device_class: SensorDeviceClass | None
    native_unit_of_measurement: str | None
    suggested_display_precision: int


ROOM_SENSOR_META: dict[str, RoomSensorMeta] = {
    SENSOR_TYPE_TEMPERATURE: RoomSensorMeta(
        translation_key="room_temperature",
        parameter_keys=("temperature",),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    SENSOR_TYPE_HUMIDITY: RoomSensorMeta(
        translation_key="room_humidity",
        parameter_keys=("humidity",),
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    SENSOR_TYPE_CO2: RoomSensorMeta(
        translation_key="room_co2",
        parameter_keys=("concentration",),
        device_class=SensorDeviceClass.CO2,
        native_unit_of_measurement=UnitOfRatio.PARTS_PER_MILLION,
        suggested_display_precision=0,
    ),
    SENSOR_TYPE_VOC: RoomSensorMeta(
        translation_key="room_voc",
        parameter_keys=("concentration", "voc_calc_embedded"),
        # Home Assistant's own class for a VOC *ratio* (ppm/ppb), as
        # opposed to VOLATILE_ORGANIC_COMPOUNDS, which is a density in
        # µg/m3. This reading is a ratio, and declaring it gets the
        # reading its proper icon and the same per-entity unit picker
        # every other physical reading here has.
        device_class=SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS_PARTS,
        native_unit_of_measurement=UnitOfRatio.PARTS_PER_MILLION,
        suggested_display_precision=0,
    ),
    SENSOR_TYPE_AQI: RoomSensorMeta(
        translation_key="room_aqi",
        parameter_keys=("index",),
        # An index on Renson's own 0-100+ scale, with no unit and no
        # physical quantity behind it - there is no device class for that,
        # and inventing one would only mis-describe it.
        device_class=None,
        native_unit_of_measurement=None,
        suggested_display_precision=1,
    ),
}




def _room_current_flow_rate(room: Room) -> float | None:
    """Return a room's current live flow rate in m3/h.

    Sums flow_rate across every actuator that reports one. Both real
    fixtures only ever have a single air-valve actuator per room, but the
    API schema allows more than one (Room.actuators is a list) and
    `nominal` is scoped to the whole room rather than a specific valve -
    so if a room is ever fed by more than one valve, this combines their
    live flow rates against that single room-level reference instead of
    silently reading only the first one found. An actuator reporting a
    non-numeric flow_rate is treated the same as one not reporting it at
    all - skipped, not a reason to fail the whole room's reading.
    """
    rates: list[float] = []
    for actuator in room.actuators:
        parameter = actuator.parameters.get("flow_rate")
        if parameter is None:
            continue
        rate = as_float(parameter.value)
        if rate is not None:
            rates.append(rate)
    if not rates:
        return None
    return sum(rates)


def _room_airflow_percentage(room: Room) -> float | None:
    """Return a room's current flow rate as a percentage of nominal.

    Not a 0-100 bounded value: boost can drive live flow well past
    nominal (the boost API's own level field goes up to 200%), and
    there's a nonzero floor even at rest - real-world values run roughly
    10-200%, not 0-100.
    """
    nominal = room_nominal_flow(room)
    flow_rate = _room_current_flow_rate(room)
    if not nominal or flow_rate is None:
        return None
    return flow_rate / nominal * 100


# A boost end time is recomputed from "seconds remaining" on every poll,
# so it lands a second or two apart each time even while nothing changes.
# Republishing that would fill the recorder with a timestamp that jitters
# rather than moves. Anything inside this window is treated as the same
# end time; a boost that is restarted moves it by minutes and updates.
_BOOST_END_TOLERANCE = timedelta(seconds=30)


class Healthbox3RoomBoostEndSensor(Healthbox3Entity, SensorEntity):
    """When this room's boost stops, as a timestamp.

    The device counts down in seconds, which is the shape Renson's own
    app shows ("15 min remaining"). Published as the *end time* rather
    than the remaining seconds, because that is the one Home Assistant
    renders as a live countdown - a tile shows "in 15 minutes" and keeps
    counting between polls, where a seconds-remaining number would sit
    still for fifteen of them and then jump.

    Unknown while no boost is running, not unavailable. The difference
    matters: unavailable means this integration cannot get the data, and
    Home Assistant flags it with an error in the entity list. Nothing is
    wrong when no boost is running - the device answered perfectly, and
    what it said is that there is nothing to count down. That is exactly
    what `unknown` is for.

    Unavailable is kept for the case it describes: the room's boost
    status could not be read at all.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "room_boost_end"

    def __init__(
        self,
        coordinator: Healthbox3DataUpdateCoordinator,
        serial: str,
        room_id: int,
        room_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator, serial, room=RoomRef(id=room_id, name=room_name)
        )
        self._room_id = room_id
        self._attr_unique_id = f"{serial}_room{room_id}_boost_end"
        self._end: datetime | None = None

    @property
    @override
    def available(self) -> bool:
        """Return whether this room's boost status could be read at all."""
        return super().available and self._room_id in self.coordinator.data.boost

    @property
    @override
    def native_value(self) -> datetime | None:
        """Return when the running boost stops, or None if none is."""
        status = self.coordinator.data.boost.get(self._room_id)
        if status is None or not status.enable or status.remaining <= 0:
            self._end = None
            return None
        end = dt_util.utcnow() + timedelta(seconds=status.remaining)
        if self._end is None or abs(end - self._end) > _BOOST_END_TOLERANCE:
            self._end = end
        return self._end


def _room_sensors(
    coordinator: Healthbox3DataUpdateCoordinator, serial: str, room: Room
) -> list[Healthbox3Entity]:
    """Return every sensor one room warrants.

    Which entities a room gets depends on what that room actually
    reports, so this is a series of individual checks rather than a
    fixed list. Split out of async_setup_entry so the same checks also
    run for a room that only appears later - see async_setup_rooms.
    """
    entities: list[Healthbox3Entity] = []
    for sensor in room.sensors:
        meta = ROOM_SENSOR_META.get(sensor.type)
        if meta is None:
            continue
        entities.append(
            Healthbox3RoomSensor(coordinator, serial, room.id, room.name, sensor.type, meta)
        )
        if sensor.type == SENSOR_TYPE_AQI:
            entities.append(
                Healthbox3RoomAqiLevelSensor(coordinator, serial, room.id, room.name)
            )
    if _room_airflow_percentage(room) is not None:
        entities.append(
            Healthbox3RoomAirflowSensor(coordinator, serial, room.id, room.name)
        )
    if _room_current_flow_rate(room) is not None:
        entities.append(
            Healthbox3RoomAirflowRateSensor(coordinator, serial, room.id, room.name)
        )
    if room_nominal_flow(room) is not None:
        entities.append(
            Healthbox3RoomNominalAirflowSensor(
                coordinator, serial, room.id, room.name
            )
        )
    # Unconditional: a room that has a boost fan has a boost end time the
    # moment someone starts one, and every room gets a boost fan.
    entities.append(
        Healthbox3RoomBoostEndSensor(coordinator, serial, room.id, room.name)
    )
    if room_symbol(room) is not None:
        entities.append(
            Healthbox3RoomSymbolSensor(coordinator, serial, room.id, room.name)
        )
    # Reported by some units and not others (absent from the test
    # fixture, present in a real capture), so it's per-room optional
    # rather than assumed.
    if room_legislation_code(room) is not None:
        entities.append(
            Healthbox3RoomLegislationCodeSensor(
                coordinator, serial, room.id, room.name
            )
        )
    # The duct model is keyed by collector port, so a room without a
    # valve parameter simply has no per-room pressure/conductance to
    # show - not an error, just a room these two entities skip.
    if room_valve_port(room) is not None:
        entities.append(
            Healthbox3RoomValvePortSensor(coordinator, serial, room.id, room.name)
        )
        entities.append(
            Healthbox3RoomValvePressureSensor(coordinator, serial, room.id, room.name)
        )
        entities.append(
            Healthbox3RoomConductanceSensor(coordinator, serial, room.id, room.name)
        )
    return entities


class Healthbox3RoomSensor(Healthbox3Entity, SensorEntity):
    """A single sensor (temperature/humidity/CO2/VOC/AQI) within a room."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: Healthbox3DataUpdateCoordinator,
        serial: str,
        room_id: int,
        room_name: str,
        sensor_type: str,
        meta: RoomSensorMeta,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator, serial, room=RoomRef(id=room_id, name=room_name)
        )
        self._room_id = room_id
        self._sensor_type = sensor_type
        self._meta = meta
        self._attr_translation_key = meta.translation_key
        self._attr_device_class = meta.device_class
        self._attr_native_unit_of_measurement = meta.native_unit_of_measurement
        self._attr_suggested_display_precision = meta.suggested_display_precision
        self._attr_unique_id = f"{serial}_room{room_id}_{meta.translation_key}"

    def _find_sensor(self) -> Sensor | None:
        room = next(
            (r for r in self.coordinator.data.healthbox.rooms if r.id == self._room_id),
            None,
        )
        if room is None:
            return None
        return next((s for s in room.sensors if s.type == self._sensor_type), None)

    @property
    @override
    def available(self) -> bool:
        """Return whether the sensor is reporting data.

        A sensor with an empty `parameter` dict (confirmed on real
        hardware for a CO2 sensor) is not-yet-reporting, not an error.
        """
        if not super().available:
            return False
        sensor = self._find_sensor()
        return sensor is not None and sensor.is_available

    @property
    @override
    def native_value(self) -> bool | float | str | None:
        """Return the sensor's current value."""
        sensor = self._find_sensor()
        if sensor is None:
            return None
        for key in self._meta.parameter_keys:
            if key in sensor.parameters:
                return sensor.parameters[key].value
        return None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return the main pollutant and qualification band, for the AQI
        sensor only. See `categorize_aqi_quality`'s docstring in api.py
        for the qualification band source and caveats.
        """
        if self._sensor_type != SENSOR_TYPE_AQI:
            return None
        sensor = self._find_sensor()
        if sensor is None:
            return None
        attributes: dict[str, str] = {}
        main_pollutant = sensor.parameters.get("main_pollutant")
        if main_pollutant is not None and main_pollutant.value:
            attributes["main_pollutant"] = str(main_pollutant.value)
        index = sensor.parameters.get("index")
        aqi_value = as_float(index.value) if index is not None else None
        if aqi_value is not None:
            attributes["qualification"] = categorize_aqi_quality(aqi_value)
        return attributes or None


class Healthbox3RoomAqiLevelSensor(Healthbox3Entity, SensorEntity):
    """A room's AQI qualification band, as a selectable enum state.

    Companion to Healthbox3RoomSensor's numeric AQI sensor, which keeps the
    same qualification as an `extra_state_attributes` value (unchanged, for
    anyone already templating off it) - this exists so the qualification can
    be a dashboard tile's headline value instead of something only visible
    via Developer Tools > States. A separate entity rather than swapping the
    numeric sensor's own state, since `SensorDeviceClass.ENUM` can't carry
    `SensorStateClass.MEASUREMENT` - swapping would drop the numeric
    sensor's existing history/statistics.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(AQI_QUALIFICATION_LEVELS)
    _attr_translation_key = "room_aqi_level"

    def __init__(
        self,
        coordinator: Healthbox3DataUpdateCoordinator,
        serial: str,
        room_id: int,
        room_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator, serial, room=RoomRef(id=room_id, name=room_name)
        )
        self._room_id = room_id
        self._attr_unique_id = f"{serial}_room{room_id}_aqi_level"

    def _find_sensor(self) -> Sensor | None:
        room = next(
            (r for r in self.coordinator.data.healthbox.rooms if r.id == self._room_id),
            None,
        )
        if room is None:
            return None
        return next((s for s in room.sensors if s.type == SENSOR_TYPE_AQI), None)

    @property
    @override
    def available(self) -> bool:
        """Return whether this room's AQI sensor is reporting data."""
        if not super().available:
            return False
        sensor = self._find_sensor()
        return sensor is not None and sensor.is_available

    @property
    @override
    def native_value(self) -> str | None:
        """Return the room's current AQI qualification band."""
        sensor = self._find_sensor()
        if sensor is None:
            return None
        index = sensor.parameters.get("index")
        aqi_value = as_float(index.value) if index is not None else None
        return categorize_aqi_quality(aqi_value) if aqi_value is not None else None


class Healthbox3RoomAirflowSensor(Healthbox3Entity, SensorEntity):
    """A room's current airflow, as a percentage of its valve's nominal
    (rated reference) flow rate - not a 0-100 bounded value, see
    `_room_airflow_percentage`.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "room_airflow"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = AIRFLOW_DISPLAY_PRECISION

    def __init__(
        self,
        coordinator: Healthbox3DataUpdateCoordinator,
        serial: str,
        room_id: int,
        room_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator, serial, room=RoomRef(id=room_id, name=room_name)
        )
        self._room_id = room_id
        self._attr_unique_id = f"{serial}_room{room_id}_airflow"

    def _find_room(self) -> Room | None:
        return next(
            (r for r in self.coordinator.data.healthbox.rooms if r.id == self._room_id),
            None,
        )

    @property
    @override
    def available(self) -> bool:
        """Return whether this room currently reports both flow_rate and nominal."""
        if not super().available:
            return False
        room = self._find_room()
        return room is not None and _room_airflow_percentage(room) is not None

    @property
    @override
    def native_value(self) -> float | None:
        """Return current flow rate as a percentage of nominal."""
        room = self._find_room()
        if room is None:
            return None
        return _room_airflow_percentage(room)


class _Healthbox3RoomValueSensor(Healthbox3Entity, SensorEntity):
    """Base for per-room sensors that read one number for their room.

    Subclasses implement `_room_value`; everything else (finding the room,
    availability, naming) is shared.
    """

    # Annotated, not just assigned: most rooms sensors are measurements,
    # but a commissioning constant like the valve port is not, and a
    # subclass has to be able to say so by setting None.
    _attr_state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
    _unique_id_suffix: str

    def __init__(
        self,
        coordinator: Healthbox3DataUpdateCoordinator,
        serial: str,
        room_id: int,
        room_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator, serial, room=RoomRef(id=room_id, name=room_name)
        )
        self._room_id = room_id
        self._attr_unique_id = f"{serial}_room{room_id}_{self._unique_id_suffix}"

    def _find_room(self) -> Room | None:
        return next(
            (r for r in self.coordinator.data.healthbox.rooms if r.id == self._room_id),
            None,
        )

    def _room_value(self, room: Room) -> float | None:
        raise NotImplementedError

    @property
    @override
    def available(self) -> bool:
        """Return whether this room currently reports this value."""
        return super().available and self.native_value is not None

    @property
    @override
    def native_value(self) -> float | None:
        """Return the value for this room, if reported."""
        room = self._find_room()
        return self._room_value(room) if room is not None else None


class Healthbox3RoomAirflowRateSensor(_Healthbox3RoomValueSensor):
    """A room's current airflow in m3/h.

    The absolute counterpart to the existing airflow sensor's percentage
    of nominal: needed to total or compare real extracted volumes, which
    a ratio can't express.
    """

    _attr_translation_key = "room_airflow_rate"
    _attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE
    _attr_native_unit_of_measurement = UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR
    _attr_suggested_display_precision = 1
    _unique_id_suffix = "airflow_rate"

    @override
    def _room_value(self, room: Room) -> float | None:
        return _room_current_flow_rate(room)


class Healthbox3RoomNominalAirflowSensor(_Healthbox3RoomValueSensor):
    """A room's nominal (rated reference) airflow in m3/h.

    Diagnostic: it's a commissioning constant, not a live reading, but
    without it the percentage airflow sensor can't be turned back into an
    absolute target.
    """

    _attr_translation_key = "room_nominal_airflow"
    _attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE
    _attr_native_unit_of_measurement = UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unique_id_suffix = "nominal_airflow"

    @override
    def _room_value(self, room: Room) -> float | None:
        return room_nominal_flow(room)


class Healthbox3RoomSymbolSensor(Healthbox3Entity, SensorEntity):
    """Which pictogram the device picked for a room, e.g. "BathRoom".

    Exists so a dashboard can illustrate each room without the layout
    being written by hand per install: a card templates the room's
    picture off this state. Home Assistant has no per-device icon, so a
    card is the only place a real picture can appear - this reports the
    key, it doesn't ship the artwork.

    Reports Renson's own spelling rather than a Home Assistant icon name.
    Translating it into `mdi:` icons here would be a guess dressed up as a
    fact for every value beyond the handful this unit reports.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "room_symbol"

    def __init__(
        self,
        coordinator: Healthbox3DataUpdateCoordinator,
        serial: str,
        room_id: int,
        room_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator, serial, room=RoomRef(id=room_id, name=room_name)
        )
        self._room_id = room_id
        self._attr_unique_id = f"{serial}_room{room_id}_symbol"

    @property
    @override
    def icon(self) -> str | None:
        """Return Renson's own pictogram for this room.

        Set here rather than in icons.json because the icon depends on the
        entity's state, and icon translations only take a fixed set of
        states - the symbol vocabulary is open-ended. Falls back to the
        generic house for anything unrecognised, which is what Renson's
        own picker does too.
        """
        symbol = self.native_value
        if symbol is None:
            # Nothing to draw; let icons.json's generic default apply.
            return None
        return icon_name(ROOM_SYMBOL_TO_ICON.get(symbol, FALLBACK_ICON))

    @property
    @override
    def available(self) -> bool:
        """Return whether this room still resolves to a symbol."""
        return super().available and self.native_value is not None

    @property
    @override
    def native_value(self) -> str | None:
        """Return the room's symbol key, if any."""
        room = next(
            (r for r in self.coordinator.data.healthbox.rooms if r.id == self._room_id),
            None,
        )
        return room_symbol(room) if room is not None else None


class Healthbox3RoomLegislationCodeSensor(Healthbox3Entity, SensorEntity):
    """A room's regulatory destination code, e.g. "C16" for a bathroom.

    What the ventilation standard classes the room as - which is what set
    its nominal flow when the unit was commissioned, so it's the context
    that explains why one room's Qnom is 45 m3/h and another's 26. The
    Renson app prints it beside each room's name.

    Left as the device's own string rather than expanded to a label: the
    code set is defined by the applicable standard, not by Renson, and
    guessing at codes this unit has never reported would be inventing a
    mapping. No state class - it's a commissioning constant, not a
    reading.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "room_legislation_code"

    def __init__(
        self,
        coordinator: Healthbox3DataUpdateCoordinator,
        serial: str,
        room_id: int,
        room_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator, serial, room=RoomRef(id=room_id, name=room_name)
        )
        self._room_id = room_id
        self._attr_unique_id = f"{serial}_room{room_id}_legislation_code"

    @property
    @override
    def available(self) -> bool:
        """Return whether this room still reports a code."""
        return super().available and self.native_value is not None

    @property
    @override
    def native_value(self) -> str | None:
        """Return the room's code, if reported."""
        room = next(
            (r for r in self.coordinator.data.healthbox.rooms if r.id == self._room_id),
            None,
        )
        return room_legislation_code(room) if room is not None else None


class Healthbox3RoomValvePortSensor(_Healthbox3RoomValueSensor):
    """Which collector port a room's valve is wired to.

    The number printed next to the port on the unit itself, and the one
    the Renson app lists rooms by - so it's what a floor-plan or
    schematic-style dashboard needs in order to place each room against
    the right outlet, instead of the placement being hardcoded per
    install.

    A commissioning constant, not a reading: no state class (averaging a
    port number is meaningless) and no unit.
    """

    _attr_state_class = None
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "room_valve_port"
    _unique_id_suffix = "valve_port"

    @override
    def _room_value(self, room: Room) -> float | None:
        return room_valve_port(room)


class _Healthbox3RoomDuctSensor(_Healthbox3RoomValueSensor):
    """Base for per-room readings that live in `/v1/device`'s duct model.

    Those blocks are keyed by collector port, so each lookup goes room ->
    valve port -> value; a room whose port isn't present in the model
    (not yet calibrated) reports None rather than raising.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def _duct_value(self, port: int, device: DeviceTelemetry) -> float | None:
        raise NotImplementedError

    @override
    def _room_value(self, room: Room) -> float | None:
        device = self.coordinator.data.device
        port = room_valve_port(room)
        if device is None or port is None:
            return None
        return self._duct_value(port, device)


class Healthbox3RoomValvePressureSensor(_Healthbox3RoomValueSensor):
    """The pressure drop across a room's duct at its nominal flow, in Pa.

    A room needing markedly more pressure than its neighbours for the
    same flow has a longer, narrower or more restricted duct - useful
    context when its airflow looks low. The one asking for the most is
    also what sets the fan's working point (see `network_pressure`).

    Computed from the duct model rather than read from the device: the
    pressures the device publishes are the calibration solver's scratch
    space and come back zeroed outside a sweep, while the conductance
    they would be derived from is always there. See aeraulic.py, which
    reproduces Renson's own figure for this to every published digit.

    At *nominal* flow, so it does not move when the room throttles down -
    the same thing Renson's installer app shows, and a property of the
    duct rather than a live reading.
    """

    _attr_translation_key = "room_valve_pressure"
    _attr_device_class = SensorDeviceClass.PRESSURE
    _attr_native_unit_of_measurement = UnitOfPressure.PA
    _attr_suggested_display_precision = 1
    _unique_id_suffix = "valve_pressure"

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @override
    def _room_value(self, room: Room) -> float | None:
        return valve_pressure(room, self.coordinator.data.device)


class Healthbox3RoomConductanceSensor(_Healthbox3RoomDuctSensor):
    """A room's duct conductance, the C in the solver's Q = C x sqrt(dP).

    A property of the duct itself (length, diameter, bends), so it stays
    put across profile and mode changes - which is what makes a sustained
    downward drift meaningful: a duct or valve slowly fouling up. Single
    readings are noisy (a recalibration alone moves it by a few percent),
    so it's worth a trend over weeks, not a threshold alarm.
    """

    _attr_translation_key = "room_conductance"
    _attr_native_unit_of_measurement = CONDUCTANCE_UNIT
    _attr_suggested_display_precision = 2
    _unique_id_suffix = "conductance"

    @override
    def _duct_value(self, port: int, device: DeviceTelemetry) -> float | None:
        return device.valve_conductance.get(port)
