"""Structural checks tying strings.json and every translations/*.json
file together.

Not entity-behavior tests - guard against the files silently drifting
apart (a new key added to one but not the others), which nothing else
in the test suite would catch.

Translation status: Dutch (nl.json) has been reviewed by a native
speaker. French (fr.json) started as a best-effort translation and has
since been checked against the wording of Renson's own French app,
which is the closest thing to an authority there is for this vocabulary
- the terms a user already reads on their phone. Corrections from a
native speaker are still welcome; the parts with no counterpart in the
app remain best-effort.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from custom_components.healthbox3 import api as api_mod
from custom_components.healthbox3.api import AQI_QUALIFICATION_LEVELS
from custom_components.healthbox3.const import PROFILES

_HEALTHBOX3_DIR = Path(__file__).parent.parent / "custom_components" / "healthbox3"
STRINGS_PATH = _HEALTHBOX3_DIR / "strings.json"
TRANSLATIONS_DIR = _HEALTHBOX3_DIR / "translations"
TRANSLATIONS_EN_PATH = TRANSLATIONS_DIR / "en.json"
TRANSLATIONS_NL_PATH = TRANSLATIONS_DIR / "nl.json"
TRANSLATIONS_FR_PATH = TRANSLATIONS_DIR / "fr.json"

# Every file that ships, so a language added on disk without being wired
# into the checks below fails rather than drifting unnoticed.
TRANSLATION_PATHS = (
    TRANSLATIONS_EN_PATH,
    TRANSLATIONS_NL_PATH,
    TRANSLATIONS_FR_PATH,
)


def test_strings_and_translations_en_are_byte_identical():
    """strings.json is what HA validates against the integration's own
    schema; translations/en.json is what actually ships to English-
    speaking users at runtime. These two are meant to always be
    identical - a change to one without the other silently ships stale
    or missing English text, with nothing else in the test suite (or
    hassfest) catching it.
    """
    assert STRINGS_PATH.read_text() == TRANSLATIONS_EN_PATH.read_text()


def _key_shape(data: Any) -> Any:
    """Return the structural shape of a translations dict - same keys and
    nesting as the input, with every leaf value collapsed to None so only
    the structure (which keys exist, not what they say) is compared.
    """
    if isinstance(data, dict):
        return {key: _key_shape(value) for key, value in data.items()}
    return None


def _load_json(path: Path) -> Any:
    with path.open() as f:
        return json.load(f)


def test_translations_nl_matches_en_key_structure():
    """nl.json's actual text is necessarily different from en.json's -
    only the shape (every key present, same nesting, nothing extra or
    missing) is expected to match.
    """
    assert _key_shape(_load_json(TRANSLATIONS_NL_PATH)) == _key_shape(
        _load_json(TRANSLATIONS_EN_PATH)
    )


def test_translations_fr_matches_en_key_structure():
    """Same structural check as Dutch - see this module's docstring for
    fr.json's translation-quality caveat, which this test doesn't (and
    can't) verify.
    """
    assert _key_shape(_load_json(TRANSLATIONS_FR_PATH)) == _key_shape(
        _load_json(TRANSLATIONS_EN_PATH)
    )


def test_every_translation_file_is_checked():
    """A language dropped into translations/ is shipped by Home Assistant
    whether or not any test looks at it. This ties the checks above to
    what is actually on disk, so a language cannot arrive - or be dropped
    again - without the checks following it.
    """
    assert set(TRANSLATIONS_DIR.glob("*.json")) == set(TRANSLATION_PATHS)


def _aqi_band_label_sites(data: Any) -> dict[str, dict[str, str]]:
    """Return every place a translations file names the AQI qualification
    bands, keyed by a readable site name.

    The same band is named in four places per file: as the `qualification`
    attribute of each of the two numeric AQI sensors, and as the state of
    each of the two `AQI level` sensors.
    """
    sensors = data["entity"]["sensor"]
    return {
        "room_aqi.qualification": sensors["room_aqi"]["state_attributes"][
            "qualification"
        ]["state"],
        "global_aqi.qualification": sensors["global_aqi"]["state_attributes"][
            "qualification"
        ]["state"],
        "room_aqi_level": sensors["room_aqi_level"]["state"],
        "global_aqi_level": sensors["global_aqi_level"]["state"],
    }


def test_every_aqi_band_is_named_once_per_language():
    """One band, one word - in every language, everywhere it appears.

    The four sites are the same scale seen from different entities, so a
    user reading "Moyen" on a dashboard tile and "Modéré" in the
    attributes of the sensor feeding it is looking at one band wearing
    two names. That is exactly what shipped once already, when only the
    `AQI level` sensors were re-worded to match Renson's app.

    Keys are derived from AQI_QUALIFICATION_LEVELS, so a band added in
    code without a label fails here rather than showing its raw key.
    """
    for path in (STRINGS_PATH, *TRANSLATION_PATHS):
        sites = _aqi_band_label_sites(_load_json(path))
        reference_name, reference = next(iter(sites.items()))
        assert set(reference) == set(AQI_QUALIFICATION_LEVELS), path.name
        for name, labels in sites.items():
            assert labels == reference, f"{path.name}: {name} != {reference_name}"


def test_every_select_option_is_translated():
    """A select whose options carry no translation shows the device's own
    raw values - a French user reading "health" where the Renson app says
    "Santé". Nothing else catches it: the entity works perfectly, it just
    speaks the wrong language.

    Derived from PROFILES rather than a literal, so a profile added in
    code without a matching label fails here.
    """
    strings = _load_json(STRINGS_PATH)
    options = strings["entity"]["select"]["room_profile"]["state"]

    assert set(options) == set(PROFILES)
    assert all(label and not label.islower() for label in options.values())


def test_every_documented_error_code_has_a_title_in_every_language():
    """Renson documents sixteen error codes, each with its own one-line
    description of the fault. Each gets its own repair-issue key, so the
    issue's title is that description rather than the generic "Healthbox
    reported an error".

    Checked against the category table rather than a second list of
    codes: `error_issue_key` derives the key from that same table, so a
    prefix added there without a title here would silently raise issues
    under a translation key that does not exist - which Home Assistant
    renders as the raw key.
    """
    for path in (STRINGS_PATH, *TRANSLATION_PATHS):
        issues = _load_json(path)["issues"]
        for prefix in api_mod._ERROR_CATEGORIES:
            key = api_mod.error_issue_key(f"{prefix}99")
            assert key in issues, f"{path.name} is missing {key}"
            assert issues[key]["title"].strip(), f"{path.name}: {key} has no title"


def test_an_undocumented_error_code_falls_back_to_the_generic_issue():
    """A code outside the sixteen still raises an issue - one that says
    only what the device said, which is better than a missing title.
    """
    assert api_mod.error_issue_key("99999") == "device_error"
    assert "device_error" in _load_json(STRINGS_PATH)["issues"]


CATALOGUE_PATH = Path(__file__).parent.parent / "docs" / "renson-error-catalogue.json"

_CATALOGUE_LANGUAGES = {
    "en": TRANSLATIONS_EN_PATH,
    "fr": TRANSLATIONS_FR_PATH,
    "nl": TRANSLATIONS_NL_PATH,
}


def test_shipped_error_titles_match_rensons_catalogue_verbatim():
    """docs/renson-error-catalogue.json is the source of record for both
    the category table and the shipped issue titles, and this is what
    makes that claim checkable rather than decorative.

    Verbatim on purpose: these are Renson's own translations of Renson's
    own error catalogue. An edit here - a reworded French title, a
    "helpful" clarification - would be this project inventing wording for
    a fault it has never seen, in a language it does not vouch for.
    """
    catalogue = _load_json(CATALOGUE_PATH)["errors"]
    assert len(catalogue) == len(api_mod._ERROR_CATEGORIES)

    for entry in catalogue:
        code = entry["code"]
        assert api_mod._categorize_error_code(code).casefold() == (
            entry["subsystem"].casefold()
        ), f"{code}: category disagrees with the catalogue"

        key = api_mod.error_issue_key(code)
        for language, path in _CATALOGUE_LANGUAGES.items():
            shipped = _load_json(path)["issues"][key]["title"]
            assert shipped == entry["titles"][language], (
                f"{code}: {path.name} title differs from the catalogue"
            )
