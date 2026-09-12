"""The Renson Healthbox 3 integration."""

from __future__ import annotations

from homeassistant.const import CONF_API_KEY, CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Healthbox3ApiClient, Healthbox3ConnectionError, Healthbox3Error
from .card import async_register as async_register_card
from .const import DOMAIN
from .coordinator import (
    Healthbox3ConfigEntry,
    Healthbox3DataUpdateCoordinator,
    scan_interval,
)
from .entity import unit_device_info
from .icon_set import async_register as async_register_icon_set

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.FAN,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]


async def async_setup_entry(hass: HomeAssistant, entry: Healthbox3ConfigEntry) -> bool:
    """Set up Renson Healthbox 3 from a config entry."""
    client = Healthbox3ApiClient(entry.data[CONF_HOST], async_get_clientsession(hass))

    # Activation is a one-time, device-side state change (confirmed: the
    # device never expects a per-request Authorization header), not
    # something scoped to this config entry. So privileged access may
    # already be active even if the user never gave us a key - e.g. they
    # POSTed it directly per the Renson docs instead of through our config
    # flow. Always ask the device, rather than trusting only what we stored.
    try:
        status = await client.async_get_api_key_status()
    except Healthbox3ConnectionError as err:
        raise ConfigEntryNotReady(
            f"Error connecting to Healthbox 3 at {entry.data[CONF_HOST]}"
        ) from err
    except Healthbox3Error as err:
        raise ConfigEntryNotReady(
            "Unexpected error checking Healthbox 3 API key status"
        ) from err

    if status.is_pending:
        # The device is re-checking the key with Renson's servers - it does
        # this on its own after a device reboot, not only right after a key
        # is submitted. Nothing is wrong yet and nothing is decided yet, so
        # retry setup rather than either losing v2 for the whole session or
        # asking the user to re-enter a key that is probably fine.
        raise ConfigEntryNotReady("Healthbox 3 is still validating its API key")

    use_v2 = status.is_valid
    if entry.data.get(CONF_API_KEY) and not status.is_valid:
        # The user told us they had a working key; the device now disagrees.
        raise ConfigEntryAuthFailed("Healthbox 3 API key is no longer valid")

    # Cosmetic and best-effort, so they run before anything that can fail
    # and never gate setup - see icon_set.py and card.py.
    await async_register_icon_set(hass)
    await async_register_card(hass)

    coordinator = Healthbox3DataUpdateCoordinator(
        hass, entry, client, use_v2=use_v2
    )
    await coordinator.async_config_entry_first_refresh()

    # The unit's device entry is created here, before any platform, rather
    # than being left to whichever entity happens to be added first. Each
    # room device nests under it by registry id (`via_device_id`), and a
    # registry id only exists once the device does - so the order has to be
    # guaranteed rather than inferred from the platform list.
    coordinator.unit_device_id = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        **unit_device_info(coordinator, coordinator.data.healthbox.serial),
    ).id

    entry.runtime_data = coordinator
    # The poll interval is read once, when the coordinator is built, so a
    # change to it only takes effect on a reload - which this listener is
    # what triggers.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: Healthbox3ConfigEntry
) -> None:
    """Reload the entry when the poll interval changes, and only then.

    Home Assistant calls an update listener on *any* change to the entry,
    not just its options - renaming it counts, and a rename used to tear
    the integration down and set it up again, every entity going briefly
    unavailable for a change that is purely cosmetic.

    The reconfigure and discovery-relocation flows do their own reload
    (`async_update_reload_and_abort`), so they no longer get a second one
    from here either.

    The comparison is against the interval the coordinator is actually
    running on rather than a remembered copy: that is the applied value by
    definition, so if the entry still asks for it, nothing about polling
    has changed.
    """
    if entry.runtime_data.update_interval == scan_interval(entry):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: Healthbox3ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: Healthbox3ConfigEntry,
    device: dr.DeviceEntry,
) -> bool:
    """Allow deleting the device of a room the unit no longer reports.

    Without this hook Home Assistant refuses every deletion, and a vent
    that has been physically removed leaves a device behind for good, with
    its entities stuck on unavailable and a Delete button that does
    nothing. The device is the only thing the user can act on, so being
    unable to act on it is the whole problem.

    Live rooms - and the unit itself - are refused: deleting one of those
    would only have it recreated on the next poll, which reads as the
    button being broken.
    """
    coordinator = entry.runtime_data
    serial = coordinator.data.healthbox.serial
    live = {(DOMAIN, serial)} | {
        (DOMAIN, f"{serial}_room{room.id}")
        for room in coordinator.data.healthbox.rooms
    }
    return not (device.identifiers & live)
