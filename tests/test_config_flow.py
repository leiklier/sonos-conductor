"""Tests for the Sonos Conductor config and options flows."""

from __future__ import annotations

import dataclasses
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sonos_conductor.const import DOMAIN
from custom_components.sonos_conductor.core.model import Tunables
from tests.test_discovery import (
    ARC,
    ERA,
    MOVE,
    MOVE_DOCK,
    PC_HOME,
    PC_KJOKKEN_OCC,
    add_presence_conductor,
    build_installation,
)

TUNABLE_DEFAULTS = {f.name: f.default for f in dataclasses.fields(Tunables)}

KJOKKEN_OCCUPANCY = [
    "binary_sensor.apollo_msr_2_kjokken_occupancy",
    "binary_sensor.kjokken_occupancy",
]
SOFAKROK_TVS = ["media_player.sofakrok_apple_tv", "media_player.sofakrok_tv"]
DOOR = "binary_sensor.inngangsdor"


def _suggested(result: dict[str, Any]) -> dict[str, Any]:
    """Extract the suggested values from a form result's schema."""
    values: dict[str, Any] = {}
    for key in result["data_schema"].schema:
        if isinstance(key, vol.Marker) and key.description:
            values[str(key)] = key.description.get("suggested_value")
    return values


def _base_options() -> dict[str, Any]:
    """Stored options mirroring the contract, incl. runtime-written keys."""
    return {
        "speakers": [
            {
                "entity_id": MOVE,
                "name": "Kjøkken Sonos Move",
                "trim": 1.2,
                "dock_sensor": MOVE_DOCK,
            },
            {"entity_id": ARC, "name": "Sofakrok Sonos", "trim": 1.0, "dock_sensor": None},
        ],
        "zones": [
            {
                "zone_id": "kjokken",
                "name": "Kjøkken",
                "speaker": MOVE,
                "room": "kjokken",
                "occupancy": ["binary_sensor.kjokken_occupancy"],
                "tvs": [],
                "hold_seconds": 60.0,
                "fallback": False,
            },
            {
                "zone_id": "sofakrok",
                "name": "Sofakrok",
                "speaker": ARC,
                "room": "stue",
                "occupancy": ["binary_sensor.sofakrok_occupancy"],
                "tvs": SOFAKROK_TVS,
                "hold_seconds": 15.0,
                "fallback": True,
            },
        ],
        "duck_inputs": [
            {
                "entity_id": DOOR,
                "name": "Inngangsdør",
                "duck_volume": 0.05,
                "engage_fade": 0.5,
                "release_fade": 2.0,
            }
        ],
        "primary_speaker": ARC,
        "tunables": dict(TUNABLE_DEFAULTS),
        "last_master": 0.42,
    }


