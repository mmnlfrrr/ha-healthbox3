"""Checks on the Healthbox card and the layout it is driven by.

The card's own drawing was verified separately, by running the module in
Node against a stub `hass` and rasterising the SVG it produced. What is
worth pinning here is the half that changes with the code: the layout the
integration hands it, and that the module it serves is well-formed.
"""

from __future__ import annotations

import copy
import json

from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from custom_components.healthbox3 import api as api_mod
from custom_components.healthbox3.card import (
    CARD_URL,
    LAYOUT_URL,
    build_card_module,
    build_layout,
)
from custom_components.healthbox3.const import DOMAIN
from custom_components.healthbox3.scene_assets import SCENE_ASSETS, SCENE_GEOMETRY

from .conftest import setup_integration


async def test_layout_reports_rooms_by_collector_port(
    hass, mock_api_client, v2_data, boost_status
):
    """The card places rooms by port, so that is what the layout leads
    with - together with the entity ids it should read, resolved through
    the registry rather than guessed from names.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    layout = build_layout(hass)

    assert len(layout["units"]) == 1
    unit = layout["units"][0]
    assert unit["serial"] == v2_data.serial
    assert [room["port"] for room in unit["rooms"]] == [1, 2, 3, 4, 5, 6, 7]

    toilet = unit["rooms"][0]
    assert toilet["name"] == "Toilet"
    assert toilet["icon"] == "healthbox:toilet"
    assert toilet["entities"]["airflow"].startswith("sensor.")
    assert toilet["entities"]["boost"].startswith("fan.")


async def test_layout_uses_the_pictogram_the_device_picked(
    hass, mock_api_client, v2_data, boost_status
):
    """Room 3 is a BedRoom carrying the StudioFlat icon; the layout must
    follow the icon, which is what Renson's own UIs draw.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    rooms = {room["id"]: room for room in build_layout(hass)["units"][0]["rooms"]}

    assert rooms[3]["icon"] == "healthbox:bed"
    assert rooms[4]["icon"] == "healthbox:living"


async def test_layout_skips_a_room_with_no_collector_port(
    hass, mock_api_client, v2_data, boost_status
):
    """There is nowhere on the drawing to put it. It stays a normal set of
    entities, it just isn't on the card.
    """
    stripped = copy.deepcopy(v2_data)
    del next(r for r in stripped.rooms if r.id == 1).parameters["valve"]

    await setup_integration(
        hass,
        mock_api_client,
        serial=stripped.serial,
        healthbox_data=stripped,
        boost_status=boost_status,
    )

    ids = [room["id"] for room in build_layout(hass)["units"][0]["rooms"]]
    assert 1 not in ids
    assert len(ids) == 6


