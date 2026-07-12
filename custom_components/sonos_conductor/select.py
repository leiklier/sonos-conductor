"""Conductor selects: the TV-solo mode (off / same_room / tv_zone)."""

from __future__ import annotations

from typing import ClassVar

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .controller import ConductorEntity, SonosConductorController
from .core.events import SetTvSoloMode
from .core.model import TvSoloMode


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the conductor selects."""
    controller: SonosConductorController | None = hass.data[DOMAIN][entry.entry_id]
    if controller is None:
        return
    async_add_entities([SonosConductorTvSoloSelect(controller)])


class SonosConductorTvSoloSelect(ConductorEntity, SelectEntity, RestoreEntity):
    """The engine's TV-solo mode, restored across restarts.

    The engine seeds ``tv_solo_mode`` to OFF; if a valid mode was restored,
    it is pushed back through the controller queue as a ``SetTvSoloMode``
    event (drained after startup reconciliation, like any user command).
    An unknown or invalid restored state leaves the engine at OFF.
    """

    _attr_translation_key = "tv_solo"
    # Hardcoded English name like every sibling entity: keeps the generated
    # entity id stable (select.sonos_conductor_tv_solo) on any HA language.
    _attr_name = "TV solo"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options: ClassVar[list[str]] = [mode.value for mode in TvSoloMode]

    def __init__(self, controller: SonosConductorController) -> None:
        super().__init__(controller)
        self._attr_unique_id = f"{controller.entry.entry_id}_tv_solo"

    @property
    def current_option(self) -> str:
        return self.engine_state.tv_solo_mode.value

    async def async_select_option(self, option: str) -> None:
        self.controller.submit(SetTvSoloMode(TvSoloMode(option)))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state not in self._attr_options:
            return  # nothing restored (or invalid): keep the engine default
        mode = TvSoloMode(last.state)
        if mode is not self.engine_state.tv_solo_mode:
            self.controller.submit(SetTvSoloMode(mode))
