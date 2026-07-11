"""Conductor switches: enabled / mute / tv_solo / keep_grouped."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .controller import ConductorEntity, SonosConductorController
from .core.events import Event, SetEnabled, SetKeepGrouped, SetMute, SetTvSolo
from .core.model import EngineState


@dataclass(frozen=True, slots=True)
class ConductorSwitchDescription:
    """Maps a switch to its engine-state reader and command event."""

    key: str
    name: str
    icon: str
    entity_category: EntityCategory | None
    is_on_fn: Callable[[EngineState], bool]
    event_fn: Callable[[bool], Event]


SWITCHES: tuple[ConductorSwitchDescription, ...] = (
    ConductorSwitchDescription(
        key="enabled",
        name="Enabled",
        icon="mdi:play-circle",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda state: state.enabled,
        event_fn=SetEnabled,
    ),
    ConductorSwitchDescription(
        key="mute",
        name="Mute",
        icon="mdi:volume-mute",
        entity_category=None,
        is_on_fn=lambda state: state.muted,
        event_fn=lambda on: SetMute(on, source="switch"),
    ),
    ConductorSwitchDescription(
        key="tv_solo",
        name="TV solo",
        icon="mdi:television-speaker",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda state: state.tv_solo,
        event_fn=SetTvSolo,
    ),
    ConductorSwitchDescription(
        key="keep_grouped",
        name="Keep grouped",
        icon="mdi:link-variant",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda state: state.keep_grouped,
        event_fn=SetKeepGrouped,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the conductor switches."""
    controller: SonosConductorController | None = hass.data[DOMAIN][entry.entry_id]
    if controller is None:
        return
    async_add_entities(SonosConductorSwitch(controller, description) for description in SWITCHES)


class SonosConductorSwitch(ConductorEntity, SwitchEntity):
    """A boolean engine flag exposed as a switch."""

    def __init__(
        self, controller: SonosConductorController, description: ConductorSwitchDescription
    ) -> None:
        super().__init__(controller)
        self._description = description
        self._attr_unique_id = f"{controller.entry.entry_id}_{description.key}"
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_entity_category = description.entity_category

    @property
    def is_on(self) -> bool:
        return self._description.is_on_fn(self.engine_state)

    async def async_turn_on(self, **kwargs: object) -> None:
        self.controller.submit(self._description.event_fn(True))

    async def async_turn_off(self, **kwargs: object) -> None:
        self.controller.submit(self._description.event_fn(False))