async def test_layout_omits_entities_a_room_does_not_have(
    hass, mock_api_client, v1_data, boost_status
):
    """A v1-only unit has no profile select and no air quality; the card
    is told what exists rather than being left to probe for it.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    room = build_layout(hass)["units"][0]["rooms"][0]

    assert "boost" in room["entities"]
    assert "profile" not in room["entities"]
    assert "aqi_level" not in room["entities"]


async def test_a_split_outlet_keeps_both_rooms_on_the_same_port(
    hass, mock_api_client, v2_data, boost_status
):
    """One physical outlet can feed several rooms. They must all survive
    into the layout, in a stable order, or the card would draw one and
    silently drop the rest.
    """
    split = copy.deepcopy(v2_data)
    for room_id in (2, 3):
        next(r for r in split.rooms if r.id == room_id).parameters["valve"].value = "1"

    await setup_integration(
        hass,
        mock_api_client,
        serial=split.serial,
        healthbox_data=split,
        boost_status=boost_status,
    )

    rooms = build_layout(hass)["units"][0]["rooms"]
    on_port_1 = [room["id"] for room in rooms if room["port"] == 1]
    assert on_port_1 == [1, 2, 3]


async def test_chains_on_every_edge_survive_into_the_layout(
    hass, mock_api_client, v2_data, boost_status
):
    """Splits are not a right-hand-edge curiosity: 2.1, 3.1, 6.3 and 7.2
    are all shapes a real installation can take, and each edge of the
    drawing chains in a different direction.

    The layout has to keep every branch, grouped by port and ordered
    within it, because that order is what becomes the `.1`, `.2`, `.3`
    the card prints.
    """
    wired = copy.deepcopy(v2_data)
    # ports 2 and 3 get two branches, 6 gets three, 7 keeps one.
    ports = {1: 2, 2: 2, 3: 3, 4: 3, 5: 3, 6: 6, 7: 7}
    for room in wired.rooms:
        room.parameters["valve"].value = str(ports[room.id])

    await setup_integration(
        hass,
        mock_api_client,
        serial=wired.serial,
        healthbox_data=wired,
        boost_status=boost_status,
    )

    rooms = build_layout(hass)["units"][0]["rooms"]
    grouped: dict[int, list[int]] = {}
    for room in rooms:
        grouped.setdefault(room["port"], []).append(room["id"])

    assert grouped == {2: [1, 2], 3: [3, 4, 5], 6: [6], 7: [7]}
    # Ordered by port, then by room id, so the branch numbering is stable.
    assert [room["port"] for room in rooms] == [2, 2, 3, 3, 3, 6, 7]


async def test_layout_reports_the_home_assistant_area_of_each_room(
    hass, mock_api_client, v2_data, boost_status
):
    """Rooms are devices, and areas are assigned per device, so that is
    where the answer normally is.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    areas = ar.async_get(hass)
    devices = dr.async_get(hass)
    bathroom = areas.async_create("Salle de bains")
    device = next(
        d
        for d in dr.async_entries_for_config_entry(devices, entry.entry_id)
        if (DOMAIN, f"{v2_data.serial}_room2") in d.identifiers
    )
    devices.async_update_device(device.id, area_id=bathroom.id)

    rooms = {r["id"]: r for r in build_layout(hass)["units"][0]["rooms"]}

    assert rooms[2]["area"] == "Salle de bains"
    assert rooms[1]["area"] is None


async def test_an_entity_moved_on_its_own_overrides_its_device_area(
    hass, mock_api_client, v2_data, boost_status
):
    """Home Assistant lets an entity sit in a different area from its
    device, and resolves the entity's own first. The card should say what
    Home Assistant says.
    """
    entry = await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    areas = ar.async_get(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    on_device = areas.async_create("Cellier")
    on_entity = areas.async_create("Buanderie")

    device = next(
        d
        for d in dr.async_entries_for_config_entry(devices, entry.entry_id)
        if (DOMAIN, f"{v2_data.serial}_room2") in d.identifiers
    )
    devices.async_update_device(device.id, area_id=on_device.id)
    airflow = entities.async_get_entity_id(
        "sensor", DOMAIN, f"{v2_data.serial}_room2_airflow"
    )
    entities.async_update_entity(airflow, area_id=on_entity.id)

    rooms = {r["id"]: r for r in build_layout(hass)["units"][0]["rooms"]}

    assert rooms[2]["area"] == "Buanderie"


async def test_layout_offers_the_legislation_code_entity(
    hass, mock_api_client, v2_data, boost_status
):
    """The regulatory code is shown beside the room in Renson's own app,
    so the card offers it too - but only where the unit reports one.
    """
    coded = copy.deepcopy(v2_data)
    next(r for r in coded.rooms if r.id == 1).parameters["legislation_code"] = (
        api_mod.Parameter(value="C22")
    )

    await setup_integration(
        hass,
        mock_api_client,
        serial=coded.serial,
        healthbox_data=coded,
        boost_status=boost_status,
    )

    rooms = {r["id"]: r for r in build_layout(hass)["units"][0]["rooms"]}

    assert rooms[1]["entities"]["legislation_code"].startswith("sensor.")
    assert "legislation_code" not in rooms[2]["entities"]


async def test_an_error_is_pinned_to_an_outlet_only_when_it_names_a_port(
    hass, mock_api_client, v2_data, boost_status
):
    """`/v1/error` says nothing about what its association id identifies,
    so only an id that is literally one of this unit's port numbers marks
    a room. Everything else is counted against the unit.
    """
    errors = [
        api_mod.DeviceError(
            code="E042",
            time="2026-01-15T08:30:00Z",
            description="on port 2",
            association_id="2",
            severity="critical",
            category="Fan and main PCB",
        ),
        api_mod.DeviceError(
            code="E043",
            time="2026-01-15T08:30:00Z",
            description="opaque",
            association_id="abc123",
            severity="warning",
            category="Power",
        ),
    ]

    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        errors=errors,
    )

    unit = build_layout(hass)["units"][0]
    faulted = {room["port"] for room in unit["rooms"] if room["error"]}

    assert faulted == {2}
    assert unit["unattributed_errors"] == 1