async def _add_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Sonos Conductor",
        unique_id=DOMAIN,
        data={},
        options=_base_options(),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_full_happy_path(hass: HomeAssistant) -> None:
    """All steps with the real installation; exact resulting options."""
    await build_installation(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"speakers": [MOVE, ARC, ERA]}
    )

    # Zone 1/3: the Move. Discovery-driven suggestions are prefilled.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zone"
    assert result["description_placeholders"] == {
        "speaker": "Kjøkken Sonos Move",
        "zone_number": "1",
        "zone_count": "3",
    }
    suggested = _suggested(result)
    assert suggested["name"] == "Kjøkken"
    assert suggested["room"] == "Kjøkken"
    assert suggested["occupancy"] == KJOKKEN_OCCUPANCY
    assert suggested["tvs"] == []
    assert suggested["dock_sensor"] == MOVE_DOCK

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Kjøkken",
            "room": "kjokken",
            "occupancy": KJOKKEN_OCCUPANCY,
            "tvs": [],
            "hold_seconds": 60,
            "fallback": False,
            "trim": 1.2,
            "dock_sensor": MOVE_DOCK,
        },
    )

    # Zone 2/3: the Arc (sofakrok) — fallback zone with TVs.
    assert result["step_id"] == "zone"
    assert result["description_placeholders"]["zone_number"] == "2"
    assert _suggested(result)["tvs"] == SOFAKROK_TVS
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Sofakrok",
            "room": "Stue",  # slugified on save -> "stue"
            "occupancy": ["binary_sensor.sofakrok_occupancy"],
            "tvs": SOFAKROK_TVS,
            "hold_seconds": 15,
            "fallback": True,
            "trim": 1.0,
        },
    )

    # Zone 3/3: the Era (spisebord) — same acoustic room as sofakrok.
    assert result["step_id"] == "zone"
    assert result["description_placeholders"]["zone_number"] == "3"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Spisebord",
            "room": "stue",
            "occupancy": ["binary_sensor.spisebord_occupancy"],
            "tvs": [],
            "hold_seconds": 15,
            "fallback": False,
            "trim": 1.1,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "ducks"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"duck_entities": [DOOR], "duck_volume": 0.05, "release_fade": 2.0},
    )

    # No Presence Conductor installed: no home-presence suggestion.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "home"
    assert _suggested(result)["home_presence_entity"] is None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tunables"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(TUNABLE_DEFAULTS)
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Sonos Conductor"
    assert result["data"] == {}
    assert dict(result["result"].options) == {
        "speakers": [
            {
                "entity_id": MOVE,
                "name": "Kjøkken Sonos Move",
                "trim": 1.2,
                "dock_sensor": MOVE_DOCK,
            },
            {"entity_id": ARC, "name": "Sofakrok Sonos", "trim": 1.0, "dock_sensor": None},
            {"entity_id": ERA, "name": "Spisebord Sonos", "trim": 1.1, "dock_sensor": None},
        ],
        "zones": [
            {
                "zone_id": "kjokken",
                "name": "Kjøkken",
                "speaker": MOVE,
                "room": "kjokken",
                "presence_entity": None,
                "occupancy": KJOKKEN_OCCUPANCY,
                "tvs": [],
                "hold_seconds": 60.0,
                "fallback": False,
            },
            {
                "zone_id": "sofakrok",
                "name": "Sofakrok",
                "speaker": ARC,
                "room": "stue",
                "presence_entity": None,
                "occupancy": ["binary_sensor.sofakrok_occupancy"],
                "tvs": SOFAKROK_TVS,
                "hold_seconds": 15.0,
                "fallback": True,
            },
            {
                "zone_id": "spisebord",
                "name": "Spisebord",
                "speaker": ERA,
                "room": "stue",
                "presence_entity": None,
                "occupancy": ["binary_sensor.spisebord_occupancy"],
                "tvs": [],
                "hold_seconds": 15.0,
                "fallback": False,
            },
        ],
        "duck_inputs": [
            {
                "entity_id": DOOR,
                "name": "Inngangsdør",
                "duck_volume": 0.05,
                "engage_fade": 0.0,
                "release_fade": 2.0,
            }
        ],
        "primary_speaker": ARC,  # the fallback zone's speaker
        "home_presence_entity": None,
        "tunables": TUNABLE_DEFAULTS,
    }


async def test_presence_conductor_prioritized_in_setup(hass: HomeAssistant) -> None:
    """With Presence Conductor installed, its room is the suggested presence
    source, plain occupancy suggestions are suppressed, and the home step
    offers the anyone-home sensor."""
    areas = await build_installation(hass)
    await add_presence_conductor(hass, areas)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"speakers": [MOVE]})

    assert result["step_id"] == "zone"
    suggested = _suggested(result)
    assert suggested["presence_entity"] == PC_KJOKKEN_OCC
    assert suggested["occupancy"] == []

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Kjøkken",
            "room": "kjokken",
            "presence_entity": PC_KJOKKEN_OCC,
            "occupancy": [],
            "tvs": [],
            "hold_seconds": 60,
            "fallback": True,
            "trim": 1.2,
            "dock_sensor": MOVE_DOCK,
        },
    )

    assert result["step_id"] == "ducks"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"duck_entities": [], "duck_volume": 0.05, "release_fade": 2.0}
    )

    assert result["step_id"] == "home"
    assert _suggested(result)["home_presence_entity"] == PC_HOME
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"home_presence_entity": PC_HOME}
    )

    assert result["step_id"] == "tunables"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(TUNABLE_DEFAULTS)
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = dict(result["result"].options)
    assert options["zones"][0]["presence_entity"] == PC_KJOKKEN_OCC
    assert options["zones"][0]["occupancy"] == []
    assert options["home_presence_entity"] == PC_HOME


