"""Tests for Healthbox3 config entry setup/unload."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigEntryState, SOURCE_REAUTH
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.helpers import entity_registry as er

from custom_components.healthbox3 import api as api_mod
from custom_components.healthbox3.const import DEFAULT_SCAN_INTERVAL, DOMAIN

from .conftest import make_config_entry, setup_integration


async def test_setup_entry_without_api_key_uses_v1(hass, mock_api_client, v1_data, boost_status):
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.use_v2 is False
    # The device is always asked, even with no key on file - see
    # test_setup_entry_detects_externally_activated_key below.
    mock_api_client.async_get_api_key_status.assert_awaited_once()


async def test_setup_entry_detects_externally_activated_key(
    hass, mock_api_client, v2_data, boost_status
):
    """A key POSTed directly to the device (bypassing our config flow
    entirely, e.g. via curl per Renson's docs) must still be picked up,
    since activation lives on the device, not in what this config entry
    remembers.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        api_key=None,
        api_key_valid=True,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.use_v2 is True


async def test_setup_entry_with_valid_api_key_uses_v2(hass, mock_api_client, v2_data, boost_status):
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.use_v2 is True


async def test_setup_entry_with_already_invalid_key_triggers_reauth(hass, mock_api_client, v2_data):
    mock_api_client.async_get_api_key_status = AsyncMock(
        return_value=api_mod.ApiKeyStatus(
            state="empty",
            disable_telemetry_data_allowed=False,
            local_sensor_data_allowed=False,
        )
    )
    entry = make_config_entry(hass, serial=v2_data.serial)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    progress = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"].get("source") == SOURCE_REAUTH for f in progress)


async def test_setup_while_key_is_validating_retries_instead_of_reauth(
    hass, mock_api_client, v2_data
):
    """The device re-checks its key with Renson on its own after a reboot,
    and reports "validating" while it does. Nothing is wrong and nothing is
    decided yet, so setup must retry - not ask the user to re-enter a key
    that is almost certainly fine.
    """
    mock_api_client.async_get_api_key_status = AsyncMock(
        return_value=api_mod.ApiKeyStatus(
            state="validating",
            disable_telemetry_data_allowed=False,
            local_sensor_data_allowed=False,
        )
    )
    entry = make_config_entry(hass, serial=v2_data.serial)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    progress = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert not any(f["context"].get("source") == SOURCE_REAUTH for f in progress)


async def test_setup_entry_connection_error_retries(hass, mock_api_client, v2_data):
    mock_api_client.async_get_api_key_status = AsyncMock(
        side_effect=api_mod.Healthbox3ConnectionError("offline")
    )
    entry = make_config_entry(hass, serial=v2_data.serial)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_entry(hass, mock_api_client, v1_data, boost_status):
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_renaming_the_entry_does_not_reload_it(
    hass, mock_api_client, v2_data, boost_status
):
    """Home Assistant calls an update listener on any change to the entry,
    renaming included.

    That used to tear the integration down and set it up again - every
    entity briefly unavailable - for a change that is purely cosmetic. It
    is also what made a test in this suite fail in a way that took a while
    to explain, which is how it was found.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )
    coordinator = entry.runtime_data

    hass.config_entries.async_update_entry(entry, title="Upstairs Healthbox")
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # The same coordinator object: a reload would have built a new one.
    assert entry.runtime_data is coordinator


async def test_changing_the_poll_interval_does_reload_it(
    hass, mock_api_client, v2_data, boost_status
):
    """The interval is read once, when the coordinator is built, so it only
    takes effect on a reload - which is the listener's whole reason to
    exist.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )
    coordinator = entry.runtime_data
    assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL

    hass.config_entries.async_update_entry(entry, options={CONF_SCAN_INTERVAL: 120})
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not coordinator
    assert entry.runtime_data.update_interval == timedelta(seconds=120)


async def test_setup_deletes_entities_this_version_no_longer_creates(
    hass, mock_api_client, v2_data, boost_status, firmware_version
):
    """An entity that stops being created does not go away on its own.

    Its registry entry survives, Home Assistant shows it as restored and
    forever unavailable, and it stays in every list and every automation
    picker with a Delete button that is the user's problem rather than
    ours. So the ones removed on purpose are removed properly, on the
    first setup after the upgrade.

    Both groups at once, on a wired unit: the three that the device entry
    now carries (firmware version, IP, MAC), and the two that a wired
    unit can never answer (Wi-Fi status, Internet connection).
    """
    entry = make_config_entry(hass, serial=v2_data.serial)
    registry = er.async_get(hass)
    stale = {
        ("sensor", "firmware_version"),
        ("sensor", "ip_address"),
        ("sensor", "mac_address"),
        ("sensor", "wifi_status"),
        ("binary_sensor", "internet_connection"),
    }
    for platform, suffix in stale:
        registry.async_get_or_create(
            platform,
            DOMAIN,
            f"{v2_data.serial}_{suffix}",
            config_entry=entry,
        )

    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        firmware_version=firmware_version,
        entry=entry,
    )

    for platform, suffix in stale:
        assert (
            registry.async_get_entity_id(
                platform, DOMAIN, f"{v2_data.serial}_{suffix}"
            )
            is None
        )


async def test_setup_keeps_the_wifi_entities_of_a_wireless_unit(
    hass, mock_api_client, v2_data, boost_status, firmware_version
):
    """The Wi-Fi pair is dropped only where it could never answer.

    On a unit actually on Wi-Fi they are recreated normally, keeping the
    history they already had - which is the whole reason the two groups
    are pruned under different conditions.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        firmware_version=firmware_version,
        interface_type="WIFI",
        wifi=api_mod.WifiStatus(status="connected", ssid="somewhere"),
    )

    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{v2_data.serial}_wifi_status"
        )
        is not None
    )
    assert (
        registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{v2_data.serial}_internet_connection"
        )
        is not None
    )
