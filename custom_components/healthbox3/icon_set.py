"""Serve Renson's zone pictograms to the frontend as a `custom:` icon set.

Home Assistant has no per-device icon, so a room's own pictogram can only
appear on an entity. Entity icons are addressed by name, not by artwork,
and the only way to add names is a frontend icon set: a small JavaScript
module that registers `window.customIconsets[prefix]`, which the frontend
then calls to resolve `custom:<prefix>-<name>`.

So this module does two things at setup: publish that JavaScript, and
tell the frontend to load it. The icon data itself is generated from
Renson's own drawables - see zone_icons.py.

Served from memory by a view rather than written to disk as a static
file: the component's own directory is managed by HACS and replaced on
update, and nothing here needs to outlive the process.

The whole thing is best-effort. A failure to register leaves the icons
resolving to nothing, which is a cosmetic loss on one diagnostic sensor,
so it is logged and swallowed rather than allowed to fail setup.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .zone_icons import ICON_PREFIX, ZONE_ICON_PATHS

_LOGGER = logging.getLogger(__name__)

ICON_SET_URL = f"/{DOMAIN}/zone-icons.js"
_REGISTERED_KEY = f"{DOMAIN}_icon_set_registered"

# Home Assistant resolves a custom icon by awaiting this function and
# reading `path` off the result; returning undefined means "no such icon",
# which the frontend renders as blank rather than erroring. Everything is
# inlined - the data is ~15 kB, cheaper to ship once than to fetch per
# icon, and it leaves the icons working with no further requests.
_MODULE_TEMPLATE = """// Renson zone pictograms, served by the healthbox3 integration.
const PATHS = %s;
window.customIconsets = window.customIconsets || {};
window.customIconsets[%s] = async (name) => {
  const path = PATHS[name];
  return path ? { path } : undefined;
};
"""


def build_module() -> str:
    """Return the JavaScript module registering the icon set."""
    return _MODULE_TEMPLATE % (
        json.dumps(ZONE_ICON_PATHS, separators=(",", ":"), sort_keys=True),
        json.dumps(ICON_PREFIX),
    )


class Healthbox3ZoneIconsView(HomeAssistantView):
    """Serve the icon set module.

    Unauthenticated on purpose: the frontend fetches extra JS modules
    before a session exists, and the response is artwork with nothing
    installation-specific in it - no addresses, no identifiers, the same
    bytes for every user.
    """

    url = ICON_SET_URL
    name = f"{DOMAIN}:zone-icons"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the module."""
        return web.Response(
            text=build_module(),
            content_type="application/javascript",
            charset="utf-8",
        )


async def async_register(hass: HomeAssistant) -> None:
    """Publish the icon set and ask the frontend to load it.

    Idempotent: two configured Healthbox units would otherwise register
    the same view and URL twice.
    """
    data: dict[str, Any] = hass.data
    if data.get(_REGISTERED_KEY):
        return

    try:
        hass.http.register_view(Healthbox3ZoneIconsView())
        add_extra_js_url(hass, ICON_SET_URL)
    except Exception:  # noqa: BLE001 - cosmetic only, must never fail setup
        _LOGGER.exception(
            "Could not publish the Renson zone icon set; room symbol icons "
            "will be missing, everything else is unaffected"
        )
        return

    data[_REGISTERED_KEY] = True
