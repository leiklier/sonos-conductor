"""Per-zone audibility sensors (replace the legacy *_audio_zone helpers).

``on`` mirrors the engine's zone phase (ACTIVE/RELEASING). The attributes
additionally derive audibility and room scale from the engine's published
state (``EngineState.suppressed``, rule 6.2) so dashboards can see the
effective target without re-implementing suppression.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .controller import ConductorEntity, SonosConductorController
from .core.model import ConductorConfig, EngineState, ZoneConfig, ZonePhase
from .core.volume_math import room_scale, speaker_target

AUDIBLE_PHASES = (ZonePhase.ACTIVE, ZonePhase.RELEASING)


def _is_audible(state: EngineState, zone: ZoneConfig) -> bool:
    """The engine's audibility rule over its published state."""
    zone_state = state.zones.get(zone.zone_id)
    return (
        zone_state is not None
        and zone_state.phase in AUDIBLE_PHASES
        and zone.zone_id not in state.suppressed
    )


def _room_scale_for(config: ConductorConfig, state: EngineState, room_id: str) -> float:
    audible = [z for z in config.zones_in_room(room_id) if _is_audible(state, z)]
    tv_active = any(state.zones[z.zone_id].tv_playing for z in audible)
    return room_scale(len(audible), tv_active)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up one audibility sensor per zone."""
    controller: SonosConductorController | None = hass.data[DOMAIN][entry.entry_id]
    if controller is None:
        return
    async_add_entities(
        SonosConductorZoneSensor(controller, zone) for zone in controller.config.zones
    )


class SonosConductorZoneSensor(ConductorEntity, BinarySensorEntity):
    """Is this zone audible?"""

    _attr_device_class = BinarySensorDeviceClass.SOUND
    _attr_translation_key = "zone"

    def __init__(self, controller: SonosConductorController, zone: ZoneConfig) -> None:
        super().__init__(controller)
        self._zone = zone
        self._attr_unique_id = f"{controller.entry.entry_id}_zone_{zone.zone_id}"
        self._attr_name = f"Zone {zone.name}"

    @property
    def is_on(self) -> bool:
        zone_state = self.engine_state.zones.get(self._zone.zone_id)
        return zone_state is not None and zone_state.phase in AUDIBLE_PHASES

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.engine_state
        config = self.controller.config
        zone_state = state.zones.get(self._zone.zone_id)
        if zone_state is None:
            return {"room": self._zone.room_id}
        scale = _room_scale_for(config, state, self._zone.room_id)
        trim = config.speaker(self._zone.speaker_id).trim
        audible = _is_audible(state, self._zone)
        return {
            "phase": str(zone_state.phase),
            "occupied": zone_state.occupied,
            "tv_playing": zone_state.tv_playing,
            "room": self._zone.room_id,
            "room_scale": scale,
            "target_volume": speaker_target(state.master, trim, scale) if audible else 0.0,
        }
