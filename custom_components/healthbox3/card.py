"""A Lovelace card drawing the unit with its outlets, as the Renson app does.

Two pieces, in two views:

`/api/healthbox3/layout` answers with the installation's actual topology -
which collector ports carry a room, what that room is called, which
pictogram it uses, and the entity ids of its readings. The integration
already knows all of this exactly, so the card never has to guess it from
entity names or ids, which is what makes the same card work on any
install and in any language.

`/healthbox3/healthbox-card.js` is the card itself. It draws Renson's own
artwork (see scene_assets.py), places an outlet at each port, caps the
rest, and reads live values straight out of `hass.states`.

Registration is best-effort in exactly the way icon_set.py's is: a card
that fails to publish costs a dashboard element, not the integration.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from .api import DeviceError, HealthboxData, room_symbol, room_valve_port
from .const import DOMAIN
from .scene_assets import SCENE_ASSETS, SCENE_GEOMETRY
from .zone_icons import FALLBACK_ICON, ICON_PREFIX, ROOM_SYMBOL_TO_ICON

_LOGGER = logging.getLogger(__name__)

CARD_URL = f"/{DOMAIN}/healthbox-card.js"
LAYOUT_URL = f"/api/{DOMAIN}/layout"
_REGISTERED_KEY = f"{DOMAIN}_card_registered"

# Which of a room's entities the card draws. Keyed by the suffix of the
# unique id each one is registered under, so the lookup goes through the
# entity registry rather than through entity ids, which users rename.
_ROOM_ENTITIES: dict[str, tuple[str, str]] = {
    "airflow": ("sensor", "airflow"),
    "airflow_rate": ("sensor", "airflow_rate"),
    "aqi_level": ("sensor", "aqi_level"),
    "legislation_code": ("sensor", "legislation_code"),
    "boost": ("fan", "boost"),
    "profile": ("select", "profile"),
}


def _room_entities(
    registry: er.EntityRegistry, serial: str, room_id: int
) -> dict[str, str]:
    """Resolve a room's entity ids, skipping the ones it doesn't have.

    A room only gets the entities its own hardware supports - no air
    quality without an API key, no profile select on a v1-only unit - so
    a missing one is normal and simply isn't offered to the card.
    """
    found: dict[str, str] = {}
    for key, (platform, suffix) in _ROOM_ENTITIES.items():
        entity_id = registry.async_get_entity_id(
            platform, DOMAIN, f"{serial}_room{room_id}_{suffix}"
        )
        if entity_id is not None:
            found[key] = entity_id
    return found


def _room_area(
    area_registry: ar.AreaRegistry,
    devices: dict[tuple[str, str], dr.DeviceEntry],
    entity_registry: er.EntityRegistry,
    serial: str,
    room_id: int,
    entity_id: str | None,
) -> str | None:
    """Return the Home Assistant area this room sits in, if any.

    Each ventilated room is its own device (see entity.py), and an area is
    assigned per device, so that is where the answer normally is. An
    entity can override its device's area though, so a room entity that
    has been moved on its own wins - that is the order Home Assistant
    itself resolves in.
    """
    area_id: str | None = None
    if entity_id is not None:
        entry = entity_registry.async_get(entity_id)
        if entry is not None:
            area_id = entry.area_id
    if area_id is None:
        device = devices.get((DOMAIN, f"{serial}_room{room_id}"))
        area_id = device.area_id if device is not None else None
    if area_id is None:
        return None
    area = area_registry.async_get_area(area_id)
    return area.name if area is not None else None


def _attribute_errors(
    errors: list[DeviceError], healthbox: HealthboxData
) -> tuple[set[int], int]:
    """Split reported errors into "this outlet" and "somewhere else".

    `/v1/error` gives an `association_id` and nothing that says what it
    identifies. The Renson SDK's own error model carries a context type
    beside it - GLOBAL, DEVICE, COLLECTOR and the like - but the local
    endpoint does not return that field, and no real unit has ever been
    observed reporting an error at all, so there is no capture to settle
    it against.

    So the rule here is deliberately one that cannot mis-attribute: an
    error is pinned to an outlet only when its association id is exactly
    one of this unit's own collector port numbers. Anything else - an
    opaque id, a serial, a sensor id - is counted as unattributed and
    shown on the unit rather than blamed on a room. The cost of being
    wrong the other way (marking the wrong duct faulty) is much higher
    than showing a fault without saying where.
    """
    ports = {
        port
        for port in (room_valve_port(room) for room in healthbox.rooms)
        if port is not None
    }
    faulted: set[int] = set()
    unattributed = 0
    for error in errors:
        try:
            port = int(error.association_id)
        except (TypeError, ValueError):
            port = None
        if port is not None and port in ports:
            faulted.add(port)
        else:
            unattributed += 1
    return faulted, unattributed


def build_layout(hass: HomeAssistant) -> dict[str, Any]:
    """Return every configured unit's topology."""
    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    units: list[dict[str, Any]] = []

    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is None or coordinator.data is None:
            continue
        healthbox = coordinator.data.healthbox
        serial = healthbox.serial
        devices = {
            identifier: device
            for device in dr.async_entries_for_config_entry(
                device_registry, entry.entry_id
            )
            for identifier in device.identifiers
        }

        faulted, unattributed = _attribute_errors(coordinator.data.errors, healthbox)

        rooms = []
        for room in healthbox.rooms:
            port = room_valve_port(room)
            if port is None:
                # Without a port there is nowhere on the drawing to put
                # it; it stays a normal entity, just not on the card.
                continue
            symbol = room_symbol(room)
            entities = _room_entities(registry, serial, room.id)
            rooms.append(
                {
                    "id": room.id,
                    "port": port,
                    "name": room.name,
                    "area": _room_area(
                        area_registry,
                        devices,
                        registry,
                        serial,
                        room.id,
                        entities.get("airflow"),
                    ),
                    "icon": "custom:{}-{}".format(
                        ICON_PREFIX,
                        ROOM_SYMBOL_TO_ICON.get(symbol or "", FALLBACK_ICON),
                    ),
                    "error": port in faulted,
                    "entities": entities,
                }
            )

        units.append(
            {
                "serial": serial,
                "name": healthbox.description,
                # Sorted by port, then by room id, so a split outlet's
                # branches are numbered in a stable order rather than
                # shuffling between refreshes.
                "rooms": sorted(rooms, key=lambda room: (room["port"], room["id"])),
                "unattributed_errors": unattributed,
            }
        )

    return {"units": units}


