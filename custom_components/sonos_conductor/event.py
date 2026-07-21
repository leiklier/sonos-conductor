"""Remote-key event entity: HomeKit remote presses as automation triggers.

The HomeKit bridge re-fires every remote key it does not handle itself as a
``homekit_tv_remote_key_pressed`` bus event. The media player consumes those
(and acts on some — skip, play/pause, favorite stepping) and re-dispatches
each key name on the controller's ``remote_key_signal``; this entity records
them, so any key — most usefully the otherwise-unmapped info button — can
trigger an automation (e.g. TTS announcing what's playing).
"""

from __future__ import annotations

from typing import ClassVar

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .controller import ConductorEntity, SonosConductorController

#: Every key name the HomeKit bridge forwards as a bus event (its
#: ``REMOTE_KEYS`` table minus ``play_pause``, which the bridge maps straight
#: to the play/pause service and never re-fires).
REMOTE_KEYS = (
    "arrow_up",
    "arrow_down",
    "arrow_left",
    "arrow_right",
    "select",
    "back",
    "exit",
    "information",
    "previous_track",
    "next_track",
    "rewind",
    "fast_forward",
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the remote-key event entity."""
    controller: SonosConductorController | None = hass.data[DOMAIN][entry.entry_id]
    if controller is None:
        return
    async_add_entities([SonosConductorRemoteKeyEvent(controller)])


class SonosConductorRemoteKeyEvent(ConductorEntity, EventEntity):
    """Records HomeKit remote key presses aimed at the conductor."""

    _attr_translation_key = "remote_key"
    # Hardcoded English name like every sibling entity: keeps the generated
    # entity id stable (event.sonos_conductor_remote_key) on any HA language.
    _attr_name = "Remote key"
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types: ClassVar[list[str]] = list(REMOTE_KEYS)

    def __init__(self, controller: SonosConductorController) -> None:
        super().__init__(controller)
        self._attr_unique_id = f"{controller.entry.entry_id}_remote_key"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.controller.remote_key_signal, self._on_remote_key
            )
        )

    @callback
    def _on_remote_key(self, key: str) -> None:
        if key not in self._attr_event_types:
            return  # a key this HA version forwards but we do not know about
        self._trigger_event(key)
        self.async_write_ha_state()
