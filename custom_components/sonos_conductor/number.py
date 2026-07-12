"""Master volume + per-speaker trim numbers.

``number.<name>_master_volume`` is the drop-in replacement for the legacy
``input_number.master_sonos_volume`` (Hue Tap Dial and automations repoint
here). Trim numbers adjust per-speaker loudness compensation at runtime.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .controller import ConductorEntity, SonosConductorController
from .core.events import SetMaster, SetTrim
from .core.model import SpeakerConfig


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the master volume and trim numbers."""
    controller: SonosConductorController | None = hass.data[DOMAIN][entry.entry_id]
    if controller is None:
        return
    entities: list[NumberEntity] = [MasterVolumeNumber(controller)]
    entities.extend(
        SpeakerTrimNumber(controller, speaker) for speaker in controller.config.speakers
    )
    async_add_entities(entities)


class MasterVolumeNumber(ConductorEntity, NumberEntity):
    """The conductor's master volume (0..1)."""

    _attr_name = "Master volume"
    _attr_translation_key = "master_volume"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 1.0
    _attr_native_step = 0.01
    _attr_mode = NumberMode.SLIDER

    def __init__(self, controller: SonosConductorController) -> None:
        super().__init__(controller)
        self._attr_unique_id = f"{controller.entry.entry_id}_master"

    @property
    def native_value(self) -> float:
        return self.engine_state.master

    async def async_set_native_value(self, value: float) -> None:
        self.controller.submit(SetMaster(value, source="number"))


class SpeakerTrimNumber(ConductorEntity, NumberEntity):
    """Per-speaker loudness trim.

    Optimistic: the engine keeps its runtime trim shadow internally (spec
    §10.1) and does not expose it via ``EngineState``, so the entity tracks
    the last value it submitted, seeded from the configured trim.
    """

    _attr_translation_key = "trim"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0.5
    _attr_native_max_value = 2.0
    _attr_native_step = 0.05

    def __init__(self, controller: SonosConductorController, speaker: SpeakerConfig) -> None:
        super().__init__(controller)
        self._speaker = speaker
        self._attr_name = f"Trim {speaker.name}"
        self._attr_unique_id = f"{controller.entry.entry_id}_trim_{speaker.speaker_id}"
        self._attr_native_value = speaker.trim

    async def async_set_native_value(self, value: float) -> None:
        self.controller.submit(SetTrim(self._speaker.speaker_id, value))
        self._attr_native_value = value
        self.async_write_ha_state()