async def test_options_home_roundtrip(hass: HomeAssistant) -> None:
    """The home section stores the entity, suggests discovery for legacy
    entries, and preserves the stored choice (including clearing it)."""
    areas = await build_installation(hass)
    await add_presence_conductor(hass, areas)
    entry = await _add_entry(hass)  # pre-presence options: no home key

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "home"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "home"
    # Legacy entry (key absent): discovery fills the gap.
    assert _suggested(result)["home_presence_entity"] == PC_HOME

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"home_presence_entity": PC_HOME}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["home_presence_entity"] == PC_HOME
    assert entry.options["last_master"] == 0.42

    # Clearing sticks: the stored None wins over discovery.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "home"}
    )
    assert _suggested(result)["home_presence_entity"] == PC_HOME  # stored value
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert entry.options["home_presence_entity"] is None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "home"}
    )
    assert _suggested(result)["home_presence_entity"] is None


async def test_abort_no_speakers_found(hass: HomeAssistant) -> None:
    """No Sonos players in the registry -> abort."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_speakers_found"


async def test_abort_single_instance(hass: HomeAssistant) -> None:
    """A second flow aborts before discovery even runs."""
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_two_fallback_zones_rejected(hass: HomeAssistant) -> None:
    """Marking a second zone as fallback re-shows the form with an error."""
    await build_installation(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"speakers": [MOVE, ARC]}
    )

    zone_input = {
        "name": "Kjøkken",
        "room": "kjokken",
        "occupancy": [],
        "tvs": [],
        "hold_seconds": 60,
        "fallback": True,
        "trim": 1.2,
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], zone_input)
    assert result["step_id"] == "zone"
    assert result["description_placeholders"]["zone_number"] == "2"

    # Second fallback zone: rejected, still on zone 2.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Sofakrok",
            "room": "stue",
            "occupancy": [],
            "tvs": [],
            "hold_seconds": 15,
            "fallback": True,
            "trim": 1.0,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zone"
    assert result["errors"] == {"fallback": "multiple_fallback_zones"}
    assert result["description_placeholders"]["zone_number"] == "2"

    # Unchecking fallback lets the flow continue.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Sofakrok",
            "room": "stue",
            "occupancy": [],
            "tvs": [],
            "hold_seconds": 15,
            "fallback": False,
            "trim": 1.0,
        },
    )
    assert result["step_id"] == "ducks"


async def test_duplicate_zone_name_rejected(hass: HomeAssistant) -> None:
    """Zone names must slugify to unique zone ids."""
    await build_installation(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"speakers": [MOVE, ARC]}
    )
    zone_input = {
        "name": "Stue",
        "room": "stue",
        "occupancy": [],
        "tvs": [],
        "hold_seconds": 15,
        "fallback": False,
        "trim": 1.0,
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], zone_input)
    # "STUE" slugifies to the same zone_id as "Stue".
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**zone_input, "name": "STUE"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"name": "duplicate_zone_name"}


async def test_options_zones_roundtrip_preserves_last_master(hass: HomeAssistant) -> None:
    """Edit one zone's hold_seconds; everything else survives byte-for-byte."""
    entry = await _add_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zones"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zones"
    assert result["description_placeholders"] == {
        "zone": "Kjøkken",
        "zone_number": "1",
        "zone_count": "2",
    }
    # Defaults are the stored values, not discovery output.
    suggested = _suggested(result)
    assert suggested["hold_seconds"] == 60.0
    assert suggested["occupancy"] == ["binary_sensor.kjokken_occupancy"]
    assert suggested["trim"] == 1.2
    assert suggested["dock_sensor"] == MOVE_DOCK

    # Zone 1: change hold_seconds 60 -> 45, keep the rest.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "room": "kjokken",
            "occupancy": ["binary_sensor.kjokken_occupancy"],
            "tvs": [],
            "hold_seconds": 45,
            "fallback": False,
            "trim": 1.2,
            "dock_sensor": MOVE_DOCK,
        },
    )
    # Zone 2: keep everything.
    assert result["step_id"] == "zones"
    assert result["description_placeholders"]["zone"] == "Sofakrok"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "room": "stue",
            "occupancy": ["binary_sensor.sofakrok_occupancy"],
            "tvs": SOFAKROK_TVS,
            "hold_seconds": 15,
            "fallback": True,
            "trim": 1.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    expected = _base_options()
    expected["zones"][0]["hold_seconds"] = 45.0
    # The edit round-trips through the new schema: the (unset) presence
    # entity is materialized on every zone.
    for zone in expected["zones"]:
        zone["presence_entity"] = None
    assert dict(entry.options) == expected
    assert entry.options["last_master"] == 0.42