async def test_a_port_number_that_is_not_wired_is_not_attributed(
    hass, mock_api_client, v2_data, boost_status
):
    """A numeric association id is only a port if this unit actually has
    that port; otherwise it is as opaque as any other id.
    """
    errors = [
        api_mod.DeviceError(
            code="E042",
            time="2026-01-15T08:30:00Z",
            description="port 11",
            association_id="11",
            severity="critical",
            category="Power",
        )
    ]

    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
        errors=errors,
    )

    unit = build_layout(hass)["units"][0]

    assert not any(room["error"] for room in unit["rooms"])
    assert unit["unattributed_errors"] == 1


def test_card_module_draws_branch_labels_and_fault_art():
    """Branches are numbered with Renson's dot notation, and a faulty one
    gets the error drawing rather than the plain connection.
    """
    module = build_card_module()

    assert "${port}.${i + 1}" in module
    assert "valve_manual_error" in module


def test_everything_but_the_number_lives_in_the_hover_panel():
    """The drawing carries only the badges, so a split outlet stays
    readable however many branches it has. Name, pictogram and readings
    are shown on hover.

    An HTML panel rather than an SVG <title>, because a native tooltip
    cannot draw the room's pictogram.
    """
    module = build_card_module()

    assert "hb3-tip" in module
    assert "<ha-icon" in module
    assert 'addEventListener("mouseenter"' in module
    assert 'addEventListener("mouseleave"' in module
    # Keyboard reachable, and the panel follows focus as well as the mouse.
    assert 'tabindex="0"' in module
    assert 'addEventListener("focus"' in module


def test_hover_panel_is_positioned_without_measuring_the_dom():
    """Placement is a percentage of the viewport, which maps exactly onto
    the rendered drawing because the SVG scales to the card's width and
    keeps its aspect. Nothing to measure at runtime, so nothing to get
    wrong on a card that is still laying out.
    """
    module = build_card_module()

    assert "((bx - view[0]) / view[2]) * 100" in module
    assert "((by - view[1]) / view[3]) * 100" in module
    assert "data-left=" in module and "data-top=" in module


def test_hover_panel_uses_css_anchor_positioning_where_available():
    """A percentage alone cannot keep the panel inside the card: an outlet
    near an edge would push it out. CSS anchor positioning lets the
    browser flip it to the other side instead.

    Detected rather than assumed - it is recent enough that a Home
    Assistant user may be on a browser without it - with the percentage
    placement kept as the fallback. Both paths were exercised by running
    the module with CSS.supports stubbed either way.
    """
    module = build_card_module()

    assert 'CSS.supports("position-area", "block-start")' in module
    assert "position-anchor:--hb3-anchor" in module
    assert "anchor-name:--hb3-anchor" in module
    # Flip on either axis, and on both at once for a corner.
    assert "flip-block,flip-inline,flip-block flip-inline" in module
    # The fallback is still there and still needs no measuring.
    assert "translate(-50%%,-115%%)" in module.replace("%", "%%")
    assert "const target = ANCHORED ? anchor : tip" in module


def test_card_module_chains_branches_outward():
    """A split outlet's branches run end to end away from the unit, which
    is how the Renson installer app draws them - not fanned out along the
    edge, which is what this card did first.

    The placement itself was checked by running the module and rasterising
    the result against that app's own screen; what is pinned here is that
    both the artwork and its badge take the same branch offset, since one
    without the other puts the number on the wrong bracket.
    """
    module = build_card_module()

    assert "const outward = (side) =>" in module
    assert "const place = (port, art, branch = 0)" in module
    assert "const badgeAt = (port, branch = 0)" in module
    assert "outward(side) * branch" in module


def test_card_module_sizes_its_own_viewport():
    """A chain grows the drawing in whichever direction it runs, so the
    viewport is computed from the layout rather than fixed.

    It was fixed at first, and two branches on the bottom edge already
    overflowed it - the drawing was silently cut off. Two mistakes were
    made fixing it, both caught by rasterising: reserving a text *width*
    above a top outlet (a band of empty space), and measuring only the
    wired ports, which cut the blanking caps off the other edges.
    """
    module = build_card_module()

    assert 'viewBox="${view.join(" ")}"' in module
    # Every position draws something, wired or capped.
    assert "G.base_x - G.valve_side_w" in module
    assert "G.base_y + G.base_size + G.valve_end_h" in module
    # Opposite sides get the same margin, so the unit sits in the middle
    # of the card however lopsided the installation is.
    assert "const dx = Math.max(left, right) + margin" in module
    assert "const dy = Math.max(top, bottom) + margin" in module


