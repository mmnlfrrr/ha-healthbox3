"""DataUpdateCoordinator for the Renson Healthbox 3 integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import override

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.const import CONF_HOST, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import discovery_flow, issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    BoostStatus,
    BreezeSettings,
    DeviceDecision,
    DeviceError,
    DeviceTelemetry,
    GlobalInfo,
    Healthbox3ApiClient,
    Healthbox3AuthenticationError,
    Healthbox3ConnectionError,
    Healthbox3Error,
    Healthbox3InvalidResponseError,
    HealthboxData,
    RoomDecision,
    WifiStatus,
    async_discover_broadcast,
)
from .const import (
    BOOST_DURATION_PRESETS,
    BOOST_FALLBACK_LEVEL,
    BOOST_FALLBACK_TIMEOUT,
    BOOST_LEVEL_MAX,
    BOOST_LEVEL_MIN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    GLOBAL_INFO_REFRESH_EVERY,
    SCAN_INTERVAL_MAX,
    SCAN_INTERVAL_MIN,
)

_LOGGER = logging.getLogger(__name__)

# Confirmed real values for DeviceError.severity - anything else falls
# back to WARNING rather than raising, since a defensive default here is
# safer than crashing a coordinator update over a device sending a
# severity value nobody's seen it send before.
_ERROR_SEVERITY: dict[str, ir.IssueSeverity] = {
    "critical": ir.IssueSeverity.CRITICAL,
    "warning": ir.IssueSeverity.WARNING,
}


@dataclass
class Healthbox3Data:
    """Combined result of a coordinator refresh.

    Boost status lives on a separate per-room endpoint
    (`/v1/api/boost/{room_id}`), not on `data/current`, so it's fetched as
    a second step and merged in here. `decision` is `None` whenever it
    can't be fetched (no active API key, or the fetch itself failed) -
    entities built against it must treat that as "unavailable", not raise.
    """

    healthbox: HealthboxData
    boost: dict[int, BoostStatus] = field(default_factory=dict)
    decision: DeviceDecision | None = None
    breeze: BreezeSettings | None = None
    room_decisions: dict[int, RoomDecision] = field(default_factory=dict)
    global_info: GlobalInfo | None = None
    errors: list[DeviceError] = field(default_factory=list)
    device: DeviceTelemetry | None = None
    wifi: WifiStatus | None = None


@dataclass
class BoostParams:
    """The level/timeout to use next time a boost is (re)started.

    This is user-editable UI state (via each boost fan entity's
    percentage/preset_mode, in fan.py), not polled device data - it lives
    on the coordinator directly rather than in Healthbox3Data, which gets
    wholesale-replaced every refresh cycle.
    """

    level: float
    timeout: int


def _level_ceiling(default_level: float | None) -> float:
    """Return the highest boost level a room will be asked for.

    Renson's app stops at BOOST_LEVEL_MAX, but the device does not: a real
    unit holds `default_level: 270` for its kitchen, the French hygro B
    peak extraction rate written in at commissioning. Taking 200 as the
    limit silently rewrote that to 200 - 135 m3/h of regulatory airflow
    asked for as 100.

    So a room's ceiling is its own stored default when that is higher.
    Every room whose default sits inside the app's range keeps exactly the
    scale it had, which is why this widens rather than replaces.
    """
    if default_level is None:
        return BOOST_LEVEL_MAX
    return max(BOOST_LEVEL_MAX, default_level)


def _clamp_level(level: float | None, maximum: float = BOOST_LEVEL_MAX) -> float:
    if level is None:
        return BOOST_FALLBACK_LEVEL
    return max(BOOST_LEVEL_MIN, min(maximum, level))


def _clamp_timeout(timeout: int | None) -> int:
    """Snap a device-reported timeout to the nearest curated preset.

    The fan entity's preset_mode is one of a fixed set of duration labels
    (see BOOST_DURATION_PRESETS), so any timeout we seed from the device
    must land on one of those exact values to round-trip cleanly - even
    if a room's own default_timeout isn't an exact match.
    """
    if timeout is None:
        return BOOST_FALLBACK_TIMEOUT
    return min(BOOST_DURATION_PRESETS.values(), key=lambda seconds: abs(seconds - timeout))


type Healthbox3ConfigEntry = ConfigEntry["Healthbox3DataUpdateCoordinator"]


def scan_interval(entry: ConfigEntry) -> timedelta:
    """Return the poll interval this entry is configured for.

    Read from the entry's options rather than stored once at construction,
    so changing it in the UI takes effect on the next reload without the
    user having to remove and re-add the device. Out-of-range values are
    clamped rather than rejected: the only way to get one is an entry
    written by an older version or edited by hand, and refusing to set up
    over it would be worse than quietly bringing it back in bounds.
    """
    configured = entry.options.get(CONF_SCAN_INTERVAL)
    if configured is None:
        return DEFAULT_SCAN_INTERVAL
    seconds = max(SCAN_INTERVAL_MIN, min(SCAN_INTERVAL_MAX, int(configured)))
    return timedelta(seconds=seconds)


class Healthbox3DataUpdateCoordinator(DataUpdateCoordinator[Healthbox3Data]):
    """Coordinator that polls `data/current`, using v2 if an API key is active."""

    config_entry: Healthbox3ConfigEntry  # DataUpdateCoordinator itself types
    # this ConfigEntry | None, since a coordinator can technically exist
    # without one - ours is always constructed with a real entry (see
    # __init__ below), so this narrows the type to match, the same idiom
    # used throughout HA core integrations for the same situation.

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: Healthbox3ConfigEntry,
        client: Healthbox3ApiClient,
        *,
        use_v2: bool,
        update_interval: timedelta | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=update_interval or scan_interval(config_entry),
        )
        self.client = client
        self.use_v2 = use_v2
        self.boost_params: dict[int, BoostParams] = {}
        # The top of each room's own boost scale - see _level_ceiling. Keyed
        # by room id and filled from every poll's boost read; `level_max`
        # answers for a room not in it yet.
        self.boost_level_max: dict[int, float] = {}
        self.boost_all_params = BoostParams(
            level=BOOST_FALLBACK_LEVEL, timeout=BOOST_FALLBACK_TIMEOUT
        )
        self._relocate_attempted = False
        self._tracked_error_issue_ids: set[str] = set()
        # Last good `/renson_core/v2/global` read, and how many polls ago
        # it was taken - see _async_get_global_data.
        self._global_info: GlobalInfo | None = None
        self._global_info_age = 0
        # The registry id of the unit's own device entry, which each room
        # device points at to nest under it. Filled in by async_setup_entry
        # before any platform is forwarded - see entity.py's _room_device
        # for why it cannot be worked out from the room's side.
        self.unit_device_id: str | None = None

    def level_max(self, room_id: int | None) -> float:
        """Return the top of a room's boost scale - see `_level_ceiling`.

        `None` is the all-rooms fan, which sends one level to every room at
        once: it keeps the app's own range, since a level above it is only
        known to be accepted by the one room that stores it as its default.
        A room not polled yet gets the same answer, for the same reason -
        nothing yet says it takes more.
        """
        if room_id is None:
            return BOOST_LEVEL_MAX
        return self.boost_level_max.get(room_id, BOOST_LEVEL_MAX)

    @override
    async def _async_update_data(self) -> Healthbox3Data:
        """Fetch one full picture of the device.

        `data/current` goes first and alone: it is the call that decides
        whether this poll is a v2 or a v1 one, whether the key is still
        good, and which rooms exist - everything below depends on one of
        those answers, and it is the only call whose failure fails the
        whole update.

        The rest read different endpoints and none depends on another, so
        they go out together instead of one after the next. On a
        seven-room installation that is 14 requests; sequentially, at up
        to the 10s per-request timeout, a slow poll could outlast the
        interval that scheduled it. The client bounds how many are
        actually in flight (see MAX_CONCURRENT_REQUESTS), so this
        overlaps the round-trips without dumping the lot on the device.

        A TaskGroup rather than `asyncio.gather`: gather collapses eight
        differently-typed results into one union that then has to be
        picked apart by hand, while a task keeps its own type. Failures
        are not swallowed either - each helper already absorbs its own
        Healthbox3Error and degrades to None/{}/[], so anything still
        propagating here is a real programming error and should surface
        as one rather than quietly becoming a missing reading.
        """
        healthbox = await self._async_get_healthbox_data()

        async with asyncio.TaskGroup() as group:
            boost = group.create_task(self._async_get_boost_data(healthbox))
            decision = group.create_task(self._async_get_decision_data())
            breeze = group.create_task(self._async_get_breeze_data())
            room_decisions = group.create_task(self._async_get_room_decisions_data())
            global_info = group.create_task(self._async_get_global_data())
            errors = group.create_task(self._async_get_errors_data())
            device = group.create_task(self._async_get_device_data())
            wifi = group.create_task(self._async_get_wifi_data())

        self._async_reconcile_error_issues(errors.result())
        return Healthbox3Data(
            healthbox=healthbox,
            boost=boost.result(),
            decision=decision.result(),
            breeze=breeze.result(),
            room_decisions=room_decisions.result(),
            global_info=global_info.result(),
            errors=errors.result(),
            device=device.result(),
            wifi=wifi.result(),
        )

    async def _async_get_decision_data(self) -> DeviceDecision | None:
        """Fetch `/v1/decision`, tolerating failure the same way boost does.

        By this point `data/current` already succeeded, so the device is
        known reachable; a failure here just means the entities built on
        it go unavailable, not a full update failure. Also never attempted
        without an active API key - see const.py's API_V1_DECISION comment
        on why that's a deliberately conservative, not yet proven,
        assumption.
        """
        if not self.use_v2:
            return None
        try:
            return await self.client.async_get_decision()
        except Healthbox3Error as err:
            _LOGGER.debug("Failed to fetch decision data: %s", err)
            return None

    async def _async_get_breeze_data(self) -> BreezeSettings | None:
        """Fetch `/v2/decision/breeze` - same gating/tolerance as decision."""
        if not self.use_v2:
            return None
        try:
            return await self.client.async_get_breeze()
        except Healthbox3Error as err:
            _LOGGER.debug("Failed to fetch breeze data: %s", err)
            return None

    async def _async_get_room_decisions_data(self) -> dict[int, RoomDecision]:
        """Fetch `/v2/decision/room` - same gating/tolerance as decision and
        breeze, but returns `{}` (not `None`) on failure/v1-only, since
        callers key into it per room id the same way boost does.
        """
        if not self.use_v2:
            return {}
        try:
            return await self.client.async_get_room_decisions()
        except Healthbox3Error as err:
            _LOGGER.debug("Failed to fetch room decision data: %s", err)
            return {}

    async def _async_get_global_data(self) -> GlobalInfo | None:
        """Fetch `/renson_core/v2/global` - same gating/tolerance as
        decision/breeze/room_decisions, but not on every poll.

        Firmware version, MAC, IP and serial do not change between two
        polls; they change on a firmware update or a network move. So a
        good answer is reused for GLOBAL_INFO_REFRESH_EVERY polls (ten
        minutes at the default interval) rather than re-asked every time.

        Only *successful* reads are reused. A failure still drops to None
        exactly as before - the entities built on it go unavailable and
        the next poll asks again - rather than papering over an
        unreachable endpoint with a stale reading.
        """
        if not self.use_v2:
            return None

        if self._global_info is not None:
            self._global_info_age += 1
            if self._global_info_age < GLOBAL_INFO_REFRESH_EVERY:
                return self._global_info

        try:
            info = await self.client.async_get_global()
        except Healthbox3Error as err:
            _LOGGER.debug("Failed to fetch global device info: %s", err)
            self._global_info = None
            self._global_info_age = 0
            return None

        self._global_info = info
        self._global_info_age = 0
        return info

    async def _async_get_errors_data(self) -> list[DeviceError]:
        """Fetch `/v1/error` - same gating/tolerance as room_decisions (a
        list, so `[]` not `None` on failure/v1-only).
        """
        if not self.use_v2:
            return []
        try:
            return await self.client.async_get_errors()
        except Healthbox3Error as err:
            _LOGGER.debug("Failed to fetch device errors: %s", err)
            return []

    async def _async_get_device_data(self) -> DeviceTelemetry | None:
        """Fetch `/v1/device` - same gating/tolerance as decision.

        Gated on `use_v2` for consistency with every other
        reverse-engineered endpoint here (see const.py's API_V1_DECISION
        comment): it has only ever been probed against a device with an
        active API key, so it isn't assumed key-independent just because
        it's "v1"-prefixed.
        """
        if not self.use_v2:
            return None
        try:
            return await self.client.async_get_device()
        except Healthbox3Error as err:
            _LOGGER.debug("Failed to fetch device telemetry: %s", err)
            return None

    async def _async_get_wifi_data(self) -> WifiStatus | None:
        """Fetch `/renson_core/v1/wifi/client/status` - same gating/tolerance
        as device telemetry above.
        """
        if not self.use_v2:
            return None
        try:
            return await self.client.async_get_wifi_status()
        except Healthbox3Error as err:
            _LOGGER.debug("Failed to fetch Wi-Fi status: %s", err)
            return None

    def _async_reconcile_error_issues(self, errors: list[DeviceError]) -> None:
        """Create a repair issue for each currently-reported device error,
        and delete any previously-tracked issue whose error is no longer
        present - self-healing, no user action required to dismiss one
        that's resolved itself.

        Deliberately `is_fixable=False`: DELETE /v1/error/clear (see
        API_V1_ERROR's comment in const.py) is a bulk "clear everything"
        action with no per-error variant, so wiring a "Fix" button to it
        would risk dismissing unrelated errors alongside the one the user
        meant to act on. Clearing stays a manual device/Renson-app
        action; these issues exist purely to inform.

        Keyed on `association_id` - its name strongly implies it's the
        device's own per-fault correlation id, but this has never been
        confirmed against a real populated response (see DeviceError's
        docstring - /v1/error has only ever been observed empty). If that
        assumption turns out wrong (e.g. two distinct faults somehow
        share one association_id), the practical effect is at worst
        under-reporting - one visible issue instead of two - not
        anything dangerous.
        """
        current_issue_ids: set[str] = set()
        for error in errors:
            issue_id = f"device_error_{error.association_id}"
            current_issue_ids.add(issue_id)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=_ERROR_SEVERITY.get(error.severity, ir.IssueSeverity.WARNING),
                translation_key="device_error",
                translation_placeholders={
                    "code": error.code,
                    "description": error.description,
                    "time": error.time,
                    "severity": error.severity,
                    "category": error.category,
                },
            )

        for stale_issue_id in self._tracked_error_issue_ids - current_issue_ids:
            ir.async_delete_issue(self.hass, DOMAIN, stale_issue_id)

        self._tracked_error_issue_ids = current_issue_ids

    async def _async_try_relocate(self) -> None:
        """Best-effort: if this entry's device is answering at a new IP
        (e.g. a DHCP lease renewal), find it via broadcast discovery and
        trigger a silent, invisible-to-the-user reconnect.

        Only attempted once per outage, not on every failed poll (every
        DEFAULT_SCAN_INTERVAL indefinitely) - the device may simply be
        offline for an unrelated reason, and there's no reason to keep
        broadcasting while that sorts itself out. Reset on the next
        successful poll.

        Triggers a real config flow (source integration_discovery) rather
        than updating the entry directly, so the same identity
        verification (a real HTTP call, not just trusting the broadcast
        reply) used everywhere else in this integration also gates this -
        see Healthbox3ConfigFlow.async_step_integration_discovery.
        """
        if self._relocate_attempted:
            return
        self._relocate_attempted = True

        try:
            devices = await async_discover_broadcast()
        except OSError:
            _LOGGER.debug("Relocate broadcast discovery failed", exc_info=True)
            return

        match = next(
            (d for d in devices if d.serial == self.config_entry.unique_id), None
        )
        if match is None or match.ip == self.config_entry.data[CONF_HOST]:
            return

        _LOGGER.info(
            "Healthbox 3 %s found at new address %s (was %s); reconnecting",
            self.config_entry.unique_id,
            match.ip,
            self.config_entry.data[CONF_HOST],
        )
        discovery_flow.async_create_flow(
            self.hass,
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data={CONF_HOST: match.ip},
        )

    async def _async_get_healthbox_data(self) -> HealthboxData:
        if not self.use_v2:
            return await self._async_get_v1_data()

        key_invalid = False
        try:
            data = await self.client.async_get_v2_data_current()
        except Healthbox3AuthenticationError:
            key_invalid = True
        except Healthbox3ConnectionError as err:
            await self._async_try_relocate()
            raise UpdateFailed(f"Error communicating with Healthbox 3: {err}") from err
        except Healthbox3InvalidResponseError as err:
            if not await self._async_api_key_still_valid():
                key_invalid = True
            else:
                raise UpdateFailed(
                    f"Error communicating with Healthbox 3: {err}"
                ) from err
        else:
            self._relocate_attempted = False
            return data

        assert key_invalid  # every branch above either returns or sets this
        self._async_handle_key_invalid()
        return await self._async_get_v1_data()

    async def _async_get_v1_data(self) -> HealthboxData:
        try:
            data = await self.client.async_get_v1_data_current()
        except Healthbox3ConnectionError as err:
            await self._async_try_relocate()
            raise UpdateFailed(f"Error communicating with Healthbox 3: {err}") from err
        except Healthbox3InvalidResponseError as err:
            raise UpdateFailed(f"Error communicating with Healthbox 3: {err}") from err
        self._relocate_attempted = False
        return data

    async def _async_api_key_still_valid(self) -> bool:
        """Disambiguate a v2 data/current parse failure from a revoked key.

        Neither Renson PDF documents what `data/current` actually returns
        once a previously-active key stops working, so on a v2 parse
        failure we fall back to the one endpoint with an unambiguous
        contract (`/v2/api/api_key/status`) to decide whether this is an
        auth problem or a transient/format problem.
        """
        try:
            status = await self.client.async_get_api_key_status()
        except Healthbox3Error:
            return True
        # "validating" means the device is re-checking the key with Renson,
        # which it also does by itself after a reboot. Treated like the
        # error case above - undecided, so assume the key is fine and let a
        # later poll settle it, rather than starting a reauth flow over a
        # state that clears on its own.
        return status.is_valid or status.is_pending

    def _async_handle_key_invalid(self) -> None:
        """Fall back to v1-only and request reauth, without failing this update.

        Renson-issued API keys carry a multi-year expiry that the device
        itself has no local awareness of (confirmed: `/v2/api/api_key/status`
        exposes no expiry field), so there's no way to warn ahead of time.
        When the key does eventually stop working - from expiry or
        revocation - v1 still works without one, so this degrades to
        v1-only functionality (same as if no key were ever configured)
        instead of failing every poll and taking every entity unavailable
        until the user reauthenticates.
        """
        _LOGGER.warning(
            "Healthbox 3 API key is no longer valid; falling back to v1-only "
            "functionality until reauthentication"
        )
        self.use_v2 = False
        self.config_entry.async_start_reauth(self.hass)

    async def _async_get_boost_data(
        self, healthbox: HealthboxData
    ) -> dict[int, BoostStatus]:
        """Fetch boost status for every room.

        By this point `data/current` already succeeded, so the device is
        known reachable; a failure fetching one room's boost status is
        treated as that room's boost entity going unavailable, not as a
        full update failure.
        """
        results = await asyncio.gather(
            *(self.client.async_get_boost(room.id) for room in healthbox.rooms),
            return_exceptions=True,
        )
        boost: dict[int, BoostStatus] = {}
        for room, result in zip(healthbox.rooms, results, strict=True):
            if isinstance(result, Healthbox3Error):
                _LOGGER.debug(
                    "Failed to fetch boost status for room %s: %s", room.id, result
                )
                continue
            if isinstance(result, BaseException):
                raise result
            boost[room.id] = result
            # The room's ceiling is refreshed every poll, unlike the staged
            # level below: it describes the device's own configuration, so
            # a room reconfigured at the unit must not keep answering to
            # the range it had when Home Assistant first saw it.
            self.boost_level_max[room.id] = _level_ceiling(result.default_level)
            if room.id not in self.boost_params:
                # Seed once from the room's own device-reported defaults;
                # never overwritten afterwards so a user's own choice (or a
                # restored one) sticks across refreshes.
                self.boost_params[room.id] = BoostParams(
                    level=_clamp_level(
                        result.default_level, self.boost_level_max[room.id]
                    ),
                    timeout=_clamp_timeout(result.default_timeout),
                )
        return boost
