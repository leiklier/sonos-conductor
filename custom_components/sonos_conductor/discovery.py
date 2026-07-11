"""Registry-driven discovery for Sonos Conductor.

Pure read-only helpers over the entity/device/area registries and the state
machine. The config flow uses these to prefill its forms with sensible,
installation-specific defaults; nothing in here mutates anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

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
from homeassistant.util import slugify

#: Only media players from this platform are conductor speakers. Music
#: Assistant mirrors (and any other duplicate players) are excluded simply by
#: not being on this platform.
SONOS_PLATFORM = "sonos"

#: Platforms whose media players must never be suggested as zone TVs: Sonos
#: players are the speakers themselves; Music Assistant and group players
#: only mirror them.
NON_TV_PLATFORMS = ("sonos", "music_assistant", "group")

#: Binary-sensor device classes that indicate somebody is in an area.
OCCUPANCY_DEVICE_CLASSES = ("motion", "occupancy", "presence")

#: Binary-sensor device classes that make useful duck inputs.
DUCK_DEVICE_CLASSES = ("door", "opening", "window", "garage_door")

#: A battery-charging sensor on a speaker's device marks it dockable.
DOCK_DEVICE_CLASS = "battery_charging"


@dataclass(frozen=True, slots=True)
class DiscoveredSpeaker:
    """A Sonos media player found in the registries."""

    entity_id: str
    name: str
    area_id: str | None
    area_name: str | None
    #: Battery-charging binary sensor on the same device; None = not dockable.
    dock_sensor: str | None


def _device_class(entry: er.RegistryEntry) -> str | None:
    """User-set device class, falling back to the integration's original."""
    return entry.device_class or entry.original_device_class


def _usable(entry: er.RegistryEntry) -> bool:
    return entry.disabled_by is None and entry.hidden_by is None


def _effective_area_id(entry: er.RegistryEntry, dev_reg: dr.DeviceRegistry) -> str | None:
    """The entity's own area, falling back to its device's area."""
    if entry.area_id:
        return entry.area_id
    if entry.device_id and (device := dev_reg.async_get(entry.device_id)):
        return device.area_id
    return None


def _dock_sensor(ent_reg: er.EntityRegistry, device_id: str | None) -> str | None:
    """Battery-charging binary sensor on the same device, if any."""
    if device_id is None:
        return None
    for entry in er.async_entries_for_device(ent_reg, device_id):
        if entry.domain == "binary_sensor" and _device_class(entry) == DOCK_DEVICE_CLASS:
            return entry.entity_id
    return None


def discover_speakers(hass: HomeAssistant) -> list[DiscoveredSpeaker]:
    """All usable Sonos media players, with their area and dock sensor.

    Filtering on the entity registry's ``platform == "sonos"`` automatically
    excludes Music Assistant mirrors and every other non-Sonos player.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    speakers: list[DiscoveredSpeaker] = []
    for entry in list(ent_reg.entities.values()):
        if entry.domain != "media_player" or entry.platform != SONOS_PLATFORM:
            continue
        if not _usable(entry):
            continue
        device = dev_reg.async_get(entry.device_id) if entry.device_id else None
        name = entry.name or entry.original_name
        if not name and device:
            name = device.name_by_user or device.name
        area_id = entry.area_id or (device.area_id if device else None)
        area = area_reg.async_get_area(area_id) if area_id else None
        speakers.append(
            DiscoveredSpeaker(
                entity_id=entry.entity_id,
                name=name or entry.entity_id,
                area_id=area_id,
                area_name=area.name if area else None,
                dock_sensor=_dock_sensor(ent_reg, entry.device_id),
            )
        )
    speakers.sort(key=lambda speaker: speaker.entity_id)
    return speakers


def suggest_occupancy(hass: HomeAssistant, area_id: str | None, area_name: str | None) -> list[str]:
    """Occupancy-ish binary sensors for an area.

    Two complementary sources, de-duplicated and sorted:

    1. Registry entries with a motion/occupancy/presence device class whose
       entity (or device) sits in the given area.
    2. Entity ids matching ``binary_sensor.*{area_slug}*occupancy*`` — the
       fallback for area-less template helpers (checked against both the
       registry and the state machine, so registry-less template sensors are
       found too).
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    found: set[str] = set()

    for entry in list(ent_reg.entities.values()):
        if entry.domain != "binary_sensor" or not _usable(entry):
            continue
        if (
            area_id is not None
            and _device_class(entry) in OCCUPANCY_DEVICE_CLASSES
            and _effective_area_id(entry, dev_reg) == area_id
        ):
            found.add(entry.entity_id)

    if area_name:
        pattern = f"binary_sensor.*{slugify(area_name)}*occupancy*"
        candidates = set(hass.states.async_entity_ids("binary_sensor"))
        candidates.update(
            entry.entity_id
            for entry in list(ent_reg.entities.values())
            if entry.domain == "binary_sensor" and _usable(entry)
        )
        found.update(entity_id for entity_id in candidates if fnmatch(entity_id, pattern))

    return sorted(found)


def suggest_tvs(hass: HomeAssistant, area_id: str | None) -> list[str]:
    """Non-Sonos media players in the area (TVs, Apple TVs, receivers…)."""
    if area_id is None:
        return []
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    return sorted(
        entry.entity_id
        for entry in list(ent_reg.entities.values())
        if entry.domain == "media_player"
        and entry.platform not in NON_TV_PLATFORMS
        and _usable(entry)
        and _effective_area_id(entry, dev_reg) == area_id
    )


def suggest_duck_inputs(hass: HomeAssistant) -> list[str]:
    """Binary sensors that make natural duck inputs (doors, windows…)."""
    ent_reg = er.async_get(hass)
    found = {
        entry.entity_id
        for entry in list(ent_reg.entities.values())
        if entry.domain == "binary_sensor"
        and _usable(entry)
        and _device_class(entry) in DUCK_DEVICE_CLASSES
    }
    # Registry-less entities (e.g. YAML template sensors) via state attributes.
    found.update(
        state.entity_id
        for state in hass.states.async_all("binary_sensor")
        if state.attributes.get("device_class") in DUCK_DEVICE_CLASSES
    )
    return sorted(found)


def friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    """Best-effort display name for an entity."""
    if (state := hass.states.get(entity_id)) and state.name:
        return state.name
    if (entry := er.async_get(hass).async_get(entity_id)) and (
        name := entry.name or entry.original_name
    ):
        return name
    return entity_id.split(".", 1)[1].replace("_", " ")
