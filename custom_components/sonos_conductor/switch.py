"""Conductor switches: enabled / keep_grouped / night_mode.

Mute is not among them — it lives on ``media_player.sonos_conductor`` (its
mute button drives the engine directly), so no switch duplicates it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .controller import ConductorEntity, SonosConductorController
from .core.events import Event, SetEnabled, SetKeepGrouped, SetNightMode
from .core.model import EngineState


@dataclass(frozen=True, slots=True)
class ConductorSwitchDescription:
    """Maps a switch to its engine-state reader and command event.

    ``key`` doubles as the entity's ``translation_key``, wiring it to the
    state-aware icon defined for it in icons.json.
    """

    key: str
    name: str
    entity_category: EntityCategory | None
    is_on_fn: Callable[[EngineState], bool]
    event_fn: Callable[[bool], Event]
    #: Restore the last state across restarts (RestoreEntity). Only for
    #: flags nothing else re-establishes after a restart (night_mode).
    restore: bool = False


SWITCHES: tuple[ConductorSwitchDescription, ...] = (
    ConductorSwitchDescription(
        key="enabled",
        name="Enabled",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda state: state.enabled,
        event_fn=SetEnabled,
    ),
    ConductorSwitchDescription(
        key="keep_grouped",
        name="Keep grouped",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda state: state.keep_grouped,
        event_fn=SetKeepGrouped,
    ),
    ConductorSwitchDescription(
        key="night_mode",
        name="Night mode",
        entity_category=None,  # daily-use control, like mute
        is_on_fn=lambda state: state.night_mode,
        event_fn=SetNightMode,
        restore=True,  # no scheduler re-establishes it after a restart
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the conductor switches."""
    controller: SonosConductorController | None = hass.data[DOMAIN][entry.entry_id]
    if controller is None:
        return
    async_add_entities(
        (SonosConductorRestoreSwitch if description.restore else SonosConductorSwitch)(
            controller, description
        )
        for description in SWITCHES
    )


class SonosConductorSwitch(ConductorEntity, SwitchEntity):
    """A boolean engine flag exposed as a switch."""

    def __init__(
        self, controller: SonosConductorController, description: ConductorSwitchDescription
    ) -> None:
        super().__init__(controller)
        self._description = description
        self._attr_unique_id = f"{controller.entry.entry_id}_{description.key}"
        self._attr_name = description.name
        self._attr_translation_key = description.key
        self._attr_entity_category = description.entity_category

    @property
    def is_on(self) -> bool:
        return self._description.is_on_fn(self.engine_state)

    async def async_turn_on(self, **kwargs: object) -> None:
        self.controller.submit(self._description.event_fn(True))

    async def async_turn_off(self, **kwargs: object) -> None:
        self.controller.submit(self._description.event_fn(False))


class SonosConductorRestoreSwitch(SonosConductorSwitch, RestoreEntity):
    """An engine flag switch restored across restarts.

    The engine seeds the flag to its model default; a differing restored
    state is pushed back through the controller queue as the switch's
    command event (drained after startup reconciliation, like any user
    command — the select.py TV-solo pattern). Unknown/unavailable restored
    states leave the engine default untouched.
    """

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state not in (STATE_ON, STATE_OFF):
            return  # nothing restored (or invalid): keep the engine default
        active = last.state == STATE_ON
        if active != self._description.is_on_fn(self.engine_state):
            self.controller.submit(self._description.event_fn(active))