async def test_options_second_fallback_rejected(hass: HomeAssistant) -> None:
    """The options flow enforces the single-fallback rule too."""
    entry = await _add_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zones"}
    )
    # Zone 1 (kjokken) becomes fallback...
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "room": "kjokken",
            "occupancy": ["binary_sensor.kjokken_occupancy"],
            "tvs": [],
            "hold_seconds": 60,
            "fallback": True,
            "trim": 1.2,
            "dock_sensor": MOVE_DOCK,
        },
    )
    # ...so zone 2 (sofakrok) may not stay fallback.
    sofakrok_input = {
        "room": "stue",
        "occupancy": ["binary_sensor.sofakrok_occupancy"],
        "tvs": SOFAKROK_TVS,
        "hold_seconds": 15,
        "fallback": True,
        "trim": 1.0,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], sofakrok_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"fallback": "multiple_fallback_zones"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**sofakrok_input, "fallback": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The fallback moved, so the primary speaker follows it.
    assert entry.options["primary_speaker"] == MOVE
    assert entry.options["zones"][0]["fallback"] is True
    assert entry.options["zones"][1]["fallback"] is False
    assert entry.options["last_master"] == 0.42


async def test_options_ducks_roundtrip(hass: HomeAssistant) -> None:
    """Duck edits keep stored names/engage fades and preserve other keys."""
    await build_installation(hass)
    entry = await _add_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ducks"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "ducks"
    suggested = _suggested(result)
    assert suggested["duck_entities"] == [DOOR]
    assert suggested["duck_volume"] == 0.05

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "duck_entities": [DOOR, "binary_sensor.kontor_vindu"],
            "duck_volume": 0.1,
            "release_fade": 3.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["duck_inputs"] == [
        {
            "entity_id": DOOR,
            "name": "Inngangsdør",  # stored name kept
            "duck_volume": 0.1,
            "engage_fade": 0.5,  # stored engage_fade kept
            "release_fade": 3.0,
        },
        {
            "entity_id": "binary_sensor.kontor_vindu",
            "name": "Kontor Vindu",  # resolved from the registry
            "duck_volume": 0.1,
            "engage_fade": 0.0,
            "release_fade": 3.0,
        },
    ]
    assert entry.options["last_master"] == 0.42
    assert entry.options["zones"] == _base_options()["zones"]


async def test_options_tunables_roundtrip(hass: HomeAssistant) -> None:
    """Tunables are seeded from storage and merged back."""
    entry = await _add_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tunables"}
    )
    assert result["type"] is FlowResultType.FORM
    assert _suggested(result)["fade_out"] == 5.0

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**TUNABLE_DEFAULTS, "fade_out": 8.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["tunables"] == {**TUNABLE_DEFAULTS, "fade_out": 8.0}
    assert entry.options["last_master"] == 0.42
    assert entry.options["speakers"] == _base_options()["speakers"]


async def test_options_media_sources_roundtrip(hass: HomeAssistant) -> None:
    """The media section stores the source allowlist and preserves other keys."""
    entry = await _add_entry(hass)
    # The leader (ARC) currently offers these sources.
    hass.states.async_set(
        ARC,
        "playing",
        {"source_list": ["TV", "NRK P1", "NRK P3", "Discover Weekly"]},
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "media"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "media"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"homekit_sources": ["NRK P1", "NRK P3", "TV"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["homekit_sources"] == ["NRK P1", "NRK P3", "TV"]
    assert entry.options["last_master"] == 0.42
    assert entry.options["zones"] == _base_options()["zones"]

    # Clearing the selection reverts to mirroring everything.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "media"}
    )
    assert _suggested(result)["homekit_sources"] == ["NRK P1", "NRK P3", "TV"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"homekit_sources": []}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["homekit_sources"] == []
