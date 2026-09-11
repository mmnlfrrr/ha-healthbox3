"""Tests for the system health platform."""

from __future__ import annotations

from inspect import isawaitable
from typing import Any

import aiohttp
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import get_system_health_info

from custom_components.healthbox3.const import DOMAIN

from .conftest import setup_integration

STATUS_URL = "http://192.0.2.1/v2/api/api_key/status"


async def _async_info(hass) -> dict[str, Any]:
    """Return the page's values, with the reachability probe resolved.

    With a single device that value is deliberately handed to the frontend
    as an un-awaited coroutine (the frontend renders and translates the
    result itself), so a test reading the page has to do what the frontend
    does.
    """
    assert await async_setup_component(hass, "system_health", {})
    info = await get_system_health_info(hass, DOMAIN)
    reachability = info.get("can_reach_device")
    if isawaitable(reachability):
        info["can_reach_device"] = await reachability
    return info


async def test_system_health_reports_what_the_device_is_doing(
    hass, aioclient_mock, mock_api_client, v2_data, boost_status, firmware_version
):
    """The six values the page exists to answer, on a healthy setup."""
    aioclient_mock.get(STATUS_URL, json={"state": "valid"})
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        firmware_version=firmware_version,
    )

    info = await _async_info(hass)

    assert info == {
        "can_reach_device": "ok",
        "firmware_version": firmware_version,
        "api_key_active": "yes",
        "poll_interval_seconds": 30,
        "last_poll_successful": "yes",
        "rooms": len(v2_data.rooms),
    }


async def test_system_health_reports_a_device_that_does_not_answer(
    hass, aioclient_mock, mock_api_client, v2_data, boost_status
):
    """The page's whole point is the "it stopped working" case, so the
    unreachable branch is the one that has to work.

    The raw `{"type": "failed", ...}` shape is what reaches the frontend,
    which is what turns it into a translated "failed to load" line - a
    plain string here would show up untranslated.
    """
    aioclient_mock.get(STATUS_URL, exc=aiohttp.ClientError)
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    info = await _async_info(hass)

    assert info["can_reach_device"] == {"type": "failed", "error": "unreachable"}


async def test_system_health_reports_a_device_without_an_api_key(
    hass, aioclient_mock, mock_api_client, v1_data, boost_status
):
    """No key means no per-room sensors, which is the other question this
    page is here to answer without a round trip through the forum.
    """
    aioclient_mock.get(STATUS_URL, json={"state": "empty"})
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    info = await _async_info(hass)

    assert info["api_key_active"] == "no"
    # No `/renson_core/v2/global` on a v1-only setup, and a missing value
    # has to read as missing rather than as an empty row.
    assert info["firmware_version"] == "unknown"


async def test_system_health_is_empty_without_a_loaded_entry(
    hass, aioclient_mock, mock_api_client, v2_data, boost_status
):
    """Configured-but-not-loaded has no coordinator to read - `runtime_data`
    does not exist yet, so reading it at all would raise and take the whole
    page down with it, not just this integration's rows.

    An empty page is also the honest answer: a page of "unknown" would read
    as the device answering badly rather than as nothing being set up.
    """
    aioclient_mock.get(STATUS_URL, json={"state": "valid"})
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )
    assert await hass.config_entries.async_unload(entry.entry_id)

    assert await _async_info(hass) == {}


async def test_system_health_carries_nothing_identifying(
    hass, aioclient_mock, mock_api_client, v2_data, boost_status, firmware_version
):
    """This page is on screen in Settings and gets screenshotted into forum
    threads - unlike diagnostics, which the user attaches deliberately.

    The config entry's own title is `Healthbox 3 (<serial>)`, so labelling
    rows with it - the obvious way to tell two devices apart - would put
    the serial on that screenshot. Rows are numbered instead, which is why
    this test sets the titles it does.
    """
    aioclient_mock.get(STATUS_URL, json={"state": "valid"})
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        firmware_version=firmware_version,
        title="Healthbox 3 (250401ABCDE)",
    )

    info = await _async_info(hass)

    rendered = " ".join(str(value) for value in info.values())
    for secret in ("250401ABCDE", "192.0.2.1", "goodkey"):
        assert secret not in rendered, secret


async def test_system_health_summarizes_several_devices(
    hass, aioclient_mock, mock_api_client, v1_data, v2_data, boost_status
):
    """Two Healthboxes share one row per value, since the labels are
    translated per key and cannot be repeated per device.

    Without this the page would silently describe whichever entry happened
    to load first, and read as if the other did not exist.
    """
    aioclient_mock.get(STATUS_URL, json={"state": "valid"})
    await setup_integration(
        hass,
        mock_api_client,
        serial="first",
        healthbox_data=v2_data,
        boost_status=boost_status,
        title="Healthbox 3 (250401ABCDE)",
    )
    await setup_integration(
        hass,
        mock_api_client,
        serial="second",
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
        title="Healthbox 3 (250401FGHIJ)",
    )

    info = await _async_info(hass)

    assert info["api_key_active"] == "#1: yes; #2: no"
    assert info["can_reach_device"] == "#1: ok; #2: ok"
    assert info["poll_interval_seconds"] == "#1: 30; #2: 30"

    rendered = " ".join(str(value) for value in info.values())
    assert "250401ABCDE" not in rendered
    assert "250401FGHIJ" not in rendered