class Healthbox3LayoutView(HomeAssistantView):
    """Serve the installation's topology to the card."""

    url = LAYOUT_URL
    name = f"{DOMAIN}:layout"

    async def get(self, request: web.Request) -> web.Response:
        """Return the layout."""
        hass: HomeAssistant = request.app["hass"]
        return self.json(build_layout(hass))


class Healthbox3CardView(HomeAssistantView):
    """Serve the card module.

    Unauthenticated for the same reason as the icon set: the frontend
    fetches dashboard resources before a session exists, and the response
    is code plus artwork, identical for every installation. Everything
    installation-specific is behind the authenticated layout view.
    """

    url = CARD_URL
    name = f"{DOMAIN}:card"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the card."""
        return web.Response(
            text=build_card_module(),
            content_type="application/javascript",
            charset="utf-8",
        )


def build_card_module() -> str:
    """Return the JavaScript module defining the card."""
    return _CARD_TEMPLATE % {
        "assets": json.dumps(SCENE_ASSETS, separators=(",", ":"), sort_keys=True),
        "geometry": json.dumps(SCENE_GEOMETRY, separators=(",", ":"), sort_keys=True),
        "layout_url": json.dumps(LAYOUT_URL.removeprefix("/api/")),
        "domain": json.dumps(DOMAIN),
    }


async def async_register(hass: HomeAssistant) -> None:
    """Publish the card and its layout endpoint. Idempotent."""
    data: dict[str, Any] = hass.data
    if data.get(_REGISTERED_KEY):
        return

    try:
        hass.http.register_view(Healthbox3LayoutView())
        hass.http.register_view(Healthbox3CardView())
        add_extra_js_url(hass, CARD_URL)
    except Exception:  # noqa: BLE001 - a dashboard extra, never fatal
        _LOGGER.exception(
            "Could not publish the Healthbox card; every entity still works, "
            "only the card is unavailable"
        )
        return

    data[_REGISTERED_KEY] = True


# The card. Ports are numbered the way Renson numbers them: 1 is the
# upper-right outlet and they run clockwise, which is the order printed on
# the unit itself. Positions sit a quarter and three quarters along each
# edge, matching the app's own drawing.
_CARD_TEMPLATE = """// Healthbox 3 card, served by the healthbox3 integration.
const ASSETS = %(assets)s;
const G = %(geometry)s;