async def test_layout_offers_the_units_own_readings(
    hass, mock_api_client, v2_data, boost_status
):
    """The card puts the whole-house ventilation level in its header, so
    the unit needs entity ids of its own - the rooms' are not enough.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v2_data.serial,
        healthbox_data=v2_data,
        boost_status=boost_status,
    )

    entities = build_layout(hass)["units"][0]["entities"]

    assert entities["ventilation_level"].startswith("sensor.")
    assert entities["airflow"].startswith("sensor.")


async def test_unit_readings_are_absent_without_an_api_key(
    hass, mock_api_client, v1_data, boost_status
):
    """Both readings are gated on privileged access, so a v1-only unit
    simply has neither - and the header falls back to the plain name.
    """
    await setup_integration(
        hass,
        mock_api_client,
        serial=v1_data.serial,
        api_key=None,
        healthbox_data=v1_data,
        boost_status=boost_status,
    )

    assert build_layout(hass)["units"][0]["entities"] == {}


def test_card_header_carries_the_ventilation_level():
    """Appended to the name rather than replacing it, and dropped whole
    when the reading is missing or not a number - a header reading
    "Healthbox 3.0 · NaN%" would be worse than no figure.
    """
    module = build_card_module()

    assert "_header(unit)" in module
    assert "Number.isFinite(value)" in module
    assert "return unit.name;" in module


def test_layout_is_empty_before_any_unit_is_set_up(hass):
    """The view answers on a bare install too, rather than raising."""
    assert build_layout(hass) == {"units": []}


def test_card_module_defines_the_element_and_advertises_it():
    """Home Assistant only offers a card in the picker if it is pushed
    onto window.customCards.
    """
    module = build_card_module()

    assert 'customElements.define("healthbox-card"' in module
    assert "window.customCards" in module
    assert LAYOUT_URL.removeprefix("/api/") in module


def test_card_module_embeds_the_artwork_and_geometry_as_valid_json():
    """Both are inlined; a quoting slip would break the whole module
    rather than one drawing, so the literals are parsed back here.
    """
    module = build_card_module()

    assets = json.loads(module.split("const ASSETS = ", 1)[1].split(";\n", 1)[0])
    geometry = json.loads(module.split("const G = ", 1)[1].split(";\n", 1)[0])

    assert assets == SCENE_ASSETS
    assert geometry == SCENE_GEOMETRY


def test_scene_geometry_matches_the_app_layout():
    """Read from the app's decoded `view_health_box.xml`. Pinned so a
    change to the drawing has to be deliberate.
    """
    assert SCENE_GEOMETRY["stage"] == 540
    assert (SCENE_GEOMETRY["base_x"], SCENE_GEOMETRY["base_y"]) == (195, 195)
    assert SCENE_GEOMETRY["base_size"] == 150
    assert (SCENE_GEOMETRY["exhaust_x"], SCENE_GEOMETRY["exhaust_y"]) == (278, 165)


def test_every_drawing_the_card_asks_for_is_present():
    """The card composes asset names at runtime (`valve_manual` + side),
    so a missing one is a blank outlet rather than an error.
    """
    for art in ("valve_manual", "valve_closed", "valve_manual_error"):
        for side in ("left", "top"):
            assert f"{art}_{side}" in SCENE_ASSETS
    assert "healthbox_base" in SCENE_ASSETS
    assert "healthbox_exhaust" in SCENE_ASSETS


def test_card_and_layout_urls_are_namespaced_to_this_integration():
    assert CARD_URL.startswith("/healthbox3/")
    assert LAYOUT_URL.startswith("/api/healthbox3/")


def test_room_symbol_helper_is_what_the_layout_uses(v2_data):
    """Guards the layout against drifting from the sensor: both must read
    the same field, or the card and the entity would disagree.
    """
    room = next(r for r in v2_data.rooms if r.id == 3)
    assert api_mod.room_symbol(room) == "StudioFlat"
