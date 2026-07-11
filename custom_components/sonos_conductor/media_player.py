"""Master media player proxy (HomeKit-friendly).

``device_class: receiver`` maps to a HomeKit Television accessory when the
entity is exposed through a HomeKit bridge, which puts the conductor's master
volume on the iOS Control Center remote. Transport commands are forwarded to
the group leader; volume/mute go through the engine.
"""

from __future__ import annotations

from homeassistant.components.media_player import (
    ATTR_MEDIA_ARTIST,
    ATTR_MEDIA_TITLE,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.components.media_player import (
    DOMAIN as MEDIA_PLAYER_DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_ENTITY_PICTURE,
    SERVICE_MEDIA_NEXT_TRACK,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    SERVICE_MEDIA_PREVIOUS_TRACK,
    STATE_PAUSED,
    STATE_PLAYING,
)
from homeassistant.core import Event as HAEvent
from homeassistant.core import EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .controller import UNAVAILABLE_STATES, ConductorEntity, SonosConductorController
from .core.events import SetMaster, SetMute
from .core.volume_math import clamp

#: Master volume change per volume_up/volume_down press.
VOLUME_STEP = 0.03


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the master media player."""
    controller: SonosConductorController | None = hass.data[DOMAIN][entry.entry_id]
    if controller is None:
        return
    async_add_entities([SonosConductorMediaPlayer(controller)])


class SonosConductorMediaPlayer(ConductorEntity, MediaPlayerEntity):
    """Proxy player: volume = engine master, transport = group leader."""

    _attr_device_class = MediaPlayerDeviceClass.RECEIVER
    _attr_name = None  # take the device name ("Sonos Conductor")
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
    )

    def __init__(self, controller: SonosConductorController) -> None:
        super().__init__(controller)
        self._attr_unique_id = f"{controller.entry.entry_id}_master"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Mirror the leader's playback state and media metadata live.
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.controller.leader_entity_id], self._on_leader_change
            )
        )

    @callback
    def _on_leader_change(self, _event: HAEvent[EventStateChangedData]) -> None:
        self.async_write_ha_state()

    @property
    def _leader(self) -> State | None:
        return self.hass.states.get(self.controller.leader_entity_id)

    @property
    def state(self) -> MediaPlayerState | None:
        leader = self._leader
        if leader is None or leader.state in UNAVAILABLE_STATES:
            return None
        if leader.state == STATE_PLAYING:
            return MediaPlayerState.PLAYING
        if leader.state == STATE_PAUSED:
            return MediaPlayerState.PAUSED
        return MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float:
        return self.engine_state.master

    @property
    def is_volume_muted(self) -> bool:
        return self.engine_state.muted

    @property
    def media_title(self) -> str | None:
        leader = self._leader
        return leader.attributes.get(ATTR_MEDIA_TITLE) if leader else None

    @property
    def media_artist(self) -> str | None:
        leader = self._leader
        return leader.attributes.get(ATTR_MEDIA_ARTIST) if leader else None

    @property
    def entity_picture(self) -> str | None:
        leader = self._leader
        if leader is not None and (picture := leader.attributes.get(ATTR_ENTITY_PICTURE)):
            return picture
        return super().entity_picture

    # -- commands -----------------------------------------------------------

    async def async_set_volume_level(self, volume: float) -> None:
        self.controller.submit(SetMaster(clamp(volume), source="media_player"))

    async def async_volume_up(self) -> None:
        self.controller.submit(
            SetMaster(clamp(self.engine_state.master + VOLUME_STEP), source="media_player")
        )

    async def async_volume_down(self) -> None:
        self.controller.submit(
            SetMaster(clamp(self.engine_state.master - VOLUME_STEP), source="media_player")
        )

    async def async_mute_volume(self, mute: bool) -> None:
        self.controller.submit(SetMute(mute, source="media_player"))

    async def async_media_play(self) -> None:
        await self._forward(SERVICE_MEDIA_PLAY)

    async def async_media_pause(self) -> None:
        await self._forward(SERVICE_MEDIA_PAUSE)

    async def async_media_next_track(self) -> None:
        await self._forward(SERVICE_MEDIA_NEXT_TRACK)

    async def async_media_previous_track(self) -> None:
        await self._forward(SERVICE_MEDIA_PREVIOUS_TRACK)

    async def _forward(self, service: str) -> None:
        await self.hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            service,
            {ATTR_ENTITY_ID: self.controller.leader_entity_id},
            blocking=False,
        )
