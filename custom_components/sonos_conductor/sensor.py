"""Diagnostic sensor exposing an engine state snapshot."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .controller import ConductorEntity, SonosConductorController


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the diagnostics sensor."""
    controller: SonosConductorController | None = hass.data[DOMAIN][entry.entry_id]
    if controller is None:
        return
    async_add_entities([SonosConductorStateSensor(controller)])


class SonosConductorStateSensor(ConductorEntity, SensorEntity):
    """Engine state at a glance."""

    _attr_name = "State"
    _attr_icon = "mdi:tune"
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
            "tv_solo": state.tv_solo,
            "keep_grouped": state.keep_grouped,
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
