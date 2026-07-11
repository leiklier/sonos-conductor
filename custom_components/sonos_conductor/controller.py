"""The Sonos Conductor controller: a single-writer actor bridging HA and the engine.

Race-condition strategy (docs/ARCHITECTURE.md):

- Every input — HA state events, entity commands, timer expiries — becomes an
  :class:`~.core.events.Event` on one ``asyncio.Queue`` consumed by exactly one
  task. ``engine.handle`` is therefore never re-entered.
- Every volume/mute write this controller issues is recorded in an echo ledger
  (value + monotonic TTL). Incoming state reports that match a pending write
  are consumed as acknowledgements and never reach the engine, so our own
  fades can never masquerade as user input.
- One cancellable ramp per speaker: a new ``RampVolume`` atomically cancels the
  in-flight ramp for that speaker.
- Both engine timers and ramp steps are scheduled with
  :func:`homeassistant.helpers.event.async_call_later` so tests can drive time
  deterministically via ``async_fire_time_changed``.

Unavailability policy:

- Binary inputs (occupancy, duck) that are unavailable/unknown count as
  ``False``; dock sensors count as ``docked=True`` (they are battery-charging
  sensors — losing the sensor must not eject the speaker from the conductor).
- Speaker state events while unavailable are ignored. On the
  unavailable → available transition the echo ledger is stale, so it is
  bypassed: the controller emits ``ExternalVolume`` / ``ExternalMute`` /
  ``GroupMembersReported`` / ``PlaybackChanged`` only for values that differ
  from the engine's world model. The engine treats them as external reports;
  its suppression rules (spec §4) make this safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from homeassistant.components.media_player import (
    ATTR_GROUP_MEMBERS,
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_VOLUME_MUTED,
    SERVICE_JOIN,
)
from homeassistant.components.media_player import (
    DOMAIN as MEDIA_PLAYER_DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_VOLUME_MUTE,
    SERVICE_VOLUME_SET,
    STATE_ON,
    STATE_PLAYING,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.core import (
    Event as HAEvent,
)
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import (
    CONF_DUCK_INPUTS,
    CONF_PRIMARY_SPEAKER,
    CONF_SPEAKERS,
    CONF_TUNABLES,
    CONF_ZONES,
    DOMAIN,
)
from .core.effects import CancelTimer, Effect, JoinGroup, RampVolume, SetSpeakerMute, StartTimer
from .core.events import (
    DockChanged,
    DuckChanged,
    Event,
    ExternalMute,
    ExternalVolume,
    GroupMembersReported,
    OccupancyChanged,
    PlaybackChanged,
    TimerFired,
    TvPlayingChanged,
)
from .core.model import (
    ConductorConfig,
    DuckInputConfig,
    EngineState,
    InitialSnapshot,
    SpeakerConfig,
    Tunables,
    ZoneConfig,
)
from .core.volume_math import volumes_equal

_LOGGER = logging.getLogger(__name__)

#: Options key the controller writes at runtime (persisted master volume).
CONF_LAST_MASTER = "last_master"

#: How long a recorded write may wait for its state-change echo.
ECHO_TTL = 3.0
#: Nominal interval between ramp steps.
RAMP_STEP_INTERVAL = 0.25
#: Smallest useful volume step (Sonos quantizes to 1/100).
RAMP_MIN_STEP = 0.005
#: Debounce before persisting engine master into entry options.
MASTER_PERSIST_DELAY = 10.0

UNAVAILABLE_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)
#: Media player states that count as "TV is playing" for zone aggregation.
TV_PLAYING_STATES = (STATE_ON, STATE_PLAYING)


# ---------------------------------------------------------------------------
# Options parsing (the config-flow contract -> core dataclasses)
# ---------------------------------------------------------------------------


def build_conductor_config(options: Mapping[str, Any]) -> ConductorConfig | None:
    """Translate config-entry options into the frozen core config.

    Returns ``None`` when no speakers are configured (entry loads with no
    controller and no entities).
    """
    raw_speakers = options.get(CONF_SPEAKERS) or []
    if not raw_speakers:
        return None
    speakers = tuple(
        SpeakerConfig(
            speaker_id=s["entity_id"],
            name=s["name"],
            trim=float(s.get("trim", 1.0)),
            dockable=bool(s.get("dock_sensor")),
        )
        for s in raw_speakers
    )
    zones = tuple(
        ZoneConfig(
            zone_id=z["zone_id"],
            name=z["name"],
            speaker_id=z["speaker"],
            room_id=z["room"],
            hold_seconds=float(z.get("hold_seconds", 15.0)),
            fallback=bool(z.get("fallback", False)),
            has_tv=bool(z.get("tvs")),
        )
        for z in options.get(CONF_ZONES) or []
    )
    duck_inputs = tuple(
        DuckInputConfig(
            input_id=d["entity_id"],
            name=d["name"],
            duck_volume=float(d.get("duck_volume", 0.05)),
            engage_fade=float(d.get("engage_fade", 0.0)),
            release_fade=float(d.get("release_fade", 2.0)),
        )
        for d in options.get(CONF_DUCK_INPUTS) or []
    )
    allowed = {f.name for f in fields(Tunables)}
    stored = options.get(CONF_TUNABLES) or {}
    tunables = Tunables(**{k: v for k, v in stored.items() if k in allowed})
    return ConductorConfig(
        speakers=speakers,
        zones=zones,
        duck_inputs=duck_inputs,
        primary_speaker_id=options.get(CONF_PRIMARY_SPEAKER) or None,
        tunables=tunables,
    )


def _is_on(hass: HomeAssistant, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    return state is not None and state.state == STATE_ON


def _is_tv_playing(hass: HomeAssistant, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    return state is not None and state.state in TV_PLAYING_STATES


def _is_docked(hass: HomeAssistant, entity_id: str) -> bool:
    """Dock sensors are battery-charging sensors: on == charging == docked.

    Unavailable/unknown counts as docked so the speaker stays managed.
    """
    state = hass.states.get(entity_id)
    if state is None or state.state in UNAVAILABLE_STATES:
        return True
    return state.state == STATE_ON


def build_initial_snapshot(hass: HomeAssistant, options: Mapping[str, Any]) -> InitialSnapshot:
    """Snapshot the current world from ``hass.states`` to seed the engine."""
    occupancy: dict[str, bool] = {}
    tv_playing: dict[str, bool] = {}
    for zone in options.get(CONF_ZONES) or []:
        zone_id = zone["zone_id"]
        occupancy[zone_id] = any(_is_on(hass, e) for e in zone.get("occupancy") or [])
        tv_playing[zone_id] = any(_is_tv_playing(hass, e) for e in zone.get("tvs") or [])

    docked: dict[str, bool] = {}
    volumes: dict[str, float | None] = {}
    muted: dict[str, bool] = {}
    playing: dict[str, bool] = {}
    group_members: dict[str, tuple[str, ...]] = {}
    for speaker in options.get(CONF_SPEAKERS) or []:
        entity_id = speaker["entity_id"]
        dock_sensor = speaker.get("dock_sensor")
        docked[entity_id] = _is_docked(hass, dock_sensor) if dock_sensor else True
        state = hass.states.get(entity_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            volumes[entity_id] = None
            muted[entity_id] = False
            playing[entity_id] = False
            group_members[entity_id] = ()
        else:
            volumes[entity_id] = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            muted[entity_id] = bool(state.attributes.get(ATTR_MEDIA_VOLUME_MUTED, False))
            playing[entity_id] = state.state == STATE_PLAYING
            group_members[entity_id] = tuple(state.attributes.get(ATTR_GROUP_MEMBERS) or ())

    duck_active = {
        d["entity_id"]: _is_on(hass, d["entity_id"]) for d in options.get(CONF_DUCK_INPUTS) or []
    }
    return InitialSnapshot(
        occupancy=occupancy,
        tv_playing=tv_playing,
        docked=docked,
        volumes=volumes,
        muted=muted,
        playing=playing,
        group_members=group_members,
        duck_active=duck_active,
        master=options.get(CONF_LAST_MASTER),
    )


# ---------------------------------------------------------------------------
# Internal bookkeeping structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _SpeakerView:
    """Last observed HA-side state of a speaker (for change detection)."""

    volume: float | None = None
    muted: bool = False
    members: tuple[str, ...] = ()
    playing: bool = False
    available: bool = True


class _Ramp:
    """A cancellable in-flight volume ramp."""

    __slots__ = ("cancelled", "unsub")

    def __init__(self) -> None:
        self.cancelled = False
        self.unsub: CALLBACK_TYPE | None = None

    def cancel(self) -> None:
        self.cancelled = True
        if self.unsub is not None:
            self.unsub()
            self.unsub = None


# ---------------------------------------------------------------------------
# The controller actor
# ---------------------------------------------------------------------------


class SonosConductorController:
    """Single-writer actor: HA events -> queue -> engine -> effect executor."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        config: ConductorConfig,
        snapshot: InitialSnapshot,
        engine_factory: Callable[[ConductorConfig, InitialSnapshot], Any],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.config = config
        self.signal = f"{DOMAIN}_{entry.entry_id}_updated"
        # The engine is created eagerly so entities added during platform
        # forwarding can always read ``engine.state``.
        self.engine = engine_factory(config, snapshot)

        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._drain_task: asyncio.Task[None] | None = None
        self._started = False
        self._unsubs: list[CALLBACK_TYPE] = []
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._ramps: dict[str, _Ramp] = {}
        self._volume_echo: dict[str, deque[tuple[float, float]]] = {}
        self._mute_echo: dict[str, deque[tuple[bool, float]]] = {}

        # Master persistence (debounced write of engine master into options).
        self._persist_unsub: CALLBACK_TYPE | None = None
        self._persisted_master: float | None = entry.options.get(CONF_LAST_MASTER)
        self._pending_master: float | None = None

        # Entity maps derived from the options contract.
        options = entry.options
        self._occ_sensors_by_zone: dict[str, tuple[str, ...]] = {}
        self._tvs_by_zone: dict[str, tuple[str, ...]] = {}
        self._zones_by_occ_sensor: dict[str, list[str]] = {}
        self._zones_by_tv: dict[str, list[str]] = {}
        for zone in options.get(CONF_ZONES) or []:
            zone_id = zone["zone_id"]
            sensors = tuple(zone.get("occupancy") or ())
            tvs = tuple(zone.get("tvs") or ())
            self._occ_sensors_by_zone[zone_id] = sensors
            self._tvs_by_zone[zone_id] = tvs
            for sensor in sensors:
                self._zones_by_occ_sensor.setdefault(sensor, []).append(zone_id)
            for tv in tvs:
                self._zones_by_tv.setdefault(tv, []).append(zone_id)
        self._speaker_by_dock: dict[str, str] = {
            s["dock_sensor"]: s["entity_id"]
            for s in options.get(CONF_SPEAKERS) or []
            if s.get("dock_sensor")
        }
        self._duck_entities: tuple[str, ...] = tuple(
            d["entity_id"] for d in options.get(CONF_DUCK_INPUTS) or []
        )

        # Aggregate caches (seeded from the snapshot; flips produce events).
        self._occ_agg: dict[str, bool] = dict(snapshot.occupancy)
        self._tv_agg: dict[str, bool] = dict(snapshot.tv_playing)
        self._dock_agg: dict[str, bool] = dict(snapshot.docked)
        self._duck_agg: dict[str, bool] = dict(snapshot.duck_active)

        # Last observed speaker attributes (change detection + availability).
        self._speaker_views: dict[str, _SpeakerView] = {}
        for speaker in config.speakers:
            state = hass.states.get(speaker.speaker_id)
            self._speaker_views[speaker.speaker_id] = _SpeakerView(
                volume=snapshot.volumes.get(speaker.speaker_id),
                muted=bool(snapshot.muted.get(speaker.speaker_id, False)),
                members=tuple(snapshot.group_members.get(speaker.speaker_id, ())),
                playing=bool(snapshot.playing.get(speaker.speaker_id, False)),
                available=state is not None and state.state not in UNAVAILABLE_STATES,
            )

    # -- public API for entities -------------------------------------------

    @property
    def leader_entity_id(self) -> str:
        """The media_player that transport commands are forwarded to."""
        return self.config.leader_id()

    @property
    def state(self) -> EngineState:
        return self.engine.state

    @callback
    def submit(self, event: Event) -> None:
        """Enqueue an engine event. Entities and listeners run in the loop,
        so a plain ``put_nowait`` is sufficient (no cross-thread callers)."""
        self._queue.put_nowait(event)
        self._ensure_drain()

    # -- lifecycle -----------------------------------------------------------

    async def async_start(self) -> None:
        """Run startup reconciliation, subscribe to HA, start the actor."""
        self._started = True
        effects = self.engine.start(time.monotonic())
        await self._execute(effects)
        self._subscribe()
        self._ensure_drain()
        self._publish()

    async def async_stop(self) -> None:
        """Cancel subscriptions, the actor, timers and ramps; flush master."""
        self._started = False
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain_task
        self._drain_task = None
        for unsub in self._timers.values():
            unsub()
        self._timers.clear()
        for ramp in self._ramps.values():
            ramp.cancel()
        self._ramps.clear()
        self._flush_master()

    @callback
    def _ensure_drain(self) -> None:
        """Ensure exactly one drain task is processing the queue.

        The drain task is created as a *tracked* hass task (not a background
        task) so ``async_block_till_done`` deterministically waits for event
        processing — and there is never more than one, which is what makes
        this a single-writer actor: ``engine.handle`` cannot be re-entered.
        """
        if not self._started or self._queue.empty():
            return
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = self.hass.async_create_task(
                self._drain(), name=f"{DOMAIN}-actor-{self.entry.entry_id}"
            )

    async def _drain(self) -> None:
        """The single writer: one event at a time, never re-entered."""
        while not self._queue.empty():
            event = self._queue.get_nowait()
            try:
                effects = self.engine.handle(event, time.monotonic())
                await self._execute(effects)
            except Exception:
                _LOGGER.exception("Error while processing %s", event)
            self._publish()

    @callback
    def _publish(self) -> None:
        """Refresh conductor entities and schedule master persistence."""
        self._maybe_schedule_master_persist()
        async_dispatcher_send(self.hass, self.signal)

    # -- HA subscriptions ----------------------------------------------------

    @callback
    def _subscribe(self) -> None:
        speaker_ids = [s.speaker_id for s in self.config.speakers]
        if speaker_ids:
            self._unsubs.append(
                async_track_state_change_event(self.hass, speaker_ids, self._on_speaker_event)
            )
        if self._zones_by_occ_sensor:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, sorted(self._zones_by_occ_sensor), self._on_occupancy_event
                )
            )
        if self._zones_by_tv:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, sorted(self._zones_by_tv), self._on_tv_event
                )
            )
        if self._speaker_by_dock:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, sorted(self._speaker_by_dock), self._on_dock_event
                )
            )
        if self._duck_entities:
            self._unsubs.append(
                async_track_state_change_event(self.hass, self._duck_entities, self._on_duck_event)
            )

    @callback
    def _on_speaker_event(self, event: HAEvent[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]
        view = self._speaker_views.get(entity_id)
        if new_state is None or view is None:
            return
        if new_state.state in UNAVAILABLE_STATES:
            # Speaker attribute reports while unavailable are meaningless.
            view.available = False
            return

        volume = new_state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
        raw_muted = new_state.attributes.get(ATTR_MEDIA_VOLUME_MUTED)
        members = tuple(new_state.attributes.get(ATTR_GROUP_MEMBERS) or ())
        playing = new_state.state == STATE_PLAYING

        if not view.available:
            self._readopt_speaker(entity_id, view, volume, raw_muted, members, playing)
            return

        if volume is not None and not volumes_equal(volume, view.volume):
            view.volume = volume
            if not self._consume_volume_echo(entity_id, volume):
                self.submit(ExternalVolume(entity_id, volume))
        if raw_muted is not None and bool(raw_muted) != view.muted:
            view.muted = bool(raw_muted)
            if not self._consume_mute_echo(entity_id, bool(raw_muted)):
                self.submit(ExternalMute(entity_id, bool(raw_muted)))
        if members != view.members:
            view.members = members
            self.submit(GroupMembersReported(entity_id, members))
        if playing != view.playing:
            view.playing = playing
            self.submit(PlaybackChanged(entity_id, playing))

    @callback
    def _readopt_speaker(
        self,
        entity_id: str,
        view: _SpeakerView,
        volume: float | None,
        raw_muted: Any,
        members: tuple[str, ...],
        playing: bool,
    ) -> None:
        """Handle the unavailable -> available transition.

        The echo ledger is stale (writes issued before the outage can no
        longer echo), so it is bypassed. Anything that differs from the
        engine's world model is reported as an external change; the engine's
        suppression rules decide what to do with it.
        """
        view.available = True
        engine_speaker = self.engine.state.speakers.get(entity_id)
        known_volume = engine_speaker.volume if engine_speaker is not None else view.volume
        known_muted = engine_speaker.muted if engine_speaker is not None else view.muted
        known_members = engine_speaker.group_members if engine_speaker is not None else view.members
        known_playing = engine_speaker.playing if engine_speaker is not None else view.playing

        if volume is not None:
            view.volume = volume
            if not volumes_equal(volume, known_volume):
                self.submit(ExternalVolume(entity_id, volume))
        if raw_muted is not None:
            view.muted = bool(raw_muted)
            if bool(raw_muted) != known_muted:
                self.submit(ExternalMute(entity_id, bool(raw_muted)))
        view.members = members
        if members != tuple(known_members):
            self.submit(GroupMembersReported(entity_id, members))
        view.playing = playing
        if playing != known_playing:
            self.submit(PlaybackChanged(entity_id, playing))

    @callback
    def _on_occupancy_event(self, event: HAEvent[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        for zone_id in self._zones_by_occ_sensor.get(entity_id, ()):
            occupied = any(_is_on(self.hass, e) for e in self._occ_sensors_by_zone[zone_id])
            if occupied != self._occ_agg.get(zone_id):
                self._occ_agg[zone_id] = occupied
                self.submit(OccupancyChanged(zone_id, occupied))

    @callback
    def _on_tv_event(self, event: HAEvent[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        for zone_id in self._zones_by_tv.get(entity_id, ()):
            playing = any(_is_tv_playing(self.hass, e) for e in self._tvs_by_zone[zone_id])
            if playing != self._tv_agg.get(zone_id):
                self._tv_agg[zone_id] = playing
                self.submit(TvPlayingChanged(zone_id, playing))

    @callback
    def _on_dock_event(self, event: HAEvent[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        speaker_id = self._speaker_by_dock.get(entity_id)
        if speaker_id is None:
            return
        docked = _is_docked(self.hass, entity_id)
        if docked != self._dock_agg.get(speaker_id):
            self._dock_agg[speaker_id] = docked
            self.submit(DockChanged(speaker_id, docked))

    @callback
    def _on_duck_event(self, event: HAEvent[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        active = _is_on(self.hass, entity_id)
        if active != self._duck_agg.get(entity_id):
            self._duck_agg[entity_id] = active
            self.submit(DuckChanged(entity_id, active))

    # -- echo ledger ----------------------------------------------------------

    @callback
    def _record_volume_echo(self, speaker_id: str, value: float) -> None:
        self._volume_echo.setdefault(speaker_id, deque()).append(
            (value, time.monotonic() + ECHO_TTL)
        )

    @callback
    def _consume_volume_echo(self, speaker_id: str, value: float) -> bool:
        entries = self._volume_echo.get(speaker_id)
        if not entries:
            return False
        now = time.monotonic()
        while entries and entries[0][1] <= now:  # deadlines are appended in order
            entries.popleft()
        for index, (recorded, _deadline) in enumerate(entries):
            if volumes_equal(recorded, value):
                del entries[index]
                return True
        return False

    @callback
    def _record_mute_echo(self, speaker_id: str, muted: bool) -> None:
        self._mute_echo.setdefault(speaker_id, deque()).append((muted, time.monotonic() + ECHO_TTL))

    @callback
    def _consume_mute_echo(self, speaker_id: str, muted: bool) -> bool:
        entries = self._mute_echo.get(speaker_id)
        if not entries:
            return False
        now = time.monotonic()
        while entries and entries[0][1] <= now:
            entries.popleft()
        for index, (recorded, _deadline) in enumerate(entries):
            if recorded == muted:
                del entries[index]
                return True
        return False

    # -- effect executor -------------------------------------------------------

    async def _execute(self, effects: list[Effect]) -> None:
        for effect in effects:
            try:
                if isinstance(effect, RampVolume):
                    await self._execute_ramp(effect)
                elif isinstance(effect, SetSpeakerMute):
                    await self._execute_mute(effect)
                elif isinstance(effect, StartTimer):
                    self._start_timer(effect.timer_id, effect.delay)
                elif isinstance(effect, CancelTimer):
                    self._cancel_timer(effect.timer_id)
                elif isinstance(effect, JoinGroup):
                    await self._execute_join(effect)
                else:
                    _LOGGER.warning("Unknown effect: %r", effect)
            except Exception:
                _LOGGER.exception("Effect %r failed", effect)

    async def _execute_ramp(self, effect: RampVolume) -> None:
        speaker_id = effect.speaker_id
        previous = self._ramps.pop(speaker_id, None)
        if previous is not None:
            previous.cancel()
        state = self.hass.states.get(speaker_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            # Speaker is away; the engine reconciles again when it returns.
            return
        target = round(effect.target, 4)
        current = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
        if effect.duration <= 0 or current is None:
            await self._async_write_volume(speaker_id, target)
            return
        delta = target - current
        steps = max(2, round(effect.duration / RAMP_STEP_INTERVAL))
        if abs(delta) / steps < RAMP_MIN_STEP:
            steps = int(abs(delta) / RAMP_MIN_STEP)
        if steps <= 1:
            await self._async_write_volume(speaker_id, target)
            return
        values = [round(current + delta * i / steps, 4) for i in range(1, steps + 1)]
        values[-1] = target
        interval = effect.duration / steps
        ramp = _Ramp()
        self._ramps[speaker_id] = ramp
        self._schedule_ramp_step(ramp, speaker_id, values, 0, interval)

    @callback
    def _schedule_ramp_step(
        self, ramp: _Ramp, speaker_id: str, values: list[float], index: int, interval: float
    ) -> None:
        async def _step(_now: Any) -> None:
            if ramp.cancelled:
                return
            ramp.unsub = None
            try:
                state = self.hass.states.get(speaker_id)
                if state is None or state.state in UNAVAILABLE_STATES:
                    self._finish_ramp(ramp, speaker_id)
                    return
                await self._async_write_volume(speaker_id, values[index])
            except Exception:
                _LOGGER.exception("Ramp step for %s failed", speaker_id)
                self._finish_ramp(ramp, speaker_id)
                return
            if ramp.cancelled:
                return
            if index + 1 < len(values):
                self._schedule_ramp_step(ramp, speaker_id, values, index + 1, interval)
            else:
                self._finish_ramp(ramp, speaker_id)

        ramp.unsub = async_call_later(self.hass, interval, _step)

    @callback
    def _finish_ramp(self, ramp: _Ramp, speaker_id: str) -> None:
        if self._ramps.get(speaker_id) is ramp:
            del self._ramps[speaker_id]

    async def _async_write_volume(self, speaker_id: str, value: float) -> None:
        # Ledger first: the state-change echo may arrive before the service
        # call returns.
        self._record_volume_echo(speaker_id, value)
        await self.hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            SERVICE_VOLUME_SET,
            {ATTR_ENTITY_ID: speaker_id, ATTR_MEDIA_VOLUME_LEVEL: value},
            blocking=False,
        )

    async def _execute_mute(self, effect: SetSpeakerMute) -> None:
        self._record_mute_echo(effect.speaker_id, effect.muted)
        await self.hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            SERVICE_VOLUME_MUTE,
            {ATTR_ENTITY_ID: effect.speaker_id, ATTR_MEDIA_VOLUME_MUTED: effect.muted},
            blocking=False,
        )

    async def _execute_join(self, effect: JoinGroup) -> None:
        # NOTE: no group-echo cooldown is applied. Spec rule 7.2 needs the
        # resulting GroupMembersReported events to cancel the repair timer,
        # and 7.3's repair-once semantics live in the engine. Everything is
        # forwarded; add a cooldown only if repair loops are ever observed.
        await self.hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            SERVICE_JOIN,
            {ATTR_ENTITY_ID: effect.leader_id, ATTR_GROUP_MEMBERS: list(effect.member_ids)},
            blocking=False,
        )

    # -- timers ------------------------------------------------------------------

    @callback
    def _start_timer(self, timer_id: str, delay: float) -> None:
        self._cancel_timer(timer_id)

        @callback
        def _fire(_now: Any) -> None:
            self._timers.pop(timer_id, None)
            self.submit(TimerFired(timer_id))

        self._timers[timer_id] = async_call_later(self.hass, delay, _fire)

    @callback
    def _cancel_timer(self, timer_id: str) -> None:
        unsub = self._timers.pop(timer_id, None)
        if unsub is not None:
            unsub()

    # -- master persistence --------------------------------------------------------

    @callback
    def _maybe_schedule_master_persist(self) -> None:
        master = self.engine.state.master
        if master == self._persisted_master:
            # Back at the stored value; drop any pending write.
            if self._persist_unsub is not None:
                self._persist_unsub()
                self._persist_unsub = None
            self._pending_master = None
            return
        if master == self._pending_master:
            return  # already scheduled for this value
        self._pending_master = master
        if self._persist_unsub is not None:
            self._persist_unsub()
        self._persist_unsub = async_call_later(
            self.hass, MASTER_PERSIST_DELAY, self._persist_master
        )

    @callback
    def _persist_master(self, _now: Any = None) -> None:
        self._persist_unsub = None
        self._pending_master = None
        master = self.engine.state.master
        self._persisted_master = master
        if self.entry.options.get(CONF_LAST_MASTER) != master:
            # The options listener in __init__.py ignores last_master-only
            # diffs, so this never causes a reload loop.
            self.hass.config_entries.async_update_entry(
                self.entry, options={**self.entry.options, CONF_LAST_MASTER: master}
            )

    @callback
    def _flush_master(self) -> None:
        if self._persist_unsub is not None:
            self._persist_unsub()
            self._persist_unsub = None
        if self.engine.state.master != self._persisted_master:
            self._persist_master()


# ---------------------------------------------------------------------------
# Shared entity plumbing
# ---------------------------------------------------------------------------


def conductor_device_info(entry: ConfigEntry) -> DeviceInfo:
    """One shared device for all conductor entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Sonos Conductor",
        manufacturer="Sonos Conductor",
        model="Presence-aware volume orchestrator",
        entry_type=DeviceEntryType.SERVICE,
    )


class ConductorEntity(Entity):
    """Base for conductor entities: dispatcher-driven, never polled."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, controller: SonosConductorController) -> None:
        self.controller = controller
        self._attr_device_info = conductor_device_info(controller.entry)

    @property
    def engine_state(self) -> EngineState:
        return self.controller.engine.state

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self.controller.signal, self._on_controller_update)
        )

    @callback
    def _on_controller_update(self) -> None:
        self.async_write_ha_state()
