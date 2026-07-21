"""Diagnostic sensors: engine state snapshot and per-speaker volumes."""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import ATTR_MEDIA_VOLUME_LEVEL
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .controller import ConductorEntity, SonosConductorController
from .core.model import SpeakerConfig


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the diagnostics sensor and the per-speaker volume sensors."""
    controller: SonosConductorController | None = hass.data[DOMAIN][entry.entry_id]
    if controller is None:
        return
    async_add_entities(
        [
            SonosConductorStateSensor(controller),
            *(SpeakerVolumeSensor(controller, speaker) for speaker in controller.config.speakers),
        ]
    )


class SonosConductorStateSensor(ConductorEntity, SensorEntity):
    """Engine state at a glance."""

    _attr_name = "State"
    _attr_translation_key = "state"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, controller: SonosConductorController) -> None:
        super().__init__(controller)
        self._attr_unique_id = f"{controller.entry.entry_id}_state"

    @property
    def native_value(self) -> str:
        return "enabled" if self.engine_state.enabled else "disabled"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.engine_state
        return {
            "master": state.master,
            "muted": state.muted,
            "tv_solo_mode": state.tv_solo_mode.value,
            "follow_mode": state.follow_mode.value,
            "idle_attenuation": state.idle_attenuation.value,
            "keep_grouped": state.keep_grouped,
            "night_mode": state.night_mode,
            "speakers": {
                speaker_id: {
                    "commanded": speaker.commanded,
                    "volume": speaker.volume,
                    "docked": speaker.docked,
                }
                for speaker_id, speaker in state.speakers.items()
            },
            "active_duck_inputs": [
                input_id for input_id, active in state.duck_active.items() if active
            ],
        }


class SpeakerVolumeSensor(ConductorEntity, SensorEntity):
    """A speaker's actual device volume as a read-only percentage.

    Mirrors the underlying ``media_player``'s ``volume_level`` directly, so
    it stays fresh through the conductor's own writes (which echo
    suppression hides from the engine's published state) as well as
    external changes. Useful for watching fades, profile changes and the
    idle beds.
    """

    _attr_translation_key = "speaker_volume"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, controller: SonosConductorController, speaker: SpeakerConfig) -> None:
        super().__init__(controller)
        self._speaker = speaker
        self._attr_name = f"Volume {speaker.name}"
        self._attr_unique_id = f"{controller.entry.entry_id}_volume_{speaker.speaker_id}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._speaker.speaker_id], self._on_speaker_state
            )
        )

    @callback
    def _on_speaker_state(self, _event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        state = self.hass.states.get(self._speaker.speaker_id)
        return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)

    @property
    def native_value(self) -> int | None:
        state = self.hass.states.get(self._speaker.speaker_id)
        if state is None:
            return None
        volume = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
        if volume is None:
            return None
        return round(volume * 100)
