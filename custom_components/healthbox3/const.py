"""Constants for the Renson Healthbox 3 integration."""

from datetime import timedelta

DOMAIN = "healthbox3"

DEFAULT_SCAN_INTERVAL = timedelta(seconds=30)

# Discovery (UDP broadcast/unicast on port 49152), documented in the
# 3rd-party API PDF. Used by the config flow's automatic broadcast
# discovery and the coordinator's relocate-on-connection-error probe.
DISCOVERY_PORT = 49152
DISCOVERY_MESSAGE = b"RENSON_DEVICE/JSON?"
DISCOVERY_TIMEOUT = 5

# v1 API - no authentication, always available.
API_V1_DATA_CURRENT = "/v1/api/data/current"
API_V1_BOOST = "/v1/api/boost/{room_id}"

# v2 API - requires an activated API key.
API_V2_DATA_CURRENT = "/v2/api/data/current"
API_V2_API_KEY = "/v2/api/api_key"
API_V2_API_KEY_STATUS = "/v2/api/api_key/status"
API_V2_PROFILE_NAME = "/v2/api/data/current/room/{room_id}/profile_name"

# Undocumented in either Renson PDF - reverse-engineered from the device's
# own firmware web UI JavaScript (kept locally, gitignored, see CLAUDE.md).
# Confirmed reachable with no auth header at all when probed directly, but
# every probe so far has been against a device with an API key already
# active - unlike the two lines above, there's no independent evidence
# these work *without* one, so entities built against them are gated on
# coordinator.use_v2 the same as profile_name, not assumed key-independent
# just because they happen to be "v1"-prefixed.
API_V1_DECISION = "/v1/decision"

# Device-wide minimum ventilation level, as a percentage. The installer
# web UI's own field has no min/max validation in its JS - these bounds
# are the range the Renson mobile app itself offers, not independently
# confirmed against the local API.
GLOBAL_MINIMUM_VENTILATION_MIN = 10.0
GLOBAL_MINIMUM_VENTILATION_MAX = 30.0

# Same undocumented/reverse-engineered status as API_V1_DECISION above.
API_V2_DECISION_BREEZE = "/v2/decision/breeze"

# Confirmed from the device's own web UI's hardcoded dropdown options
# (15-35 in steps of 1) - unlike the global minimum bounds above, these
# are a real constraint the local API's own client enforces, not just
# what the mobile app happens to offer.
BREEZE_TEMP_MIN = 15.0
BREEZE_TEMP_MAX = 35.0

# Same undocumented/reverse-engineered status as API_V1_DECISION above.
# Confirmed to use the exact same room id numbering as data/current (cross-
# checked via each room's `nominal` m3/h value, 100% match across all 7
# rooms on real hardware).
API_V2_DECISION_ROOM = "/v2/decision/room"

# CO2 static demand threshold, in ppm - confirmed from the device's own web
# UI's hardcoded dropdown options.
CO2_THRESHOLD_MIN = 650.0
CO2_THRESHOLD_MAX = 2000.0
CO2_THRESHOLD_STEP = 50.0

# Silent (reduced-noise schedule) reduction, as a percentage. The Renson
# mobile app displays this with a negative sign as a cosmetic convention
# only (e.g. "-10%") - the wire value, and this bound, are the positive
# magnitude.
SILENT_REDUCTION_MIN = 5.0
SILENT_REDUCTION_MAX = 25.0

# /v1/decision's `silent` block has one schedule array per weekday; this
# client only supports a single shared start/stop pair applied uniformly
# to every day (matching how the Renson app itself presents it), so a
# write touches all seven.
SILENT_WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

# Same undocumented/reverse-engineered status as API_V1_DECISION above -
# a different namespace (/renson_core/v2/...) from the rest of the v1/v2
# endpoints, but every probe so far has still been against a device with
# an active API key, so gated the same way rather than assumed
# key-independent.
API_RENSON_CORE_V2_GLOBAL = "/renson_core/v2/global"

# Same undocumented/reverse-engineered status as API_V1_DECISION above.
# Confirmed real shape from errors_rest.js ([{code, time, description,
# association_id, severity}]) but never actually seen populated on real
# hardware - only the empty-array case has been observed. The device
# also exposes DELETE /v1/error/clear, deliberately not implemented here
# at all: it's a bulk "clear everything" action with no per-error
# variant, which doesn't map cleanly onto Home Assistant's naturally
# per-issue repair model - clearing stays a manual device/Renson-app
# action, see coordinator.py's issue-reconciliation docstring.
API_V1_ERROR = "/v1/error"

# Same undocumented/reverse-engineered status as API_V1_DECISION above.
# Carries the device's live electrical and aeraulic telemetry: fan
# voltage/pressure/flow/power/rpm, a whole-device power figure, and the
# calibrated duct model the device's own solver maintains (per-valve
# conductances and pressures). Confirmed shape against real hardware, see
# docs/fixtures/v1-device.json.
API_V1_DEVICE = "/v1/device"

