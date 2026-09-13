"""Unit-level sensors for the Renson Healthbox 3 integration.

Everything here describes the appliance as a whole rather than one room:
the whole-house air quality index and ventilation level, the fan and duct
telemetry from `/v1/device`, the identity and network readings from
`/renson_core/v2/global`, the device error count, Wi-Fi status, and the
energy total integrated from power.

All of it beyond the global air quality index needs an active API key -
see const.py's API_V1_DECISION comment for why that is treated as
required rather than assumed optional.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
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
    UnitOfVolumeFlowRate,
)

from .api import (
    AQI_QUALIFICATION_LEVELS,
    DeviceTelemetry,
    Sensor,
    as_float,
    categorize_aqi_quality,
)
from .aeraulic import exhaust_pressure, network_pressure
from .const import (
    AIRFLOW_DISPLAY_PRECISION,
    CONDUCTANCE_UNIT,
    ENERGY_MAX_GAP_SECONDS,
    SENSOR_TYPE_GLOBAL_AQI,
)
from .coordinator import Healthbox3DataUpdateCoordinator
from .entity import Healthbox3Entity


_GLOBAL_AQI_DISPLAY_PRECISION = 1


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
        aqi_value = as_float(index.value) if index is not None else None
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
        aqi_value = as_float(index.value) if index is not None else None
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
    _attr_suggested_display_precision = AIRFLOW_DISPLAY_PRECISION

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


class _Healthbox3ModelledPressureSensor(Healthbox3Entity, SensorEntity):
    """Base for the two unit-level pressures the duct model gives.

    Both need the rooms as well as the device - a total over the
    installation, not a field of one response - which is why neither is a
    DEVICE_SENSOR_META entry like the readings beside them.

    See aeraulic.py for why these are computed rather than read, and for
    the check against Renson's own figures.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.PRESSURE
    _attr_native_unit_of_measurement = UnitOfPressure.PA
    _attr_suggested_display_precision = 1
    _unique_id_suffix: str

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_{self._unique_id_suffix}"

    @property
    @override
    def available(self) -> bool:
        """Return whether the duct model can currently answer."""
        return super().available and self.native_value is not None


class Healthbox3NetworkPressureSensor(_Healthbox3ModelledPressureSensor):
    """Total pressure the fan has to provide at nominal flow, in Pa."""

    _attr_translation_key = "network_pressure"
    _unique_id_suffix = "network_pressure"

    @property
    @override
    def native_value(self) -> float | None:
        """Return the exhaust duct plus the worst branch."""
        data = self.coordinator.data
        return network_pressure(data.healthbox, data.device)


class Healthbox3ExhaustPressureSensor(_Healthbox3ModelledPressureSensor):
    """Pressure drop across the exhaust duct at nominal flow, in Pa."""

    _attr_translation_key = "exhaust_pressure"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unique_id_suffix = "exhaust_pressure"

    @property
    @override
    def native_value(self) -> float | None:
        """Return the whole installation's flow over the outlet conductance."""
        data = self.coordinator.data
        return exhaust_pressure(data.healthbox, data.device)


class Healthbox3ConnectionTypeSensor(Healthbox3Entity, SensorEntity):
    """How the device is attached to the network ("WIFI"/"ETHERNET").

    The one field of `/renson_core/v2/global` still published as an
    entity. Its neighbours up to 0.3.x - firmware version, IP address,
    MAC address - are gone: Home Assistant puts all three on the device
    entry itself (`sw_version`, `configuration_url`, `connections`, see
    entity.py's `unit_device_info`), and an entity repeating what the
    device page already shows is a row to scroll past, not information.

    This one has nowhere else to go, and it is what makes the rest of the
    network picture readable: it is the answer to "is this unit on a
    cable", which decides whether its Wi-Fi entities exist at all (see
    `wifi_reported` in coordinator.py).

    Reported in the device's own spelling - the observed values are
    uppercase, but nothing documents the full set, and a value this
    client has never seen would have no honest translation.
    """

    _attr_translation_key = "connection_type"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: Healthbox3DataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_connection_type"

    @property
    @override
    def available(self) -> bool:
        """Return whether the device currently reports this field."""
        return super().available and self.native_value is not None

    @property
    @override
    def native_value(self) -> str | None:
        """Return the reported interface type, if the device reported one."""
        info = self.coordinator.data.global_info
        return info.interface_type if info is not None else None


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
        restored = last.native_value
        # A restored state can be any of the types a sensor may store,
        # dates included - a date where a kWh total should be is as
        # unusable as an unparseable string, and both land below.
        if isinstance(restored, (int, float, str, Decimal)):
            try:
                self._total_kwh = float(restored)
                return
            except (TypeError, ValueError):
                pass
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
