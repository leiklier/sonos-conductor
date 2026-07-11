"""Tests for registry-driven discovery.

The registries are staged to mirror the real installation described in
docs/LEGACY_BEHAVIOR.md: three Sonos speakers (a dockable Move in Kjøkken, an
Era 100 in Spisebord, an Arc in Sofakrok), Apollo MSR-2 occupancy sensors,
area-less template occupancy helpers, an LG TV + Apple TV in Sofakrok, an
entrance door sensor, and Music Assistant duplicate media players that must
never be discovered.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sonos_conductor import discovery

MOVE = "media_player.kjokken_sonos_move"
ERA = "media_player.spisebord_sonos"
ARC = "media_player.sofakrok_sonos"
MOVE_DOCK = "binary_sensor.kjokken_sonos_move_lader"


async def build_installation(hass: HomeAssistant) -> dict[str, str]:
    """Stage registries + states like the real installation. Returns area ids."""
    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    areas = {
        name: area_reg.async_create(name).id
        for name in ("Kjøkken", "Spisebord", "Sofakrok", "Gang")
    }

    config_entries = {}
    for domain in ("sonos", "music_assistant", "webostv", "apple_tv", "esphome", "zha"):
        entry = MockConfigEntry(domain=domain)
        entry.add_to_hass(hass)
        config_entries[domain] = entry

    def add_device(domain: str, identifier: str, name: str, area: str | None) -> dr.DeviceEntry:
        device = dev_reg.async_get_or_create(
            config_entry_id=config_entries[domain].entry_id,
            identifiers={(domain, identifier)},
            name=name,
        )
        if area:
            device = dev_reg.async_update_device(device.id, area_id=areas[area])
        return device

    def add_entity(
        domain: str,
        platform: str,
        object_id: str,
        *,
        device: dr.DeviceEntry | None = None,
        device_class: str | None = None,
        name: str | None = None,
        area: str | None = None,
        disabled: bool = False,
    ) -> er.RegistryEntry:
        entry = ent_reg.async_get_or_create(
            domain,
            platform,
            f"{platform}_{object_id}",
            suggested_object_id=object_id,
            device_id=device.id if device else None,
            original_device_class=device_class,
            original_name=name,
            disabled_by=er.RegistryEntryDisabler.USER if disabled else None,
        )
        if area:
            entry = ent_reg.async_update_entity(entry.entity_id, area_id=areas[area])
        return entry

    # --- Sonos speakers -----------------------------------------------------
    move = add_device("sonos", "move", "Kjøkken Sonos Move", "Kjøkken")
    add_entity(
        "media_player", "sonos", "kjokken_sonos_move", device=move, name="Kjøkken Sonos Move"
    )
    add_entity(
        "binary_sensor",
        "sonos",
        "kjokken_sonos_move_lader",
        device=move,
        device_class="battery_charging",
        name="Kjøkken Sonos Move Lader",
    )

    era = add_device("sonos", "era", "Spisebord Sonos", "Spisebord")
    add_entity("media_player", "sonos", "spisebord_sonos", device=era, name="Spisebord Sonos")

    arc = add_device("sonos", "arc", "Sofakrok Sonos", "Sofakrok")
    add_entity("media_player", "sonos", "sofakrok_sonos", device=arc, name="Sofakrok Sonos")

    # Disabled Sonos speaker: must not be discovered.
    add_entity("media_player", "sonos", "kontor_sonos", name="Kontor Sonos", disabled=True)

    # Music Assistant mirror in the same area: must NOT be discovered.
    add_entity(
        "media_player",
        "music_assistant",
        "sofakrok_sonos_2",
        name="Sofakrok Sonos",
        area="Sofakrok",
    )

    # Group player over all speakers: neither speaker nor TV.
    add_entity(
        "media_player", "group", "all_sonos_speakers", name="All Sonos Speakers", area="Sofakrok"
    )

    # --- TVs in Sofakrok ----------------------------------------------------
    tv = add_device("webostv", "lg", "Sofakrok TV", "Sofakrok")
    add_entity("media_player", "webostv", "sofakrok_tv", device=tv, name="Sofakrok TV")
    atv = add_device("apple_tv", "atv", "Sofakrok Apple TV", "Sofakrok")
    add_entity(
        "media_player", "apple_tv", "sofakrok_apple_tv", device=atv, name="Sofakrok Apple TV"
    )

    # --- Occupancy ----------------------------------------------------------
    # Apollo MSR-2 in Kjøkken: matches BOTH the area/device-class path and the
    # entity-id heuristic (must be de-duplicated).
    apollo_k = add_device("esphome", "apollo_k", "Apollo MSR-2 Kjøkken", "Kjøkken")
    add_entity(
        "binary_sensor",
        "esphome",
        "apollo_msr_2_kjokken_occupancy",
        device=apollo_k,
        device_class="occupancy",
        name="Apollo MSR-2 Kjøkken Occupancy",
    )
    # Apollo MSR-2 in Spisebord: area/device-class path only (no "occupancy"
    # in the entity id, presence device class).
    apollo_s = add_device("esphome", "apollo_s", "Apollo MSR-2 Spisebord", "Spisebord")
    add_entity(
        "binary_sensor",
        "esphome",
        "apollo_msr_2_spisebord_radar",
        device=apollo_s,
        device_class="presence",
        name="Apollo MSR-2 Spisebord Radar",
    )
    # Area-less template helper in the registry: entity-id heuristic only.
    add_entity(
        "binary_sensor",
        "template",
        "kjokken_occupancy",
        device_class="occupancy",
        name="Kjøkken Occupancy",
    )
    # Area-less, registry-less template helpers (states only): entity-id
    # heuristic via the state machine.
    hass.states.async_set("binary_sensor.spisebord_occupancy", "off")
    hass.states.async_set("binary_sensor.sofakrok_occupancy", "off")

    # --- Duck input candidates ------------------------------------------------
    door = add_device("zha", "door", "Inngangsdør", "Gang")
    add_entity(
        "binary_sensor",
        "zha",
        "inngangsdor",
        device=door,
        device_class="door",
        name="Inngangsdør",
    )
    add_entity("binary_sensor", "zha", "kontor_vindu", device_class="window", name="Kontor Vindu")

    return areas


async def test_discover_speakers(hass: HomeAssistant) -> None:
    """Sonos players only, sorted, with names, areas and dock sensors."""
    areas = await build_installation(hass)
    speakers = discovery.discover_speakers(hass)

    assert [s.entity_id for s in speakers] == [MOVE, ARC, ERA]

    move, arc, era = speakers
    assert move.name == "Kjøkken Sonos Move"
    assert move.area_id == areas["Kjøkken"]
    assert move.area_name == "Kjøkken"
    assert move.dock_sensor == MOVE_DOCK  # dockable

    assert arc.area_name == "Sofakrok"
    assert arc.dock_sensor is None
    assert era.area_name == "Spisebord"
    assert era.dock_sensor is None


async def test_music_assistant_mirror_not_discovered(hass: HomeAssistant) -> None:
    """A music_assistant player in the same area is never a speaker."""
    await build_installation(hass)
    entity_ids = [s.entity_id for s in discovery.discover_speakers(hass)]

    assert "media_player.sofakrok_sonos_2" not in entity_ids
    # Disabled and group players are excluded too.
    assert "media_player.kontor_sonos" not in entity_ids
    assert "media_player.all_sonos_speakers" not in entity_ids


async def test_discover_speakers_empty(hass: HomeAssistant) -> None:
    """No registries staged -> nothing discovered."""
    assert discovery.discover_speakers(hass) == []


async def test_suggest_occupancy_area_and_heuristic(hass: HomeAssistant) -> None:
    """Area/device-class matches + entity-id heuristic, de-duplicated."""
    areas = await build_installation(hass)

    # Kjøkken: Apollo matches both paths (appears once) + area-less registry
    # template helper via the heuristic.
    assert discovery.suggest_occupancy(hass, areas["Kjøkken"], "Kjøkken") == [
        "binary_sensor.apollo_msr_2_kjokken_occupancy",
        "binary_sensor.kjokken_occupancy",
    ]

    # Spisebord: presence sensor via area + registry-less state via heuristic.
    assert discovery.suggest_occupancy(hass, areas["Spisebord"], "Spisebord") == [
        "binary_sensor.apollo_msr_2_spisebord_radar",
        "binary_sensor.spisebord_occupancy",
    ]

    # Sofakrok: no area-matched sensor; the area-less template helper is
    # found purely via the entity-id heuristic.
    assert discovery.suggest_occupancy(hass, areas["Sofakrok"], "Sofakrok") == [
        "binary_sensor.sofakrok_occupancy",
    ]


async def test_suggest_occupancy_no_area(hass: HomeAssistant) -> None:
    """Speakers without an area still get heuristic matches by name."""
    await build_installation(hass)
    assert discovery.suggest_occupancy(hass, None, "Sofakrok") == [
        "binary_sensor.sofakrok_occupancy"
    ]
    assert discovery.suggest_occupancy(hass, None, None) == []


async def test_suggest_tvs(hass: HomeAssistant) -> None:
    """Non-Sonos/MA/group media players in the area."""
    areas = await build_installation(hass)

    assert discovery.suggest_tvs(hass, areas["Sofakrok"]) == [
        "media_player.sofakrok_apple_tv",
        "media_player.sofakrok_tv",
    ]
    assert discovery.suggest_tvs(hass, areas["Kjøkken"]) == []
    assert discovery.suggest_tvs(hass, None) == []


async def test_suggest_duck_inputs(hass: HomeAssistant) -> None:
    """Door/opening/window/garage_door binary sensors, sorted."""
    await build_installation(hass)

    assert discovery.suggest_duck_inputs(hass) == [
        "binary_sensor.inngangsdor",
        "binary_sensor.kontor_vindu",
    ]


async def test_suggest_duck_inputs_from_states(hass: HomeAssistant) -> None:
    """Registry-less sensors are found via their state's device_class."""
    await build_installation(hass)
    hass.states.async_set("binary_sensor.garasjeport", "off", {"device_class": "garage_door"})

    assert "binary_sensor.garasjeport" in discovery.suggest_duck_inputs(hass)
