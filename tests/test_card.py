"""Checks on the Healthbox card and the layout it is driven by.

The card's own drawing was verified separately, by running the module in
Node against a stub `hass` and rasterising the SVG it produced. What is
worth pinning here is the half that changes with the code: the layout the
integration hands it, and that the module it serves is well-formed.
"""

from __future__ import annotations

import copy
import json

from custom_components.healthbox3 import api as api_mod
from custom_components.healthbox3.card import (
    CARD_URL,
    LAYOUT_URL,
    build_card_module,
    build_layout,
)
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
    assert toilet["icon"] == "custom:renson-toilet"
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

    assert rooms[3]["icon"] == "custom:renson-bed"
    assert rooms[4]["icon"] == "custom:renson-living"


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


def test_card_module_draws_tooltips_and_branch_labels():
    """The two things that keep a split outlet readable: details move into
    an SVG <title>, and branches are numbered with Renson's dot notation.
    """
    module = build_card_module()

    assert "<title>" in module
    assert "${port}.${i + 1}" in module
    assert "valve_manual_error" in module


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
