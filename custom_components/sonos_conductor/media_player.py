"""Master media player proxy (HomeKit-friendly).

``device_class: tv`` maps to a HomeKit *Television*-category accessory when
the entity is exposed through HomeKit (accessory mode), which puts the
conductor's master volume on the iOS Control Center remote and renders the
input-source picker in the Home app. Transport commands are forwarded to the
group leader; volume/mute go through the engine.

Two extra HomeKit affordances:

- **Input sources**: the leader's ``source_list`` (Sonos favorites — radio
  stations, playlists — plus hardware inputs like the Arc's TV) is mirrored,
  so the Home app shows them as selectable inputs. Selection is forwarded to
  the leader. Optionally restricted via the ``homekit_sources`` option.
- **Remote skip**: the HomeKit bridge only handles play/pause itself; every
  other remote key is re-fired on the HA bus as a
  ``homekit_tv_remote_key_pressed`` event. We consume those aimed at this
  entity and turn horizontal arrows / skip keys into next/previous track on
  the leader, so swiping in the iOS Remote skips tracks.
"""

from __future__ import annotations

from homeassistant.components.media_player import (
    ATTR_INPUT_SOURCE,
    ATTR_INPUT_SOURCE_LIST,
    ATTR_MEDIA_ARTIST,
    ATTR_MEDIA_CHANNEL,
    ATTR_MEDIA_TITLE,
    SERVICE_SELECT_SOURCE,
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
    STATE_BUFFERING,
    STATE_PLAYING,
)
from homeassistant.core import Event as HAEvent
from homeassistant.core import EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_HOMEKIT_SOURCES, DOMAIN
from .controller import UNAVAILABLE_STATES, ConductorEntity, SonosConductorController
from .core.events import SetMaster, SetMute
from .core.volume_math import clamp

#: Master volume change per volume_up/volume_down press.
VOLUME_STEP = 0.03

# Literals from homeassistant.components.homekit.const — not imported because
# importing the homekit package pulls in pyhap, which is only installed on
# hosts that actually run a bridge.
EVENT_HOMEKIT_TV_REMOTE_KEY_PRESSED = "homekit_tv_remote_key_pressed"
ATTR_KEY_NAME = "key_name"
NEXT_KEYS = ("arrow_right", "next_track", "fast_forward")
PREVIOUS_KEYS = ("arrow_left", "previous_track", "rewind")

