"""Shared fixtures for Healthbox3 tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.healthbox3 import api as api_mod
from custom_components.healthbox3.const import DOMAIN

FIXTURES_DIR = Path(__file__).parent.parent / "docs" / "fixtures"


def _load_fixture(name: str) -> dict:
    with (FIXTURES_DIR / name).open() as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make custom_components/healthbox3 discoverable to Home Assistant."""
    yield


@pytest.fixture(autouse=True)
def mock_discover_broadcast():
    """Default broadcast discovery to "nothing found".

    Config flow tests that don't care about discovery (i.e. most of them)
    would otherwise try to open a real UDP socket the moment the user step
    runs, since it now attempts discovery unconditionally. Tests that do
    care override this mock's return_value/side_effect explicitly.
    """
    with patch(
        "custom_components.healthbox3.config_flow.async_discover_broadcast",
        AsyncMock(return_value=[]),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def mock_coordinator_discover_broadcast():
    """Same reasoning as mock_discover_broadcast, for the coordinator's own
    relocate-on-connection-error probe (a separate import into
    coordinator.py, not the same patch target as the config flow's).
    """
    with patch(
        "custom_components.healthbox3.coordinator.async_discover_broadcast",
        AsyncMock(return_value=[]),
    ) as mock:
        yield mock


@pytest.fixture
def v1_data_raw() -> dict:
    """Raw JSON from a real device's /v1/api/data/current."""
    return _load_fixture("v1-data-current.json")


@pytest.fixture
def v2_data_raw() -> dict:
    """Raw JSON from a real device's /v2/api/data/current."""
    return _load_fixture("v2-data-current.json")


@pytest.fixture
def boost_raw() -> dict:
    """Raw JSON from a real device's /v1/api/boost/1."""
    return _load_fixture("v1-boost-room1.json")


@pytest.fixture
def discovery_raw() -> dict:
    """Raw JSON from a real device's discovery response.

    The captured file has a leading "<n>\\t(ip, port)" prefix from the
    tool used to record it, so extract just the JSON object.
    """
    content = (FIXTURES_DIR / "discovery-response.json").read_text()
    start = content.index("{")
    end = content.rindex("}") + 1
    return json.loads(content[start:end])


@pytest.fixture
def v1_data(v1_data_raw) -> api_mod.HealthboxData:
    """Parsed HealthboxData from the real v1 fixture."""
    return api_mod._parse_v1_data(v1_data_raw)


@pytest.fixture
def v2_data(v2_data_raw) -> api_mod.HealthboxData:
    """Parsed HealthboxData from the real v2 fixture."""
    return api_mod._parse_v2_data(v2_data_raw)


@pytest.fixture
def boost_status(boost_raw) -> api_mod.BoostStatus:
    """Parsed BoostStatus from the real boost fixture."""
    return api_mod._parse_boost(boost_raw)


@pytest.fixture
def decision_raw() -> dict:
    """Hand-built /v1/decision JSON, generic values.

    Trimmed to just program/minimum/global_ventilation_level/silent - the
    real response also has room/breeze/profile/fire_protect/etc. keys
    that _parse_decision deliberately never reads (see DeviceDecision's
    docstring), so a fixture including them would only imply coverage
    this client doesn't actually have.
    """
    return _load_fixture("v1-decision.json")


@pytest.fixture
def device_decision(decision_raw) -> api_mod.DeviceDecision:
    """Parsed DeviceDecision from the decision fixture."""
    return api_mod._parse_decision(decision_raw)


@pytest.fixture
def breeze_raw() -> dict:
    """Hand-built /v2/decision/breeze JSON, generic values."""
    return _load_fixture("v2-decision-breeze.json")


@pytest.fixture
def breeze_settings(breeze_raw) -> api_mod.BreezeSettings:
    """Parsed BreezeSettings from the breeze fixture."""
    return api_mod._parse_breeze(breeze_raw)


@pytest.fixture
def room_decisions_raw() -> dict:
    """Hand-built /v2/decision/room JSON, generic values.

    Room "1" has CO2 static demand enabled, room "2" doesn't - covering
    both branches of the per-room gating logic. Only demand.CO2.static is
    included since _parse_room_decisions never reads anything else (see
    RoomDecision's docstring).
    """
    return _load_fixture("v2-decision-room.json")


@pytest.fixture
def room_decisions(room_decisions_raw) -> dict[int, api_mod.RoomDecision]:
    """Parsed room decisions from the room decision fixture."""
    return api_mod._parse_room_decisions(room_decisions_raw)


@pytest.fixture
def v2_decision_raw() -> dict:
    """Raw JSON from a real device's /v2/decision, commissioning answers
    redacted (they describe the dwelling, not the device).

    The whole decision tree in one response - profiles, per-room boost and
    demand, Breeze, Silent, Program, fire protection. The three hand-built
    fixtures it joins (v1-decision, v2-decision-breeze, v2-decision-room)
    are each one slice of this, from back when each was read separately.
    """
    return _load_fixture("v2-decision.json")


@pytest.fixture
def renson_core_global_raw() -> dict:
    """Raw JSON from a real device's /renson_core/v2/global, identifying
    fields redacted.
    """
    return _load_fixture("renson-core-v2-global.json")


@pytest.fixture
def firmware_version(renson_core_global_raw) -> str:
    """The firmware version string from the renson_core/v2/global fixture."""
    return renson_core_global_raw["firmware version"]


@pytest.fixture
def errors_raw() -> list[dict]:
    """Hand-built /v1/error JSON, generic values.

    Not a real capture - /v1/error has only ever been observed empty on
    real hardware, so there's no real populated response to genericize
    from. Matches errors_rest.js's confirmed real shape: two entries,
    one of each confirmed severity value.
    """
    return _load_fixture("v1-error.json")


@pytest.fixture
def device_errors(errors_raw) -> list[api_mod.DeviceError]:
    """Parsed device errors from the error fixture."""
    return api_mod._parse_errors(errors_raw)


@pytest.fixture
def v1_device_raw() -> dict:
    """Raw JSON from a real device's /v1/device."""
    return _load_fixture("v1-device.json")


@pytest.fixture
def device_telemetry(v1_device_raw) -> api_mod.DeviceTelemetry:
    """Parsed device telemetry from the /v1/device fixture."""
    return api_mod._parse_device(v1_device_raw)


@pytest.fixture
def wifi_status_raw() -> dict:
    """Raw JSON from a real device's /renson_core/v1/wifi/client/status."""
    return _load_fixture("wifi-client-status.json")


@pytest.fixture
def wifi_status(wifi_status_raw) -> api_mod.WifiStatus:
    """Parsed Wi-Fi status from the Wi-Fi fixture."""
    return api_mod._parse_wifi(wifi_status_raw)


@pytest.fixture
def mock_api_client():
    """Patch the API client used by __init__.py's async_setup_entry.

    autospec=True is required so autodetected async methods (e.g.
    async_get_api_key_status) become AsyncMocks instead of plain MagicMocks
    that return a non-awaitable value.

    async_get_decision_tree defaults to failing rather than being left
    unconfigured: an autospec'd AsyncMock's unconfigured return value is
    itself an AsyncMock, not a real DecisionTree (a genuine unittest.mock
    quirk), which the coordinator would store as real decision data -
    every entity built on it then reads attributes off a mock and reports
    one as its state, and number.py's setup loop calls `.get()` on what it
    thinks is a dict of room decisions and gets an unawaited coroutine
    back.

    Failing rather than answering None because the client either returns a
    tree or raises - there is no third answer - and a failed read is what
    a test saying nothing about the tree is really describing. Same fix
    pattern as the autouse broadcast-discovery fixtures above: default new
    async calls to a safe value so pre-existing tests are unaffected.
    """
    with patch(
        "custom_components.healthbox3.Healthbox3ApiClient",
        autospec=True,
    ) as mock_cls:
        client = mock_cls.return_value
        client.async_get_decision_tree = AsyncMock(
            side_effect=api_mod.Healthbox3ConnectionError(
                "no decision tree configured in this test"
            )
        )
        # Same autospec quirk as async_get_decision_tree above: left
        # unconfigured these return an AsyncMock, not None, which the
        # coordinator would happily store as real telemetry - every
        # /v1/device-backed entity would then read attributes off a mock
        # and report one as its state. Default them to "not reported".
        client.async_get_device = AsyncMock(return_value=None)
        client.async_get_wifi_status = AsyncMock(return_value=None)
        # Same quirk, sharpest edge yet: an unconfigured async_get_global
        # returns a mock whose .mac is itself a mock, which entity.py puts
        # straight into the device registry's `connections` - and the
        # registry is persisted as JSON, so every test that builds a device
        # fails on teardown rather than where the mock came from.
        client.async_get_global = AsyncMock(return_value=None)
        # Same quirk again, with a sharper edge: an unconfigured AsyncMock is
        # truthy but iterates empty, so the device-errors sensor's own
        # `if not errors` guard passes and `max()` then raises on the empty
        # iterable. Default to a real empty list.
        client.async_get_errors = AsyncMock(return_value=[])
        yield client


def make_config_entry(
    hass,
    *,
    serial: str,
    api_key: str | None = "goodkey",
    options: dict | None = None,
    title: str | None = None,
) -> MockConfigEntry:
    """Create and register a Healthbox3 config entry.

    `title` is given here rather than set afterwards on purpose: the entry
    has an update listener that reloads it on any change, title included,
    so renaming a live entry mid-test tears the integration down and sets
    it up again against whatever the shared client mock has been left
    configured to return.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "192.0.2.1", CONF_API_KEY: api_key},
        options=options or {},
        unique_id=serial,
        **({"title": title} if title is not None else {}),
    )
    entry.add_to_hass(hass)
    return entry


async def setup_integration(
    hass,
    mock_api_client,
    *,
    serial: str,
    api_key: str | None = "goodkey",
    api_key_valid: bool | None = None,
    healthbox_data: api_mod.HealthboxData | None = None,
    boost_status: api_mod.BoostStatus | None = None,
    decision: api_mod.DeviceDecision | None = None,
    breeze: api_mod.BreezeSettings | None = None,
    room_decisions: dict[int, api_mod.RoomDecision] | None = None,
    firmware_version: str | None = None,
    interface_type: str = "ETHERNET",
    errors: list[api_mod.DeviceError] | None = None,
    device: api_mod.DeviceTelemetry | None = None,
    wifi: api_mod.WifiStatus | None = None,
    title: str | None = None,
    entry: MockConfigEntry | None = None,
) -> MockConfigEntry:
    """Create a config entry and run async_setup_entry against a mocked client.

    __init__.py always checks /v2/api/api_key/status on startup, regardless
    of whether a key was ever stored in the config entry - activation is a
    one-time device-side change, not tied to what we remember. So
    `api_key_valid` (whether the *device* currently reports privileged
    access) is independent of `api_key` (whether *this config entry* has a
    key on file). Defaults to matching `api_key` when not given, to cover
    the common case of "the entry's own key is/isn't valid".

    `entry` takes an already-registered entry instead of making one, for
    a test that has to put something in place against it - registry
    entries, say - before setup runs.
    """
    if entry is None:
        entry = make_config_entry(hass, serial=serial, api_key=api_key, title=title)

    effective_valid = bool(api_key) if api_key_valid is None else api_key_valid
    mock_api_client.async_get_api_key_status.return_value = api_mod.ApiKeyStatus(
        state="valid" if effective_valid else "empty",
        disable_telemetry_data_allowed=effective_valid,
        local_sensor_data_allowed=effective_valid,
    )
    if healthbox_data is not None:
        mock_api_client.async_get_v1_data_current = AsyncMock(
            return_value=healthbox_data
        )
        mock_api_client.async_get_v2_data_current = AsyncMock(
            return_value=healthbox_data
        )
    if boost_status is not None:
        mock_api_client.async_get_boost = AsyncMock(return_value=boost_status)

    # With a key the coordinator makes one `/v2/decision` read carrying the
    # decision settings, Breeze, room demand *and* every room's boost. The
    # arguments above still describe those four separately, so they are
    # assembled into that single response here rather than at every call
    # site.
    #
    # Boost is read back through `async_get_boost` rather than from
    # `boost_status` directly, so a test giving a per-room side_effect gets
    # the same per-room answers whichever path the coordinator takes.
    #
    # A test that describes no decision gets a read that fails, not one
    # that answers None - the client either returns a tree or raises, and
    # "the tree could not be read" is the real situation those tests are
    # in. The coordinator then falls back to the per-room boost endpoint,
    # which is what every test written before this read existed expects.
    # The device answers the whole tree at once - there is no response
    # carrying Breeze or room demand but no decision block - so a test
    # asking for one without the other gets the fixture's decision, rather
    # than a shape the device cannot produce.
    effective_decision = decision
    if effective_decision is None and (breeze is not None or room_decisions is not None):
        effective_decision = api_mod._parse_decision(_load_fixture("v1-decision.json"))

    async def _decision_tree() -> api_mod.DecisionTree:
        if effective_decision is None:
            raise api_mod.Healthbox3ConnectionError("no decision data in this test")
        boost: dict[int, api_mod.BoostStatus] = {}
        for room in healthbox_data.rooms if healthbox_data is not None else []:
            try:
                boost[room.id] = await mock_api_client.async_get_boost(room.id)
            except api_mod.Healthbox3Error:
                pass
        return api_mod.DecisionTree(
            decision=effective_decision,
            breeze=breeze,
            room_decisions=room_decisions or {},
            boost=boost,
        )

    mock_api_client.async_get_decision_tree = AsyncMock(side_effect=_decision_tree)
    # `interface_type` decides whether this unit's Wi-Fi entities exist at
    # all, and whether its Wi-Fi endpoint is polled - see `wifi_reported`
    # in coordinator.py. The fixture device is wired, so that is the
    # default; a test about the Wi-Fi entities passes "WIFI".
    if firmware_version is not None:
        mock_api_client.async_get_global = AsyncMock(
            return_value=api_mod.GlobalInfo(
                firmware_version=firmware_version,
                mac="64:1c:10:00:00:01",
                ip="192.0.2.1",
                interface_type=interface_type,
            )
        )
    if errors is not None:
        mock_api_client.async_get_errors = AsyncMock(return_value=errors)
    if device is not None:
        mock_api_client.async_get_device = AsyncMock(return_value=device)
    if wifi is not None:
        mock_api_client.async_get_wifi_status = AsyncMock(return_value=wifi)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
