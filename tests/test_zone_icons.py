"""Checks on the Renson zone pictogram set.

Whether Home Assistant actually paints the pictogram cannot be exercised
here; there is no frontend in the test harness. What can be checked is
everything leading up to it: the data is complete and well-formed, every
symbol resolves to an icon that exists, the JavaScript that registers the
set is what the frontend expects, and - the part that was wrong once and
that nothing noticed - the names handed to the frontend are addressable
by the set actually registered.
"""

from __future__ import annotations

import json
import re

from custom_components.healthbox3.icon_set import (
    ICON_SET_URL,
    build_module,
    icon_name,
)
from custom_components.healthbox3.zone_icons import (
    FALLBACK_ICON,
    ICON_PREFIX,
    ROOM_SYMBOL_TO_ICON,
    ZONE_ICON_PATHS,
)


def test_every_symbol_resolves_to_an_icon_that_exists():
    """A symbol pointing at a missing icon would render blank, and only
    ever on the one room unlucky enough to report it.
    """
    missing = {
        symbol: icon
        for symbol, icon in ROOM_SYMBOL_TO_ICON.items()
        if icon not in ZONE_ICON_PATHS
    }
    assert not missing


def test_icon_names_are_addressable_by_the_registered_set():
    """The name handed to the frontend has to reach the set registered.

    The frontend splits an icon name on its FIRST colon, looks the left
    half up in `window.customIconsets`, and calls it with the right half.
    So the name must be `<the set this module registers>:<a key in
    PATHS>`, and nothing else - `custom:renson-bath`, which this shipped
    for a while, asks a set called `custom` for an icon called
    `renson-bath` and gets neither.

    Nothing reports that failure: an unresolvable icon renders as empty
    space. This test is the only place it can be caught without a browser,
    so it reads the registered set name back out of the generated module
    rather than trusting the constant.
    """
    registered = re.search(r'window\.customIconsets\[("[^"]+")\]', build_module())
    assert registered, "the module does not register an icon set"
    set_name = json.loads(registered.group(1))

    for icon in ZONE_ICON_PATHS:
        name = icon_name(icon)
        prefix, _, rest = name.partition(":")
        assert prefix == set_name, name
        assert rest in ZONE_ICON_PATHS, name


def test_fallback_icon_exists():
    """Everything unrecognised lands here, so this one must never be missing."""
    assert FALLBACK_ICON in ZONE_ICON_PATHS


def test_icon_paths_look_like_svg_path_data():
    """Guards against a generator change emitting empty or truncated data."""
    for name, path in ZONE_ICON_PATHS.items():
        assert path, name
        assert path[0] in "Mm", f"{name} does not start with a moveto"
        assert re.fullmatch(r"[MmLlHhVvCcSsQqTtAaZz0-9eE,.\s+-]+", path), name


def test_symbol_vocabulary_is_the_one_read_from_the_device_library():
    """The 24 values the Renson app's own library carries.

    Pinned as a literal rather than derived: if a future firmware reports
    a value outside this set, the sensor falls back to the generic house,
    and this test is where that fact is recorded.
    """
    assert set(ROOM_SYMBOL_TO_ICON) == {
        "BabyRoom",
        "Basement",
        "BathRoom",
        "BathRoomWithoutToilet",
        "BedRoom",
        "ClosedKitchen",
        "CookerHood",
        "DiningRoom",
        "Dresser",
        "Garage",
        "Hallway",
        "HobbyRoom",
        "Kitchen",
        "LaundryRoom",
        "LivingRoom",
        "MediaRoom",
        "Office",
        "PlayRoom",
        "Shower",
        "SittingRoom",
        "Storage",
        "StudioFlat",
        "Toilet",
        "WashingRoom",
    }


def test_module_registers_the_icon_set_under_the_expected_prefix():
    """The contract with the frontend: a customIconsets entry keyed by
    prefix, resolving a name to an object carrying `path`.
    """
    module = build_module()

    assert "window.customIconsets" in module
    assert f'window.customIconsets[{json.dumps(ICON_PREFIX)}]' in module
    assert "path" in module


def test_module_embeds_every_icon_as_valid_json():
    """The paths are inlined; a quoting slip would break the whole module
    rather than one icon, so the embedded literal is parsed back here.
    """
    module = build_module()
    literal = module.split("const PATHS = ", 1)[1].split(";\n", 1)[0]
    embedded = json.loads(literal)

    assert embedded == ZONE_ICON_PATHS


def test_icon_set_url_is_namespaced_to_this_integration():
    assert ICON_SET_URL.startswith("/healthbox3/")
