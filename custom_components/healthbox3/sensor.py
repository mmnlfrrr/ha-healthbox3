"""Sensor platform for the Renson Healthbox 3 integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import override

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfPressure,
    UnitOfRatio,
    UnitOfTemperature,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    AQI_QUALIFICATION_LEVELS,
    DeviceTelemetry,
    Room,
    Sensor,
    categorize_aqi_quality,
    room_valve_port,
)
from .const import (
    CONDUCTANCE_UNIT,
    ENERGY_MAX_GAP_SECONDS,
    SENSOR_TYPE_AQI,
    SENSOR_TYPE_CO2,
    SENSOR_TYPE_GLOBAL_AQI,
    SENSOR_TYPE_HUMIDITY,
    SENSOR_TYPE_TEMPERATURE,
    SENSOR_TYPE_VOC,
)
from .coordinator import Healthbox3ConfigEntry, Healthbox3DataUpdateCoordinator
from .entity import Healthbox3Entity, RoomRef

# Entities only read from the coordinator; the coordinator itself
# serializes the actual device polling, so there's nothing for per-entity
# parallel updates to limit.
PARALLEL_UPDATES = 0


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
        device_class=None,
        native_unit_of_measurement=UnitOfRatio.PARTS_PER_MILLION,
        suggested_display_precision=0,
    ),
    SENSOR_TYPE_AQI: RoomSensorMeta(
        translation_key="room_aqi",
        parameter_keys=("index",),
        device_class=None,
        native_unit_of_measurement=None,
        suggested_display_precision=1,
    ),
}

_GLOBAL_AQI_DISPLAY_PRECISION = 1
_AIRFLOW_DISPLAY_PRECISION = 0


def _as_float(value: bool | float | str | None) -> float | None:
    """Narrow a Parameter's value to a float, or None if it isn't one.

    Parameter.value is a bool/float/str/None union covering every
    parameter type across the whole API - real hardware has only ever
    sent a float for nominal/flow_rate specifically, but nothing in the
    schema actually guarantees that. A graceful "treat it as not
    reporting" fallback here, not a silent cast, matches how an empty/
    unexpected CO2 reading is already treated as unavailable rather than
    crashing - this project has been burned by "trust the device"
    assumptions before (the profile index's cross-firmware convention
    change, boost's restart-not-adjust behavior), so a real, if currently
    theoretical, type mismatch here is worth handling the same way.
    """
    return value if isinstance(value, float) else None


def _room_nominal_flow(room: Room) -> float | None:
    """Return a room's nominal (rated reference) flow rate in m3/h."""
    param = room.parameters.get("nominal")
    return _as_float(param.value) if param is not None else None


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
        rate = _as_float(parameter.value)
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
    nominal = _room_nominal_flow(room)
    flow_rate = _room_current_flow_rate(room)
    if not nominal or flow_rate is None:
        return None
    return flow_rate / nominal * 100


@dataclass(frozen=True, kw_only=True)
class DeviceSensorMeta:
    """Presentation metadata for one `/v1/device` telemetry reading.

    `value_fn` pulls the reading out of a parsed DeviceTelemetry; returning
    None means "this reading isn't currently available", which takes the
    entity unavailable rather than raising.
    """

    translation_key: str
    value_fn: Callable[[DeviceTelemetry], float | None]
    device_class: SensorDeviceClass | None
    native_unit_of_measurement: str | None
    suggested_display_precision: int
    entity_category: EntityCategory | None = None


# Power first: it's the reason most people want this endpoint at all (see
# README's Energy dashboard section). Both power figures are surfaced
# rather than one derived from the other - see DeviceTelemetry's docstring
# on why the fan-only and whole-device numbers are kept distinct.
DEVICE_SENSOR_META: tuple[DeviceSensorMeta, ...] = (
    DeviceSensorMeta(
        translation_key="device_power",
        value_fn=lambda device: device.power,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=1,
    ),
    DeviceSensorMeta(
        translation_key="fan_power",
        value_fn=lambda device: device.fan.power,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=1,
    ),
    DeviceSensorMeta(
        translation_key="fan_airflow",
        value_fn=lambda device: device.fan.flow,
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        native_unit_of_measurement=UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
        suggested_display_precision=0,
    ),
    DeviceSensorMeta(
        translation_key="fan_speed",
        value_fn=lambda device: device.fan.rpm,
        device_class=None,
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        suggested_display_precision=0,
    ),
    DeviceSensorMeta(
        translation_key="network_pressure",
        value_fn=lambda device: device.pressure_total,
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.PA,
        suggested_display_precision=1,
    ),
    DeviceSensorMeta(
        translation_key="fan_voltage",
        value_fn=lambda device: device.fan.voltage,
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    DeviceSensorMeta(
        translation_key="fan_pressure",
        value_fn=lambda device: device.fan.pressure,
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.PA,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    DeviceSensorMeta(
        translation_key="exhaust_pressure",
        value_fn=lambda device: device.pressure_exhaust,
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.PA,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    DeviceSensorMeta(
        translation_key="outlet_conductance",
        value_fn=lambda device: device.conductance_out,
        device_class=None,
        native_unit_of_measurement=CONDUCTANCE_UNIT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    DeviceSensorMeta(
        translation_key="network_leak",
        value_fn=lambda device: device.conductance_leak,
        device_class=None,
        native_unit_of_measurement=CONDUCTANCE_UNIT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Healthbox3ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Healthbox 3 sensors from a config entry."""
    coordinator = entry.runtime_data
    serial = coordinator.data.healthbox.serial

    entities: list[Healthbox3Entity] = []
    for room in coordinator.data.healthbox.rooms:
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
        if _room_nominal_flow(room) is not None:
            entities.append(
                Healthbox3RoomNominalAirflowSensor(
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

    if any(s.type == SENSOR_TYPE_GLOBAL_AQI for s in coordinator.data.healthbox.global_sensors):
        entities.append(Healthbox3GlobalAqiSensor(coordinator, serial))
        entities.append(Healthbox3GlobalAqiLevelSensor(coordinator, serial))

    if coordinator.use_v2:
        entities.append(Healthbox3WifiStatusSensor(coordinator, serial))
        entities.append(Healthbox3GlobalVentilationLevelSensor(coordinator, serial))
        entities.append(Healthbox3FirmwareVersionSensor(coordinator, serial))
        entities.append(Healthbox3DeviceErrorsSensor(coordinator, serial))
        entities.append(Healthbox3EnergySensor(coordinator, serial))
        entities.extend(
            Healthbox3DeviceSensor(coordinator, serial, meta)
            for meta in DEVICE_SENSOR_META
        )

    async_add_entities(entities)


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
        aqi_value = _as_float(index.value) if index is not None else None
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
        aqi_value = _as_float(index.value) if index is not None else None
        return categorize_aqi_quality(aqi_value) if aqi_value is not None else None


class Healthbox3RoomAirflowSensor(Healthbox3Entity, SensorEntity):
    """A room's current airflow, as a percentage of its valve's nominal
    (rated reference) flow rate - not a 0-100 bounded value, see
    `_room_airflow_percentage`.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "room_airflow"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = _AIRFLOW_DISPLAY_PRECISION

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


class Healthbox3GlobalAqiSensor(Healthbox3Entity, SensorEntity):
    """The whole-house air quality index sensor."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "global_aqi"
    _attr_suggested_display_precision = _GLOBAL_AQI_DISPLAY_PRECISION

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_global_aqi"

    def _find_sensor(self) -> Sensor | None:
        return next(
            (
                s
                for s in self.coordinator.data.healthbox.global_sensors
                if s.type == SENSOR_TYPE_GLOBAL_AQI
            ),
            None,
        )

    @property
    @override
    def available(self) -> bool:
        """Return whether the global AQI sensor is reporting data."""
        if not super().available:
            return False
        sensor = self._find_sensor()
        return sensor is not None and sensor.is_available

    @property
    @override
    def native_value(self) -> bool | float | str | None:
        """Return the global AQI value."""
        sensor = self._find_sensor()
        if sensor is None or "index" not in sensor.parameters:
            return None
        return sensor.parameters["index"].value

    @property
    @override
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return the main pollutant, the room it was measured in, and the
        qualification band. See `categorize_aqi_quality`'s docstring in
        api.py for the qualification band source and caveats - notably,
        this whole-house value is a separate aggregation, not guaranteed
        equal to `room`'s own AQI value.
        """
        sensor = self._find_sensor()
        if sensor is None:
            return None
        attributes: dict[str, str] = {}
        for key in ("main_pollutant", "room"):
            parameter = sensor.parameters.get(key)
            if parameter is not None and parameter.value:
                attributes[key] = str(parameter.value)
        index = sensor.parameters.get("index")
        aqi_value = _as_float(index.value) if index is not None else None
        if aqi_value is not None:
            attributes["qualification"] = categorize_aqi_quality(aqi_value)
        return attributes or None


class Healthbox3GlobalAqiLevelSensor(Healthbox3Entity, SensorEntity):
    """The whole-house AQI qualification band, as a selectable enum state.

    Companion to Healthbox3GlobalAqiSensor, the same way
    Healthbox3RoomAqiLevelSensor complements the per-room numeric sensor -
    see that class's docstring for why this is a separate entity rather
    than a change to the existing numeric sensor's own state.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(AQI_QUALIFICATION_LEVELS)
    _attr_translation_key = "global_aqi_level"

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_global_aqi_level"

    def _find_sensor(self) -> Sensor | None:
        return next(
            (
                s
                for s in self.coordinator.data.healthbox.global_sensors
                if s.type == SENSOR_TYPE_GLOBAL_AQI
            ),
            None,
        )

    @property
    @override
    def available(self) -> bool:
        """Return whether the global AQI sensor is reporting data."""
        if not super().available:
            return False
        sensor = self._find_sensor()
        return sensor is not None and sensor.is_available

    @property
    @override
    def native_value(self) -> str | None:
        """Return the current whole-house AQI qualification band."""
        sensor = self._find_sensor()
        if sensor is None:
            return None
        index = sensor.parameters.get("index")
        aqi_value = _as_float(index.value) if index is not None else None
        return categorize_aqi_quality(aqi_value) if aqi_value is not None else None


class Healthbox3GlobalVentilationLevelSensor(Healthbox3Entity, SensorEntity):
    """The whole-house current ventilation level, as a percentage.

    Distinct from the `Minimum ventilation level` number entity (the
    configured floor): this is the live aggregate figure, the whole-house
    counterpart to each room's airflow sensor. Not necessarily 0-100
    bounded - same reasoning as the per-room airflow sensor, so
    device_class is left unset (none of the built-in SensorDeviceClass
    members that accept a percentage unit fit a ventilation-level
    reading regardless of its bounds).
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "global_ventilation_level"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = _AIRFLOW_DISPLAY_PRECISION

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_global_ventilation_level"

    @property
    @override
    def available(self) -> bool:
        """Return whether the device's decision data is known."""
        return super().available and self.coordinator.data.decision is not None

    @property
    @override
    def native_value(self) -> float | None:
        """Return the current whole-house ventilation level."""
        decision = self.coordinator.data.decision
        return decision.global_ventilation_level if decision is not None else None


class Healthbox3FirmwareVersionSensor(Healthbox3Entity, SensorEntity):
    """The device's currently installed firmware version.

    Diagnostic rather than a primary measurement: this exists to give a
    concrete, glanceable signal that a firmware update happened, since
    several assumptions elsewhere in this client are only confirmed
    against one specific firmware version (the room profile index's
    encoding, the DHCP hostname pattern) and could plausibly change again
    on a future update without any error - see RoomDecision's docstring
    in api.py and async_step_dhcp's docstring in config_flow.py.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "firmware_version"

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_firmware_version"

    @property
    @override
    def available(self) -> bool:
        """Return whether the device's firmware version is known."""
        return super().available and self.coordinator.data.firmware_version is not None

    @property
    @override
    def native_value(self) -> str | None:
        """Return the device's current firmware version."""
        return self.coordinator.data.firmware_version


class Healthbox3DeviceErrorsSensor(Healthbox3Entity, SensorEntity):
    """The number of device-reported errors currently active.

    Complements, not replaces, the repair issues the coordinator creates
    per error (see coordinator.py's _async_reconcile_error_issues):
    repairs are for "needs your attention now" visibility in Settings ->
    Repairs, this is passive, loggable/automatable visibility (e.g.
    "notify me if this is ever above 0"). No device_class/unit fits a
    proprietary error count, same reasoning as VOC/AQI/airflow.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "device_errors"

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_device_errors"

    @property
    @override
    def native_value(self) -> int:
        """Return the number of currently-active device errors."""
        return len(self.coordinator.data.errors)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return the most recent error's details, if any are active."""
        errors = self.coordinator.data.errors
        if not errors:
            return None
        latest = max(errors, key=lambda error: error.time)
        return {
            "code": latest.code,
            "description": latest.description,
            "severity": latest.severity,
            "time": latest.time,
            "category": latest.category,
        }


class Healthbox3DeviceSensor(Healthbox3Entity, SensorEntity):
    """One device-wide telemetry reading from `/v1/device`.

    Table-driven (see DEVICE_SENSOR_META) rather than one class per
    reading: every one of these is the same "pull a float off the parsed
    telemetry" shape, differing only in presentation.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: Healthbox3DataUpdateCoordinator,
        serial: str,
        meta: DeviceSensorMeta,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._meta = meta
        self._attr_translation_key = meta.translation_key
        self._attr_device_class = meta.device_class
        self._attr_native_unit_of_measurement = meta.native_unit_of_measurement
        self._attr_suggested_display_precision = meta.suggested_display_precision
        self._attr_entity_category = meta.entity_category
        self._attr_unique_id = f"{serial}_{meta.translation_key}"

    @property
    @override
    def available(self) -> bool:
        """Return whether this reading is currently being reported."""
        return super().available and self.native_value is not None

    @property
    @override
    def native_value(self) -> float | None:
        """Return this sensor's reading from the latest telemetry."""
        device = self.coordinator.data.device
        return self._meta.value_fn(device) if device is not None else None


class _Healthbox3RoomValueSensor(Healthbox3Entity, SensorEntity):
    """Base for per-room sensors that read one number for their room.

    Subclasses implement `_room_value`; everything else (finding the room,
    availability, naming) is shared.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
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
        return _room_nominal_flow(room)


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


class Healthbox3RoomValvePressureSensor(_Healthbox3RoomDuctSensor):
    """The differential pressure across a room's valve, in Pa.

    A room needing markedly more pressure than its neighbours for the
    same flow has a longer, narrower or more restricted duct - useful
    context when its airflow looks low.
    """

    _attr_translation_key = "room_valve_pressure"
    _attr_device_class = SensorDeviceClass.PRESSURE
    _attr_native_unit_of_measurement = UnitOfPressure.PA
    _attr_suggested_display_precision = 1
    _unique_id_suffix = "valve_pressure"

    @override
    def _duct_value(self, port: int, device: DeviceTelemetry) -> float | None:
        return device.valve_pressure.get(port)


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


class Healthbox3WifiStatusSensor(Healthbox3Entity, SensorEntity):
    """The device's Wi-Fi client status, e.g. "connected".

    Reports the Wi-Fi radio's own state, which is not the same as the
    device being reachable: a unit wired over Ethernet answers this
    endpoint with a non-connected status while working perfectly. The
    SSID is carried as an attribute rather than its own entity - it's
    context for this state, not a reading that changes on its own.
    """

    _attr_translation_key = "wifi_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_wifi_status"

    @property
    @override
    def available(self) -> bool:
        """Return whether the device's Wi-Fi status is known."""
        wifi = self.coordinator.data.wifi
        return super().available and wifi is not None and wifi.status is not None

    @property
    @override
    def native_value(self) -> str | None:
        """Return the reported Wi-Fi client status."""
        wifi = self.coordinator.data.wifi
        return wifi.status if wifi is not None else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, str | None] | None:
        """Return the SSID and any reported connection error."""
        wifi = self.coordinator.data.wifi
        if wifi is None:
            return None
        return {"ssid": wifi.ssid, "connection_error": wifi.connection_error}


class Healthbox3EnergySensor(Healthbox3Entity, RestoreSensor):
    """Cumulative electrical energy, integrated from the whole-device power.

    The Healthbox reports instantaneous power, never a cumulative figure,
    so this integrates it here rather than leaving every user to wire up a
    Riemann-sum helper by hand - a unit that runs continuously is exactly
    what Home Assistant's Energy dashboard is for, and that dashboard needs
    kWh, not W.

    Trapezoidal between consecutive readings, which is what a
    left-Riemann helper would under-report on a varying load. The running
    total survives restarts via RestoreSensor; elapsed time comes from a
    monotonic clock so that a system clock correction can't inflate or
    rewind it.

    `TOTAL_INCREASING` rather than `TOTAL`: the value only ever grows, and
    the one case where it drops - a restore that found nothing and started
    back at zero - is precisely what that state class is defined to handle.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3
    _attr_translation_key = "energy"

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_energy"
        self._total_kwh = 0.0
        # (monotonic timestamp, watts) of the previous usable reading.
        self._previous: tuple[float, float] | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Restore the running total before the first coordinator update."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is None or last.native_value is None:
            return
        try:
            self._total_kwh = float(last.native_value)
        except (TypeError, ValueError):
            # A restored state that isn't a number means starting over is the
            # only safe option; TOTAL_INCREASING covers the resulting drop.
            self._total_kwh = 0.0

    @override
    def _handle_coordinator_update(self) -> None:
        """Add the energy used since the previous reading, then write state."""
        device = self.coordinator.data.device
        watts = device.power if device is not None else None
        if watts is not None:
            now = monotonic()
            previous = self._previous
            if previous is not None:
                elapsed = now - previous[0]
                if 0 < elapsed <= ENERGY_MAX_GAP_SECONDS:
                    average_watts = (previous[1] + watts) / 2
                    self._total_kwh += average_watts * elapsed / 3_600_000
            self._previous = (now, watts)
        else:
            # Drop the anchor: the next reading must not be paired with one
            # from before an outage of unknown length.
            self._previous = None
        super()._handle_coordinator_update()

    @property
    @override
    def native_value(self) -> float:
        """Return the energy accumulated so far."""
        return self._total_kwh
