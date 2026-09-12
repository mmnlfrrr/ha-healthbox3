"""Tests for the Healthbox3 DataUpdateCoordinator."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.helpers import issue_registry as ir

from custom_components.healthbox3 import api as api_mod
from custom_components.healthbox3.const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    GLOBAL_INFO_REFRESH_EVERY,
    SCAN_INTERVAL_MAX,
    SCAN_INTERVAL_MIN,
)
from custom_components.healthbox3.coordinator import (
    Healthbox3DataUpdateCoordinator,
    scan_interval,
)

from .conftest import make_config_entry


def _client() -> AsyncMock:
    """Return a client mock whose decision-tree read fails by default.

    `AsyncMock(spec=...)` answers every call with another mock, and the
    coordinator would store that mock as real decision data - boost
    included, since the tree carries it. A failed read is the honest
    stand-in for a test that says nothing about the tree: it is what an
    older or keyless device produces, and it leaves boost coming from the
    per-room endpoint these tests were written against. Tests that care
    about the tree set it explicitly.
    """
    client = AsyncMock(spec=api_mod.Healthbox3ApiClient)
    client.async_get_decision_tree.side_effect = api_mod.Healthbox3ConnectionError(
        "no decision tree configured in this test"
    )
    return client


def _discovery_info(*, ip: str, serial: str) -> api_mod.DiscoveryInfo:
    return api_mod.DiscoveryInfo(
        device="HEALTHBOX3",
        firmware_version="2.6.9",
        ip=ip,
        mac="00:11:22:33:44:55",
        serial=serial,
        warranty_number="warranty-123",
        scope="HEALTHBOX3",
        description="Healthbox 3.0",
    )


def _patch_discover_broadcast(return_value=None, side_effect=None):
    return patch(
        "custom_components.healthbox3.coordinator.async_discover_broadcast",
        AsyncMock(return_value=return_value, side_effect=side_effect),
    )


def _patch_create_flow():
    return patch("custom_components.healthbox3.coordinator.discovery_flow.async_create_flow")


async def test_v1_only_polling_skips_v2(hass, v1_data, boost_status):
    entry = make_config_entry(hass, serial=v1_data.serial)
    client = _client()
    client.async_get_v1_data_current.return_value = v1_data
    client.async_get_boost.return_value = boost_status

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=False)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    client.async_get_v2_data_current.assert_not_called()
    assert len(coordinator.data.healthbox.rooms) == 7
    assert len(coordinator.data.boost) == 7


async def test_v1_only_polling_never_reads_the_decision_tree(
    hass, v1_data, boost_status
):
    """No independent evidence `/v2/decision` answers without an active
    API key - never even attempted in v1-only mode, matching const.py's
    API_V2_DECISION comment.

    Boost still arrives, from the per-room endpoint that is the one thing
    confirmed to work without a key.
    """
    entry = make_config_entry(hass, serial=v1_data.serial)
    client = _client()
    client.async_get_v1_data_current.return_value = v1_data
    client.async_get_boost.return_value = boost_status

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=False)
    await coordinator.async_refresh()

    client.async_get_decision_tree.assert_not_called()
    assert coordinator.data.decision is None
    assert coordinator.data.breeze is None
    assert coordinator.data.room_decisions == {}
    assert len(coordinator.data.boost) == 7


async def test_one_read_answers_decision_breeze_room_demand_and_boost(
    hass, v2_data, boost_status, device_decision, breeze_settings, room_decisions
):
    """The point of the merged read: `/v2/decision` carries all four, so
    the per-room boost endpoint is not called at all.

    That is six requests answered by one on a three-room unit, ten on a
    seven-room one.
    """
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.return_value = v2_data
    client.async_get_decision_tree.side_effect = None
    client.async_get_decision_tree.return_value = api_mod.DecisionTree(
        decision=device_decision,
        breeze=breeze_settings,
        room_decisions=room_decisions,
        boost={room.id: boost_status for room in v2_data.rooms},
    )

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    assert coordinator.data.decision == device_decision
    assert coordinator.data.breeze == breeze_settings
    assert coordinator.data.room_decisions == room_decisions
    assert len(coordinator.data.boost) == 7
    client.async_get_boost.assert_not_called()
    assert client.async_get_decision_tree.await_count == 1


async def test_a_failed_tree_read_does_not_fail_the_whole_update(
    hass, v2_data, boost_status
):
    """A failed read means the entities built on what is missing go
    unavailable, not that the whole coordinator update fails - matching
    how a single room's boost failure is tolerated.

    Boost is the exception: it falls back to the per-room endpoint. It is
    the one control that works without a key, and it should not disappear
    because a v2 read that has nothing to do with it went wrong.
    """
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.return_value = v2_data
    client.async_get_boost.return_value = boost_status

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data.decision is None
    assert coordinator.data.breeze is None
    assert coordinator.data.room_decisions == {}
    assert len(coordinator.data.boost) == 7


async def test_the_tree_seeds_each_rooms_boost_scale(
    hass, v2_data, device_decision, boost_status
):
    """The boost ceiling and the staged level are recorded from whichever
    path the status arrived by, since what they describe is the room, not
    the endpoint it was read from.
    """
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.return_value = v2_data
    client.async_get_decision_tree.side_effect = None
    client.async_get_decision_tree.return_value = api_mod.DecisionTree(
        decision=device_decision,
        boost={
            1: api_mod.BoostStatus(
                enable=False,
                level=100.0,
                timeout=900,
                remaining=0,
                default_level=270.0,
                default_timeout=1800,
            )
        },
    )

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    assert coordinator.level_max(1) == 270.0
    assert coordinator.boost_params[1].level == 270.0


async def test_v1_only_polling_never_fetches_firmware_version(hass, v1_data, boost_status):
    entry = make_config_entry(hass, serial=v1_data.serial)
    client = _client()
    client.async_get_v1_data_current.return_value = v1_data
    client.async_get_boost.return_value = boost_status

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=False)
    await coordinator.async_refresh()

    client.async_get_global.assert_not_called()
    assert coordinator.data.global_info is None


async def test_v2_polling_fetches_firmware_version(
    hass, v2_data, boost_status, firmware_version
):
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.return_value = v2_data
    client.async_get_boost.return_value = boost_status
    client.async_get_global.return_value = api_mod.GlobalInfo(
        firmware_version=firmware_version
    )

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    assert coordinator.data.global_info.firmware_version == firmware_version


async def test_firmware_version_fetch_failure_does_not_fail_whole_update(
    hass, v2_data, boost_status
):
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.return_value = v2_data
    client.async_get_boost.return_value = boost_status
    client.async_get_global.side_effect = api_mod.Healthbox3ConnectionError(
        "offline"
    )

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data.global_info is None


async def test_v1_only_polling_never_fetches_errors(hass, v1_data, boost_status):
    entry = make_config_entry(hass, serial=v1_data.serial)
    client = _client()
    client.async_get_v1_data_current.return_value = v1_data
    client.async_get_boost.return_value = boost_status

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=False)
    await coordinator.async_refresh()

    client.async_get_errors.assert_not_called()
    assert coordinator.data.errors == []


async def test_v2_polling_fetches_errors(hass, v2_data, boost_status, device_errors):
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.return_value = v2_data
    client.async_get_boost.return_value = boost_status
    client.async_get_errors.return_value = device_errors

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    assert coordinator.data.errors == device_errors


async def test_errors_fetch_failure_does_not_fail_whole_update(hass, v2_data, boost_status):
    """Same tolerance as decision/breeze/room_decisions/firmware_version -
    the fallback is `[]`, matching room_decisions' list/dict-tolerant
    precedent since an error count of 0 is always a valid state.
    """
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.return_value = v2_data
    client.async_get_boost.return_value = boost_status
    client.async_get_errors.side_effect = api_mod.Healthbox3ConnectionError("offline")

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data.errors == []


async def test_device_error_creates_repair_issue(hass, v2_data, boost_status, device_errors):
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.return_value = v2_data
    client.async_get_boost.return_value = boost_status
    client.async_get_errors.return_value = device_errors

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    registry = ir.async_get(hass)
    critical = registry.async_get_issue(DOMAIN, "device_error_abc123")
    warning = registry.async_get_issue(DOMAIN, "device_error_def456")

    assert critical is not None
    assert critical.severity is ir.IssueSeverity.CRITICAL
    assert critical.is_fixable is False
    assert critical.translation_key == "device_error"
    assert critical.translation_placeholders == {
        "code": "E042",
        "description": "Sensor fault in room 3",
        "time": "2026-01-15T08:30:00Z",
        "severity": "critical",
        "category": "Unknown",
    }
    assert warning is not None
    assert warning.severity is ir.IssueSeverity.WARNING


async def test_device_error_unknown_severity_falls_back_to_warning(hass, v2_data, boost_status):
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.return_value = v2_data
    client.async_get_boost.return_value = boost_status
    client.async_get_errors.return_value = [
        api_mod.DeviceError(
            code="X1",
            time="2026-01-15T08:30:00Z",
            description="Unknown severity",
            association_id="zzz",
            severity="something-new",
            category="Unknown",
        )
    ]

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "device_error_zzz")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING


async def test_device_error_issue_deleted_when_error_disappears(
    hass, v2_data, boost_status, device_errors
):
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.return_value = v2_data
    client.async_get_boost.return_value = boost_status
    client.async_get_errors.return_value = device_errors

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "device_error_abc123") is not None
    assert registry.async_get_issue(DOMAIN, "device_error_def456") is not None

    client.async_get_errors.return_value = [device_errors[0]]
    await coordinator.async_refresh()

    assert registry.async_get_issue(DOMAIN, "device_error_abc123") is not None
    assert registry.async_get_issue(DOMAIN, "device_error_def456") is None


async def test_no_device_errors_creates_no_repair_issues(hass, v2_data, boost_status):
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.return_value = v2_data
    client.async_get_boost.return_value = boost_status
    client.async_get_errors.return_value = []

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    assert ir.async_get(hass).issues == {}


async def test_v2_polling_merges_boost_status(hass, v2_data, boost_status):
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.return_value = v2_data
    client.async_get_boost.return_value = boost_status

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    room1 = next(r for r in coordinator.data.healthbox.rooms if r.id == 1)
    assert room1.profile_name == "health"
    assert coordinator.data.boost[1].default_level == 100.0


async def test_connection_error_marks_update_failed(hass, v1_data):
    entry = make_config_entry(hass, serial=v1_data.serial)
    client = _client()
    client.async_get_v1_data_current.side_effect = api_mod.Healthbox3ConnectionError(
        "offline"
    )

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=False)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False


async def test_relocate_triggered_when_broadcast_finds_device_at_new_ip(hass, v1_data):
    """A connection error, plus a broadcast reply with this entry's own
    serial at a different IP than currently stored, must trigger a
    relocate flow with the new host.
    """
    entry = make_config_entry(hass, serial=v1_data.serial)
    client = _client()
    client.async_get_v1_data_current.side_effect = api_mod.Healthbox3ConnectionError(
        "offline"
    )

    with (
        _patch_discover_broadcast(
            return_value=[_discovery_info(ip="192.0.2.99", serial=v1_data.serial)]
        ),
        _patch_create_flow() as mock_create_flow,
    ):
        coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=False)
        await coordinator.async_refresh()

    mock_create_flow.assert_called_once()
    _hass, domain = mock_create_flow.call_args.args
    assert domain == DOMAIN
    assert mock_create_flow.call_args.kwargs["context"] == {
        "source": "integration_discovery"
    }
    assert mock_create_flow.call_args.kwargs["data"] == {"host": "192.0.2.99"}


async def test_relocate_not_triggered_when_broadcast_finds_same_ip(hass, v1_data):
    """The device answering at the same IP it's already configured with
    is not a relocation - nothing to do.
    """
    entry = make_config_entry(hass, serial=v1_data.serial)
    client = _client()
    client.async_get_v1_data_current.side_effect = api_mod.Healthbox3ConnectionError(
        "offline"
    )

    with (
        _patch_discover_broadcast(
            return_value=[_discovery_info(ip="192.0.2.1", serial=v1_data.serial)]
        ),
        _patch_create_flow() as mock_create_flow,
    ):
        coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=False)
        await coordinator.async_refresh()

    mock_create_flow.assert_not_called()


async def test_relocate_not_triggered_when_no_serial_match(hass, v1_data):
    """A broadcast reply from an unrelated device must not trigger a relocate."""
    entry = make_config_entry(hass, serial=v1_data.serial)
    client = _client()
    client.async_get_v1_data_current.side_effect = api_mod.Healthbox3ConnectionError(
        "offline"
    )

    with (
        _patch_discover_broadcast(
            return_value=[_discovery_info(ip="192.0.2.99", serial="some-other-serial")]
        ),
        _patch_create_flow() as mock_create_flow,
    ):
        coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=False)
        await coordinator.async_refresh()

    mock_create_flow.assert_not_called()


async def test_relocate_swallows_broadcast_socket_error(hass, v1_data):
    """A real socket-creation failure during the relocate probe must not
    raise past the coordinator's own connection-error handling.
    """
    entry = make_config_entry(hass, serial=v1_data.serial)
    client = _client()
    client.async_get_v1_data_current.side_effect = api_mod.Healthbox3ConnectionError(
        "offline"
    )

    with (
        _patch_discover_broadcast(side_effect=OSError("network unreachable")),
        _patch_create_flow() as mock_create_flow,
    ):
        coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=False)
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    mock_create_flow.assert_not_called()


async def test_relocate_attempted_once_per_outage_then_reset_on_success(hass, v1_data):
    """Must not re-probe on every failed poll while the outage continues -
    only once, until a poll succeeds again.
    """
    entry = make_config_entry(hass, serial=v1_data.serial)
    client = _client()
    client.async_get_v1_data_current.side_effect = api_mod.Healthbox3ConnectionError(
        "offline"
    )

    with (
        _patch_discover_broadcast(return_value=[]) as mock_discover,
        _patch_create_flow(),
    ):
        coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=False)
        await coordinator.async_refresh()
        await coordinator.async_refresh()

    assert mock_discover.await_count == 1

    # A successful poll resets the flag, so the next failure probes again.
    client.async_get_v1_data_current.side_effect = None
    client.async_get_v1_data_current.return_value = v1_data
    await coordinator.async_refresh()

    client.async_get_v1_data_current.side_effect = api_mod.Healthbox3ConnectionError(
        "offline again"
    )
    with (
        _patch_discover_broadcast(return_value=[]) as mock_discover,
        _patch_create_flow(),
    ):
        await coordinator.async_refresh()

    assert mock_discover.await_count == 1


async def test_relocate_triggered_on_v2_connection_error(hass, v2_data):
    """The v2 polling path hits a different except-branch than v1's own
    _async_get_v1_data - must trigger a relocate too.
    """
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.side_effect = api_mod.Healthbox3ConnectionError(
        "offline"
    )

    with (
        _patch_discover_broadcast(
            return_value=[_discovery_info(ip="192.0.2.99", serial=v2_data.serial)]
        ),
        _patch_create_flow() as mock_create_flow,
    ):
        coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
        await coordinator.async_refresh()

    mock_create_flow.assert_called_once()


async def test_one_room_boost_failure_does_not_fail_whole_update(hass, v1_data):
    entry = make_config_entry(hass, serial=v1_data.serial)
    client = _client()
    client.async_get_v1_data_current.return_value = v1_data

    async def _boost_side_effect(room_id):
        if room_id == 1:
            raise api_mod.Healthbox3ConnectionError("hiccup")
        return api_mod.BoostStatus(enable=False, level=100.0, timeout=900, remaining=0)

    client.async_get_boost.side_effect = _boost_side_effect

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=False)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert 1 not in coordinator.data.boost
    assert 2 in coordinator.data.boost


async def test_key_invalid_downgrades_to_v1_and_starts_reauth(
    hass, v1_data, v2_data, boost_status
):
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.side_effect = api_mod.Healthbox3AuthenticationError(
        "expired"
    )
    client.async_get_v1_data_current.return_value = v1_data
    client.async_get_boost.return_value = boost_status

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    assert coordinator.use_v2 is False
    assert coordinator.last_update_success is True

    # `async_start_reauth` schedules the flow as a task rather than creating
    # it inline, so it does not exist yet unless the loop gets a turn first.
    # Without this the assertion below passes when the test runs alone and
    # fails inside the full suite, purely on timing.
    await hass.async_block_till_done()
    progress = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"].get("source") == SOURCE_REAUTH for f in progress)


async def test_key_validating_is_not_treated_as_revoked(hass, v2_data):
    """A v2 failure while the device happens to be re-validating its key is
    undecided, not a revocation - downgrading to v1 and starting a reauth
    flow over a state that clears on its own would be a false alarm.
    """
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.side_effect = api_mod.Healthbox3InvalidResponseError(
        "garbled"
    )
    client.async_get_api_key_status.return_value = api_mod.ApiKeyStatus(
        state="validating",
        disable_telemetry_data_allowed=False,
        local_sensor_data_allowed=False,
    )

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    assert coordinator.use_v2 is True
    await hass.async_block_till_done()
    progress = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert not any(f["context"].get("source") == SOURCE_REAUTH for f in progress)


async def test_invalid_response_disambiguated_via_key_status(hass, v2_data):
    """A v2 parse failure while the key is still valid is transient, not a reauth trigger."""
    entry = make_config_entry(hass, serial=v2_data.serial)
    client = _client()
    client.async_get_v2_data_current.side_effect = api_mod.Healthbox3InvalidResponseError(
        "garbled"
    )
    client.async_get_api_key_status.return_value = api_mod.ApiKeyStatus(
        state="valid",
        disable_telemetry_data_allowed=True,
        local_sensor_data_allowed=True,
    )

    coordinator = Healthbox3DataUpdateCoordinator(hass, entry, client, use_v2=True)
    await coordinator.async_refresh()

    assert coordinator.use_v2 is True  # not downgraded - key is still valid
    assert coordinator.last_update_success is False


def _wire_full_poll(client, *, v2_data, boost_status, decision):
    """Configure a client so one refresh exercises every endpoint for real.

    The shared fixture defaults several calls to "not reported" so older
    tests aren't affected by them; the polling tests below are about how
    many calls a poll makes, so they need every one of them to answer.
    """
    client.async_get_v2_data_current = AsyncMock(return_value=v2_data)
    client.async_get_boost = AsyncMock(return_value=boost_status)
    client.async_get_decision_tree = AsyncMock(
        return_value=api_mod.DecisionTree(
            decision=decision,
            boost={room.id: boost_status for room in v2_data.rooms},
        )
    )
    client.async_get_global = AsyncMock(
        return_value=api_mod.GlobalInfo(firmware_version="2.6.9")
    )
    return client


async def test_one_poll_reads_every_endpoint_at_most_once(
    hass, mock_api_client, v2_data, boost_status, device_decision
):
    """The fan-out is parallel now, not sequential - which is only safe if
    nothing is accidentally asked twice per cycle.

    Counted rather than asserted per-call: the point is the shape of a
    poll (one read each, plus one boost per room), and a regression here
    would be an endpoint quietly moving inside a loop.
    """
    _wire_full_poll(
        mock_api_client,
        v2_data=v2_data,
        boost_status=boost_status,
        decision=device_decision,
    )
    entry = make_config_entry(hass, serial=v2_data.serial)
    coordinator = Healthbox3DataUpdateCoordinator(
        hass, entry, mock_api_client, use_v2=True
    )
    await coordinator.async_refresh()

    assert mock_api_client.async_get_v2_data_current.call_count == 1
    for single in (
        mock_api_client.async_get_decision_tree,
        mock_api_client.async_get_global,
        mock_api_client.async_get_errors,
        mock_api_client.async_get_device,
        mock_api_client.async_get_wifi_status,
    ):
        assert single.call_count == 1, single
    # The whole poll is now five requests whatever the room count, where
    # it used to be five plus three plus one per room.
    mock_api_client.async_get_boost.assert_not_called()


async def test_global_info_is_not_re_read_on_every_poll(
    hass, mock_api_client, v2_data, boost_status, device_decision
):
    """Firmware version, MAC, IP and serial do not change between polls.

    Re-reading them every 30 seconds was a request per poll for an answer
    that is the same for months. A good read is reused for
    GLOBAL_INFO_REFRESH_EVERY polls - and, just as importantly, still
    *reported* on those polls rather than going missing.
    """
    _wire_full_poll(
        mock_api_client,
        v2_data=v2_data,
        boost_status=boost_status,
        decision=device_decision,
    )
    entry = make_config_entry(hass, serial=v2_data.serial)
    coordinator = Healthbox3DataUpdateCoordinator(
        hass, entry, mock_api_client, use_v2=True
    )

    for _ in range(GLOBAL_INFO_REFRESH_EVERY):
        await coordinator.async_refresh()
        assert coordinator.data.global_info is not None

    assert mock_api_client.async_get_global.call_count == 1

    # The next poll is the one that asks again.
    await coordinator.async_refresh()
    assert mock_api_client.async_get_global.call_count == 2


async def test_a_failed_global_read_is_not_cached(
    hass, mock_api_client, v2_data, boost_status, device_decision
):
    """Only successful reads are reused.

    A failure still drops to None - the entities built on it go
    unavailable, exactly as before - and the next poll asks again rather
    than serving a stale reading for ten minutes.
    """
    mock_api_client.async_get_global = AsyncMock(
        side_effect=api_mod.Healthbox3Error("boom")
    )
    entry = make_config_entry(hass, serial=v2_data.serial)
    coordinator = Healthbox3DataUpdateCoordinator(
        hass, entry, mock_api_client, use_v2=True
    )

    await coordinator.async_refresh()
    assert coordinator.data.global_info is None
    await coordinator.async_refresh()
    assert coordinator.data.global_info is None
    assert mock_api_client.async_get_global.call_count == 2


async def test_scan_interval_comes_from_the_entry_options(
    hass, mock_api_client, v2_data
):
    """The interval is configurable, and read from the entry rather than
    fixed at construction."""
    entry = make_config_entry(
        hass, serial=v2_data.serial, options={CONF_SCAN_INTERVAL: 120}
    )
    coordinator = Healthbox3DataUpdateCoordinator(
        hass, entry, mock_api_client, use_v2=True
    )
    assert coordinator.update_interval == timedelta(seconds=120)


async def test_an_out_of_range_interval_is_clamped_not_rejected(hass, v2_data):
    """The only way to get one is an entry written by hand or by an older
    version; refusing to set up over it would be worse than bringing it
    back in bounds.
    """
    too_fast = make_config_entry(
        hass, serial=v2_data.serial, options={CONF_SCAN_INTERVAL: 1}
    )
    too_slow = make_config_entry(
        hass, serial=f"{v2_data.serial}-2", options={CONF_SCAN_INTERVAL: 99999}
    )
    assert scan_interval(too_fast) == timedelta(seconds=SCAN_INTERVAL_MIN)
    assert scan_interval(too_slow) == timedelta(seconds=SCAN_INTERVAL_MAX)


async def test_no_options_means_the_default_interval(hass, v2_data):
    """An entry from before the option existed keeps the interval it had."""
    entry = make_config_entry(hass, serial=v2_data.serial)
    assert scan_interval(entry) == DEFAULT_SCAN_INTERVAL