# Same undocumented/reverse-engineered status as API_V1_DECISION above.
# Confirmed shape from docs/fixtures/wifi-client-status.json.
API_RENSON_CORE_V1_WIFI_STATUS = "/renson_core/v1/wifi/client/status"

# /v1/device keys its per-valve conductance and pressure blocks by the
# collector PORT number, not by room id - this room parameter is the join
# key between the two endpoints. Reported as a string on the wire (e.g.
# "1"), hence the int() coercion at the parse site.
ROOM_PARAM_VALVE = "valve"

# Both blocks nest their actual value one level deeper under a "0" key
# (conductance.c_collector.<port>.c_ij.0, cmode_pressures.p_collector.
# <port>.0). Confirmed on real hardware for all 7 ports; no port has ever
# been observed with a key other than "0", but nothing documents what a
# second entry would mean, so only "0" is read.
COLLECTOR_PRIMARY_KEY = "0"

# Conductance C as used by the device's own solver in Q = C x sqrt(dP),
# with Q in m3/h and dP in Pa. Home Assistant has no unit for this, and no
# SensorDeviceClass fits, so it's a display-only label on a
# device_class-less sensor rather than anything HA will try to convert.
CONDUCTANCE_UNIT = "m³/(h·√Pa)"

# Longest gap between two power readings that still gets integrated into the
# energy total. The coordinator polls every DEFAULT_SCAN_INTERVAL, so a normal
# gap is seconds and a restart is well under this; anything longer means the
# unit was unreachable, and there is no way to tell "Home Assistant was down
# while the fan kept running" from "the unit was off". Skipping under-reports
# rather than inventing energy that may never have been used.
ENERGY_MAX_GAP_SECONDS = 15 * 60

API_KEY_STATE_VALID = "valid"
API_KEY_STATE_EMPTY = "empty"

# Third state of /v2/api/api_key/status, and the one that makes activation
# asynchronous: POSTing a key does NOT decide anything by itself. The device
# has to reach Renson's servers to check the key against its own serial, and
# reports "validating" until that round-trip finishes. A correct key therefore
# reads as "not valid" for the first few seconds after it is submitted, which
# is exactly the window a config flow submits in - so "validating" must be
# treated as "not decided yet", never as a rejection.
API_KEY_STATE_VALIDATING = "validating"

# How long to keep asking the device before giving up on a pending validation.
# Counted in attempts rather than wall-clock so the wait is deterministic;
# 15 x 2s is ~30s, past which the config flow reports "still validating" (a
# distinct message from "rejected") instead of blocking the UI any longer.
API_KEY_ACTIVATION_ATTEMPTS = 15
API_KEY_ACTIVATION_POLL_SECONDS = 2.0

PROFILE_ECO = "eco"
PROFILE_HEALTH = "health"
PROFILE_INTENSE = "intense"
PROFILES = [PROFILE_ECO, PROFILE_HEALTH, PROFILE_INTENSE]

# Sensor "type" values as they appear on the wire (confirmed against
# docs/fixtures/v2-data-current.json). These are used as parser keys, not
# translation keys.
SENSOR_TYPE_TEMPERATURE = "indoor temperature"
SENSOR_TYPE_HUMIDITY = "indoor relative humidity"
SENSOR_TYPE_CO2 = "indoor CO2"
SENSOR_TYPE_VOC = "indoor volatile organic compounds"
SENSOR_TYPE_AQI = "indoor air quality index"
SENSOR_TYPE_GLOBAL_AQI = "global air quality index"

# Boost level is a percentage of a room's nominal flow rate; 10-200% is the
# range offered by Renson's own app. The fan entity's percentage (0-100,
# a hard requirement of HA's fan platform) is rescaled onto this range -
# see fan.py's _level_to_percentage/_percentage_to_level.
BOOST_LEVEL_MIN = 10.0
BOOST_LEVEL_MAX = 200.0

# Boost duration is seconds on the wire; the fan entity exposes it as a
# curated preset_mode list rather than exact-minute granularity (a fixed
# list, not arbitrary custom durations - accepted tradeoff for simplicity).
# Order matters: it defines the preset_mode option order shown in the UI.
# 5 min is the floor to match Renson's own app, which doesn't offer
# anything shorter either.
BOOST_DURATION_PRESETS: dict[str, int] = {
    "5 min": 300,
    "10 min": 600,
    "15 min": 900,
    "30 min": 1800,
    "45 min": 2700,
    "1 hour": 3600,
    "2 hours": 7200,
    "4 hours": 14400,
}

# Fallback level/timeout when a room's own default_level/default_timeout is
# missing or out of range - matches the factory default seen on real
# hardware (docs/fixtures/v1-boost-room1.json); 900s is also the "15 min"
# preset above.
BOOST_FALLBACK_LEVEL = 100.0
BOOST_FALLBACK_TIMEOUT = 900
