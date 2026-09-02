"""Per-speaker trim numbers.

The master volume and mute live on ``media_player.sonos_conductor`` (its
slider and mute button already drive the engine), so no separate entities
duplicate them. Trim numbers adjust per-speaker loudness compensation at
runtime.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .controller import ConductorEntity, SonosConductorController
from .core.events import SetTrim
from .core.model import SpeakerConfig

TRIM_MIN = 0.5
TRIM_MAX = 2.0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the per-speaker trim numbers."""
    controller: SonosConductorController | None = hass.data[DOMAIN][entry.entry_id]
    if controller is None:
        return
    async_add_entities(
        SpeakerTrimNumber(controller, speaker) for speaker in controller.config.speakers
    )


class SpeakerTrimNumber(ConductorEntity, NumberEntity, RestoreEntity):
    """Per-speaker loudness trim, read from ``EngineState.trims`` (spec §10.1).

    Restored across restarts: the engine seeds the trim from the configured
    value; a differing restored value is pushed back through the controller
    queue as ``SetTrim`` (the switch.py / select.py restore pattern), so a
    trim adjusted at runtime survives a reboot without editing the options.
    """

    _attr_translation_key = "trim"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = TRIM_MIN
    _attr_native_max_value = TRIM_MAX
    _attr_native_step = 0.05

    def __init__(self, controller: SonosConductorController, speaker: SpeakerConfig) -> None:
        super().__init__(controller)
        self._speaker = speaker
        self._attr_name = f"Trim {speaker.name}"
        self._attr_unique_id = f"{controller.entry.entry_id}_trim_{speaker.speaker_id}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        try:
            restored = float(last.state)
        except ValueError:
            return  # unknown/unavailable: keep the configured trim
        restored = max(TRIM_MIN, min(TRIM_MAX, restored))
        if abs(restored - self.native_value) > 1e-6:
            self.controller.submit(SetTrim(self._speaker.speaker_id, restored))

    @property
    def native_value(self) -> float:
        return self.engine_state.trims.get(self._speaker.speaker_id, self._speaker.trim)

    async def async_set_native_value(self, value: float) -> None:
        self.controller.submit(SetTrim(self._speaker.speaker_id, value))