#: Synthetic first input absorbing playback no real source matches
#: (Spotify Connect, announcements). Selecting it is a no-op.
SOURCE_OTHER = "Other"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the master media player."""
    controller: SonosConductorController | None = hass.data[DOMAIN][entry.entry_id]
    if controller is None:
        return
    async_add_entities([SonosConductorMediaPlayer(controller)])


class SonosConductorMediaPlayer(ConductorEntity, MediaPlayerEntity):
    """Proxy player: volume = engine master, transport = group leader.

    HomeKit semantics (why some choices look unusual):

    - ``device_class: tv``, not ``receiver``: Home Assistant maps receiver
      to HomeKit's *Audio Receiver* category, and the Apple Home app only
      renders the input-source picker (and a sane power flow) for
      *Television*-category accessories.
    - Power maps to playback: on = play, off = pause. The entity therefore
      reports ``off`` (never ``paused``/``idle``) when the leader is not
      playing, so the Home app power toggle round-trips truthfully.
    - Source ``Other`` is a synthetic first input that absorbs states no
      real source matches (e.g. Spotify Connect); selecting it is a no-op.
      Without it, HomeKit falls back to highlighting the first real input.
    """

    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_name = None  # take the device name ("Sonos Conductor")
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(self, controller: SonosConductorController) -> None:
        super().__init__(controller)
        self._attr_unique_id = f"{controller.entry.entry_id}_master"
        allowlist = controller.entry.options.get(CONF_HOMEKIT_SOURCES) or []
        self._source_allowlist: tuple[str, ...] = tuple(allowlist)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Mirror the leader's playback state and media metadata live.
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.controller.leader_entity_id], self._on_leader_change
            )
        )
        # Swipes/skip keys in the iOS Remote arrive as bus events (the
        # HomeKit bridge only handles play/pause itself).
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_HOMEKIT_TV_REMOTE_KEY_PRESSED, self._on_remote_key)
        )

    @callback
    def _on_leader_change(self, _event: HAEvent[EventStateChangedData]) -> None:
        self.async_write_ha_state()

    async def _on_remote_key(self, event: HAEvent) -> None:
        if event.data.get(ATTR_ENTITY_ID) != self.entity_id:
            return
        key = event.data.get(ATTR_KEY_NAME)
        if key in NEXT_KEYS:
            await self._forward(SERVICE_MEDIA_NEXT_TRACK)
        elif key in PREVIOUS_KEYS:
            await self._forward(SERVICE_MEDIA_PREVIOUS_TRACK)

    @property
    def _leader(self) -> State | None:
        return self.hass.states.get(self.controller.leader_entity_id)

    @property
    def state(self) -> MediaPlayerState | None:
        """Playing, or off — the HomeKit power toggle mirrors playback.

        Reporting ``paused``/``idle`` would keep the accessory "on" in the
        Home app while its power button (mapped to pause) can never turn it
        off, leaving a toggle that always bounces back.
        """
        leader = self._leader
        if leader is None or leader.state in UNAVAILABLE_STATES:
            return None
        if leader.state in (STATE_PLAYING, STATE_BUFFERING):
            return MediaPlayerState.PLAYING
        return MediaPlayerState.OFF

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

    @property
    def source_list(self) -> list[str] | None:
        """``Other`` + the leader's sources (favorites/inputs), optionally filtered."""
        leader = self._leader
        if leader is None:
            return None
        sources = leader.attributes.get(ATTR_INPUT_SOURCE_LIST) or []
        if self._source_allowlist:
            sources = [s for s in sources if s in self._source_allowlist]
        return [SOURCE_OTHER, *sources]

    @property
    def source(self) -> str | None:
        """Best-effort current source.

        The leader reports ``source`` for inputs it recognizes (e.g. "TV",
        "Spotify Connect"); radio favorites only show up embedded in
        ``media_channel`` (e.g. "NRK P1 · Distriktsprogram"), so fall back to
        the first listed source contained in the channel string. Anything
        else (Spotify Connect, TTS announcements, silence) is ``Other`` so
        HomeKit's current-input marker never lies.
        """
        leader = self._leader
        if leader is None:
            return None
        sources = self.source_list or []
        if (current := leader.attributes.get(ATTR_INPUT_SOURCE)) and current in sources:
            return current
        if channel := leader.attributes.get(ATTR_MEDIA_CHANNEL):
            for candidate in sources:
                if candidate != SOURCE_OTHER and candidate in str(channel):
                    return candidate
        return SOURCE_OTHER

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

    async def async_turn_on(self) -> None:
        """HomeKit power on = resume playback on the leader."""
        await self._forward(SERVICE_MEDIA_PLAY)

    async def async_turn_off(self) -> None:
        """HomeKit power off = pause the leader."""
        await self._forward(SERVICE_MEDIA_PAUSE)

    async def async_media_next_track(self) -> None:
        await self._forward(SERVICE_MEDIA_NEXT_TRACK)

    async def async_media_previous_track(self) -> None:
        await self._forward(SERVICE_MEDIA_PREVIOUS_TRACK)

    async def async_select_source(self, source: str) -> None:
        """Forward a source (Sonos favorite / input) selection to the leader."""
        if source == SOURCE_OTHER:
            return  # synthetic catch-all input: nothing to select
        await self.hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            SERVICE_SELECT_SOURCE,
            {ATTR_ENTITY_ID: self.controller.leader_entity_id, ATTR_INPUT_SOURCE: source},
            blocking=False,
        )

    async def _forward(self, service: str) -> None:
        await self.hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            service,
            {ATTR_ENTITY_ID: self.controller.leader_entity_id},
            blocking=False,
        )