const P25 = G.base_x + G.base_size * 0.25;
const P75 = G.base_x + G.base_size * 0.75;
const PORTS = {
  1: ["right", P25], 2: ["right", P75],
  3: ["bottom", P75], 4: ["bottom", P25],
  5: ["left", P75], 6: ["left", P25],
  7: ["top", P25],
};

// A split outlet is a chain: the branches sit end to end running away
// from the unit, 1.1 against the casing then 1.2 beyond it, which is how
// the Renson app draws it. `step` is how far out each further branch is,
// and it is the drawing's own width so the brackets butt up against each
// other with no gap.
// CSS anchor positioning, which lets the browser keep the hover panel
// inside the card by flipping it. Detected rather than assumed: it is
// recent enough that a Home Assistant user may well be on a browser
// without it, and the fallback below is perfectly serviceable.
const ANCHORED =
  typeof CSS !== "undefined" &&
  typeof CSS.supports === "function" &&
  CSS.supports("position-area", "block-start");

const outward = (side) =>
  side === "left" || side === "right" ? G.valve_side_w : G.valve_end_h;

const place = (port, art, branch = 0) => {
  const [side, a] = PORTS[port];
  const sw = G.valve_side_w, sh = G.valve_side_h;
  const ew = G.valve_end_w, eh = G.valve_end_h;
  const out = outward(side) * branch;
  if (side === "left")
    return `<g transform="translate(${G.base_x - sw - out},${a - sh / 2})">${ASSETS[art + "_left"]}</g>`;
  if (side === "right")
    return `<g transform="translate(${G.base_x + G.base_size + sw + out},${a - sh / 2}) scale(-1,1)">${ASSETS[art + "_left"]}</g>`;
  if (side === "top")
    return `<g transform="translate(${a - ew / 2},${G.base_y - eh - out})">${ASSETS[art + "_top"]}</g>`;
  return `<g transform="translate(${a - ew / 2},${G.base_y + G.base_size + eh + out}) scale(1,-1)">${ASSETS[art + "_top"]}</g>`;
};

const badgeAt = (port, branch = 0) => {
  const [side, a] = PORTS[port];
  const out = outward(side) * branch;
  if (side === "left") return [G.base_x - G.valve_side_w / 2 - out, a];
  if (side === "right") return [G.base_x + G.base_size + G.valve_side_w / 2 + out, a];
  if (side === "top") return [a, G.base_y - G.valve_end_h / 2 - out];
  return [a, G.base_y + G.base_size + G.valve_end_h / 2 + out];
};

class HealthboxCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._layout = null;
  }

  static getStubConfig() {
    return {};
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._layout) {
      // Fetched once: the topology only changes when a vent is added,
      // which means an integration reload anyway.
      this._layout = "loading";
      hass.callApi("GET", %(layout_url)s).then(
        (data) => {
          this._layout = data;
          this._render();
        },
        () => {
          this._layout = null;
          this._fail("Could not read the Healthbox layout.");
        },
      );
      return;
    }
    if (this._layout !== "loading") this._render();
  }

  _fail(message) {
    this.innerHTML = `<ha-card><div style="padding:16px">${message}</div></ha-card>`;
  }

  _unit() {
    const units = (this._layout && this._layout.units) || [];
    if (!units.length) return null;
    if (!this._config.serial) return units[0];
    return units.find((u) => u.serial === this._config.serial) || null;
  }

  _state(entityId) {
    const s = entityId && this._hass.states[entityId];
    return s && s.state !== "unavailable" && s.state !== "unknown" ? s : null;
  }

  _render() {
    const unit = this._unit();
    if (!unit) {
      this._fail("No Healthbox found.");
      return;
    }

    // A physical outlet can be split into several branches, each its own
    // room, so ports hold a list. One room on a port keeps the plain
    // number; several get dotted branch labels, the way Renson writes
    // them: 1.1, 1.2, 1.3.
    const byPort = new Map();
    for (const room of unit.rooms) {
      if (!byPort.has(room.port)) byPort.set(room.port, []);
      byPort.get(room.port).push(room);
    }

    const view = this._viewBox(byPort);
    const parts = [
      `<g transform="translate(${G.exhaust_x},${G.exhaust_y})">${ASSETS.healthbox_exhaust}</g>`,
      `<g transform="translate(${G.base_x},${G.base_y})">${ASSETS.healthbox_base}</g>`,
    ];

    for (const port of Object.keys(PORTS).map(Number)) {
      const rooms = byPort.get(port);
      if (!rooms) {
        parts.push(place(port, "valve_closed"));
        continue;
      }
      rooms.forEach((room, i) => {
        parts.push(place(port, room.error ? "valve_manual_error" : "valve_manual", i));
      });
    }

    for (const [port, rooms] of byPort) {
      parts.push(this._outlet(port, rooms, view));
    }

    const warn = unit.unattributed_errors
      ? `<div style="position:absolute;top:4px;left:8px;color:var(--error-color,#db4437)"` +
        ` title="${unit.unattributed_errors} error(s) not attributable to an outlet">` +
        `⚠ ${unit.unattributed_errors}</div>`
      : "";

    this.innerHTML =
      `<ha-card header="${unit.name}">` +
      `<div class="hb3" style="position:relative;padding:8px 8px 16px;` +
      `color:var(--primary-text-color)">` +
      `<svg viewBox="${view.join(" ")}" style="width:100%%;height:auto;display:block">` +
      `${parts.join("")}</svg>${warn}${this._tipElement()}</div></ha-card>`;

    this._bind(unit);
  }

  _tipElement() {
    // An HTML panel rather than an SVG <title>: it has to show the room's
    // pictogram, which a native tooltip cannot do.
    //
    // Where it goes is decided two ways. Given CSS anchor positioning,
    // the browser places it against an anchor and flips it to the other
    // side when it would spill out of the card - which matters for the
    // outlets near an edge, and is not something a fixed offset can do.
    // Without it, the panel is placed directly at the badge's position,
    // which is known exactly (see _outlet) and needs no measuring, but
    // always sits above.
    const common =
      `pointer-events:none;white-space:nowrap;display:flex;align-items:center;` +
      `gap:6px;padding:6px 10px;border-radius:8px;font-size:13px;z-index:1;` +
      `background:var(--card-background-color,#fff);color:var(--primary-text-color);` +
      `box-shadow:var(--ha-card-box-shadow,0 2px 8px rgba(0,0,0,.25));` +
      `border:1px solid var(--divider-color,rgba(127,127,127,.3));`;

    const placement = ANCHORED
      ? `position:absolute;position-anchor:--hb3-anchor;position-area:block-start;` +
        `position-try-fallbacks:flip-block,flip-inline,flip-block flip-inline;`
        + `margin:8px;`
      : `position:absolute;transform:translate(-50%%,-115%%);`;

    return (
      `<div class="hb3-anchor" style="position:absolute;width:0;height:0;` +
      `anchor-name:--hb3-anchor"></div>` +
      `<div class="hb3-tip" hidden style="${placement}${common}"></div>`
    );
  }

  _bind(unit) {
    const tip = this.querySelector(".hb3-tip");
    const anchor = this.querySelector(".hb3-anchor");

    this.querySelectorAll("[data-room]").forEach((node) => {
      const room = unit.rooms[Number(node.dataset.room)];
      node.style.cursor = "pointer";

      const show = () => {
        tip.innerHTML = this._tipContent(room, node.dataset.label);
        // One anchor, moved to the hovered badge, rather than an anchor
        // name per outlet: the panel is single too, so a second one would
        // never be pointed at.
        const target = ANCHORED ? anchor : tip;
        target.style.left = `${node.dataset.left}%%`;
        target.style.top = `${node.dataset.top}%%`;
        tip.hidden = false;
      };
      const hide = () => {
        tip.hidden = true;
      };

      node.addEventListener("mouseenter", show);
      node.addEventListener("focus", show);
      node.addEventListener("mouseleave", hide);
      node.addEventListener("blur", hide);
      node.addEventListener("click", () => {
        const id = node.dataset.entity;
        if (!id) return;
        const event = new Event("hass-more-info", { bubbles: true, composed: true });
        event.detail = { entityId: id };
        this.dispatchEvent(event);
      });
    });
  }

  _viewBox(byPort) {
    // Computed, not fixed: a chain grows the drawing in whichever
    // direction it runs, and a fixed box silently clipped anything on the
    // top or bottom edge - two branches there already overflowed it.
    //
    // Every port position draws something - a connection or a blanking
    // cap - so one valve depth on each side is always occupied, whether
    // or not a room is wired there. Counting only the wired ones cut the
    // caps off the edges that had none.
    let left = G.valve_side_w;
    let right = G.valve_side_w;
    let top = Math.max(G.valve_end_h, G.base_y - G.exhaust_y);
    let bottom = G.valve_end_h;

    for (const [port, rooms] of byPort) {
      const [side] = PORTS[port];
      const reach = outward(side) * rooms.length + 14; // + the badge
      if (side === "left") left = Math.max(left, reach);
      else if (side === "right") right = Math.max(right, reach);
      else if (side === "top") top = Math.max(top, reach);
      else bottom = Math.max(bottom, reach);
    }

    // Same margin on opposite sides, so the unit sits in the middle of
    // the card however lopsided the installation is.
    const margin = 8;
    const dx = Math.max(left, right) + margin;
    const dy = Math.max(top, bottom) + margin;
    const cx = G.base_x + G.base_size / 2;
    const cy = G.base_y + G.base_size / 2;
    const half = G.base_size / 2;
    return [cx - half - dx, cy - half - dy, (half + dx) * 2, (half + dy) * 2];
  }

  _outlet(port, rooms, view) {
    const split = rooms.length > 1;

    return rooms
      .map((room, i) => {
        const [bx, by] = badgeAt(port, i);
        const label = split ? `${port}.${i + 1}` : String(port);
        const colour = room.error ? "var(--error-color,#db4437)" : "currentColor";
        const index = this._unit().rooms.indexOf(room);
        // Percent of the viewport: the SVG scales to the card's width and
        // keeps its aspect, so these map straight onto the rendered box
        // with nothing to measure at runtime.
        const left = ((bx - view[0]) / view[2]) * 100;
        const top = ((by - view[1]) / view[3]) * 100;

        return (
          `<g data-room="${index}" data-label="${label}"` +
          ` data-entity="${room.entities.airflow || room.entities.boost || ""}"` +
          ` data-left="${left.toFixed(3)}" data-top="${top.toFixed(3)}" tabindex="0">` +
          `<circle cx="${bx}" cy="${by}" r="${split ? 11 : 12}"` +
          ` fill="var(--card-background-color,#fff)" stroke="${colour}" stroke-width="2"/>` +
          `<text x="${bx}" y="${by}" text-anchor="middle" dominant-baseline="central"` +
          ` font-size="${split ? 11 : 14}" fill="${colour}">${label}</text></g>`
        );
      })
      .join("");
  }

  _tipContent(room, label) {
    // Three tiers: what the room is, where it sits, what it is doing.
    // The middle one is the quiet one - the Home Assistant area and the
    // regulatory code are context you look up, not figures you watch.
    const where = [];
    if (room.area) where.push(room.area);
    const code = this._state(room.entities.legislation_code);
    if (code) where.push(code.state);

    const detail = [];
    const flow = this._state(room.entities.airflow);
    const rate = this._state(room.entities.airflow_rate);
    const aqi = this._state(room.entities.aqi_level);
    const profile = this._state(room.entities.profile);
    const boost = this._state(room.entities.boost);

    if (flow) detail.push(`${Math.round(Number(flow.state))}%%`);
    if (rate) detail.push(`${Math.round(Number(rate.state))} m³/h`);
    if (aqi) detail.push(this._label(aqi));
    if (profile) detail.push(this._label(profile));
    if (boost && boost.state === "on") detail.push("Boost");

    return (
      `<ha-icon icon="${room.icon}" style="--mdc-icon-size:22px;` +
      `color:${room.error ? "var(--error-color,#db4437)" : "inherit"}"></ha-icon>` +
      `<span style="line-height:1.35"><b>${label} · ${room.name}</b>` +
      (where.length
        ? `<br><span style="font-size:11px;opacity:.55">` +
          `${where.join(" · ")}</span>`
        : "") +
      (detail.length
        ? `<br><span style="opacity:.75">${detail.join(" · ")}</span>`
        : "") +
      `</span>`
    );
  }

  _label(state) {
    // Whatever the frontend already shows for this state, so the tooltip
    // speaks the user's language rather than the device's.
    const attrs = state.attributes || {};
    return this._hass.formatEntityState
      ? this._hass.formatEntityState(state)
      : attrs.friendly_name || state.state;
  }
}

if (!customElements.get("healthbox-card")) {
  customElements.define("healthbox-card", HealthboxCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "healthbox-card",
    name: "Renson Healthbox",
    description: "The unit and its outlets, drawn as in the Renson app.",
  });
}
"""
