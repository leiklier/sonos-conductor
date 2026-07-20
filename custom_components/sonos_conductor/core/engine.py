"""The conductor engine: a pure, synchronous event processor.

``handle(event, now)`` mutates :class:`~.model.EngineState` and returns the
effects the adapter must execute. It never performs I/O, never reads a
clock, and never sleeps — see docs/ENGINE_SPEC.md for the full behavioral
contract this class implements.

This module owns the engine's state, bookkeeping, and dispatch; the event
handlers live in feature modules that take the engine as their first
parameter: :mod:`.zones` (zone FSM), :mod:`.audio` (master / mute / reverse
sync / duck / trim), :mod:`.grouping` (group repair), and :mod:`.reconcile`
(volume reconciliation and derived state).

Design notes (spec references in parentheses):

- Reconciliation (§0) is the only path that emits volume effects. Every
  event handler updates state, then calls :func:`.reconcile.reconcile`
  (directly or via :meth:`ConductorEngine._finish`).
- Engine-internal bookkeeping (mutable trim map, duck/TV-mode-change
  timestamp, forced-fallback flag, pending-timer registry) lives in private
  attributes; the published :class:`~.model.EngineState` is untouched.
- The fallback rule (1.5) is materialized into the published zone phase by
  :func:`.zones.sync_fallback` after every state update, so audibility
  stays a simple predicate over phase + solo suppression.
"""

from __future__ import annotations

from statistics import median

from . import audio, grouping, reconcile, timers, zones
from .effects import Effect
from .events import (
    ActivityChanged,
    DockChanged,
    DuckChanged,
    Event,
    ExternalMute,
    ExternalVolume,
    GroupMembersReported,
    HomePresenceChanged,
    OccupancyChanged,
    PlaybackChanged,
    SetEnabled,
    SetFollowMode,
    SetIdleAttenuation,
    SetKeepGrouped,
    SetMaster,
    SetMute,
    SetNightMode,
    SetTrim,
    SetTvSoloMode,
    TimerFired,
    TvPlayingChanged,
)
from .model import (
    ConductorConfig,
    EngineState,
    InitialSnapshot,
    SpeakerState,
    ZoneConfig,
    ZonePhase,
    ZoneState,
)
from .plan import Plan
from .volume_math import clamp, implied_master

_NEG_INF = float("-inf")


class ConductorEngine:
    """Deterministic core of Sonos Conductor."""

    def __init__(self, config: ConductorConfig, snapshot: InitialSnapshot) -> None:
        self.config = config
        self.state: EngineState = EngineState()
        #: Mutable trim shadow, adjustable at runtime via SetTrim (rule 10.1).
        self._trims: dict[str, float] = {s.speaker_id: s.trim for s in config.speakers}
        #: Timer ids the engine believes are pending at the adapter.
        self._pending_timers: set[str] = set()
        #: Monotonic timestamp of the last duck / TV-mode / suppression-set
        #: change; feeds the rule 4.1 transition-suppression window.
        self._mode_change_at: float = _NEG_INF
        #: True while the fallback zone is ACTIVE only because rule 1.5
        #: forces it (it is not occupied and has no TV playing).
        self._fallback_forced: bool = False
        self._seed(snapshot)

    # ------------------------------------------------------------------
    # Startup (spec section 9)
    # ------------------------------------------------------------------

    def _seed(self, snapshot: InitialSnapshot) -> None:
        """Seed all state from the snapshot (rules 9.1, 9.2)."""
        state = self.state
        state.muted = snapshot.mute
        state.enabled = snapshot.enabled
        state.tv_solo_mode = snapshot.tv_solo_mode
        state.follow_mode = snapshot.follow_mode  # 1.9 seeds like any flag (9.1)
        state.idle_attenuation = snapshot.idle_attenuation  # 3.4 seeds like any flag (9.1)
        state.keep_grouped = snapshot.keep_grouped
        state.night_mode = snapshot.night_mode  # 3.3 seeds like any flag (9.1)
        for speaker in self.config.speakers:
            sid = speaker.speaker_id
            state.speakers[sid] = SpeakerState(
                volume=snapshot.volumes.get(sid),
                muted=snapshot.muted.get(sid, False),
                playing=snapshot.playing.get(sid, False),
                docked=snapshot.docked.get(sid, True),
                group_members=tuple(snapshot.group_members.get(sid, ())),
            )
        state.anyone_home = snapshot.anyone_home  # 1.8 seeds like any flag (9.1)
        # Pass 1: seed raw inputs so effective_occupied (1.9) can read every
        # zone's occupancy/TV before any phase is derived.
        for zone in self.config.zones:
            state.zones[zone.zone_id] = ZoneState(
                phase=ZonePhase.IDLE,
                occupied=snapshot.occupancy.get(zone.zone_id, False),
                tv_playing=snapshot.tv_playing.get(zone.zone_id, False),
                activity=snapshot.activity.get(zone.zone_id),
            )
        # Pass 2: derive each phase from the (now complete) world model.
        for zone in self.config.zones:
            zone_state = state.zones[zone.zone_id]
            if not state.speakers[zone.speaker_id].docked:
                zone_state.phase = ZonePhase.STANDALONE
            elif zones.effective_occupied(self, zone):
                zone_state.phase = ZonePhase.ACTIVE
                # An audible zone starts its episode at the current activity.
                zone_state.episode_peak = zone_state.activity
            # else: no hold timers pending at startup, unoccupied = IDLE (9.1).
        for duck in self.config.duck_inputs:
            state.duck_active[duck.input_id] = bool(snapshot.duck_active.get(duck.input_id, False))
        state.suppressed = reconcile.compute_suppressed(self)
        # Fallback forcing (rule 1.5) applies to the seeded phases too.
        fallback = self._fallback_zone()
        if (
            state.enabled
            and state.anyone_home is not False  # 1.8: empty home, no forcing
            and fallback is not None
            and state.zones[fallback.zone_id].phase is ZonePhase.IDLE
            and not any(reconcile.is_audible(self, z.zone_id) for z in self.config.zones)
        ):
            state.zones[fallback.zone_id].phase = ZonePhase.ACTIVE
            self._fallback_forced = True
        if snapshot.master is not None:
            state.master = clamp(snapshot.master)
        else:
            # 9.2: median implied master over audible zones with known volume.
            implied = [
                implied_master(
                    volume, self._trims[z.speaker_id], reconcile.room_scale(self, z.room_id)
                )
                for z in self.config.zones
                if reconcile.is_audible(self, z.zone_id)
                and (volume := state.speakers[z.speaker_id].volume) is not None
            ]
            if implied:
                state.master = float(median(implied))

    def start(self, now: float) -> list[Effect]:
        """Adopt the snapshot and return gentle startup effects (section 9).

        Never causes audible volume jumps: current volumes within
        ``startup_tolerance`` of their computed targets are adopted as-is.
        """
        plan = Plan(self._pending_timers)
        if not self.state.enabled:
            return plan.build()
        tolerance = self.config.tunables.startup_tolerance
        for speaker in self.config.speakers:
            desired = reconcile.desired(self, speaker.speaker_id)
            if desired is None:  # STANDALONE or unmanaged: emit nothing (9.3)
                continue
            speaker_state = self.state.speakers[speaker.speaker_id]
            volume = speaker_state.volume
            if volume is not None and abs(volume - desired) <= tolerance:
                speaker_state.commanded = volume  # adopt as-is
            else:
                speaker_state.commanded = desired
                plan.ramp(speaker.speaker_id, desired, self.config.tunables.rebalance_fade)
        grouping.evaluate_group_repair(self, plan)  # 9.4
        return plan.build()

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    def handle(self, event: Event, now: float) -> list[Effect]:
        """Process one event and return the effects to execute."""
        plan = Plan(self._pending_timers)
        match event:
            case OccupancyChanged():
                zones.on_occupancy(self, event, now, plan)
            case ActivityChanged():
                zones.on_activity(self, event, now)
            case HomePresenceChanged():
                zones.on_home_presence(self, event, now, plan)
            case TvPlayingChanged():
                zones.on_tv_playing(self, event, now, plan)
            case DockChanged():
                zones.on_dock(self, event, now, plan)
            case DuckChanged():
                audio.on_duck(self, event, now, plan)
            case ExternalVolume():
                audio.on_external_volume(self, event, now, plan)
            case ExternalMute():
                audio.on_external_mute(self, event, plan)
            case PlaybackChanged():
                self._on_playback(event)
            case GroupMembersReported():
                grouping.on_group_members(self, event, plan)
            case SetMaster():
                audio.on_set_master(self, event, plan)
            case SetMute():
                audio.on_set_mute(self, event, plan)
            case SetNightMode():
                audio.on_set_night_mode(self, event, now, plan)
            case SetEnabled():
                self._on_set_enabled(event, now, plan)
            case SetTvSoloMode():
                zones.on_set_tv_solo_mode(self, event, now, plan)
            case SetFollowMode():
                zones.on_set_follow_mode(self, event, now, plan)
            case SetIdleAttenuation():
                audio.on_set_idle_attenuation(self, event, now, plan)
            case SetKeepGrouped():
                grouping.on_set_keep_grouped(self, event, plan)
            case SetTrim():
                audio.on_set_trim(self, event, plan)
            case TimerFired():
                self._on_timer_fired(event, now, plan)
            case _:  # unknown event types are ignored (10.4)
                pass
        return plan.build()

    # ------------------------------------------------------------------
    # Playback (rule 10.3) — diagnostics only
    # ------------------------------------------------------------------

    def _on_playback(self, event: PlaybackChanged) -> None:
        speaker_state = self.state.speakers.get(event.speaker_id)
        if speaker_state is not None:  # 10.3: diagnostics only
            speaker_state.playing = event.playing

    # ------------------------------------------------------------------
    # Enable / disable (rule 8)
    # ------------------------------------------------------------------

    def _on_set_enabled(self, event: SetEnabled, now: float, plan: Plan) -> None:
        if not event.enabled:
            was_enabled = self.state.enabled
            self.state.enabled = False
            if was_enabled:
                # 8.1: cancel all timers, emit nothing else.
                for timer_id in self._ordered_pending_timers():
                    plan.cancel_timer(timer_id)
                for speaker_state in self.state.speakers.values():
                    speaker_state.pending_external = None  # their timers are gone
                self._fallback_forced = False
                for zone in self.config.zones:  # keep the world model fresh
                    zones.recompute_phase(self, zone, now)
            return
        self.state.enabled = True
        # 8.2: recompute all zone phases from current inputs.
        for zone in self.config.zones:
            zones.recompute_phase(self, zone, now)
        # Adopt reality before converging: the world may have drifted while
        # we were not writing (external reports updated .volume only).
        for speaker_state in self.state.speakers.values():
            speaker_state.commanded = speaker_state.volume
        reconcile.update_suppression(self, now)
        zones.sync_fallback(self, now, {})
        reconcile.reconcile(self, plan, self.config.tunables.rebalance_fade)  # uniform (8.2)
        grouping.evaluate_group_repair(self, plan)  # re-arm

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def _on_timer_fired(self, event: TimerFired, now: float, plan: Plan) -> None:
        timer_id = event.timer_id
        if timer_id not in self._pending_timers:
            return  # unknown or stale timer id (10.2)
        self._pending_timers.discard(timer_id)
        if timer_id.startswith(timers.ZONE_RELEASE_PREFIX):
            zones.on_release_fired(
                self, timer_id.removeprefix(timers.ZONE_RELEASE_PREFIX), now, plan
            )
        elif timer_id.startswith(timers.EXTERNAL_DEBOUNCE_PREFIX):
            audio.on_debounce_fired(
                self, timer_id.removeprefix(timers.EXTERNAL_DEBOUNCE_PREFIX), now, plan
            )
        elif timer_id == timers.GROUP_REPAIR:
            grouping.on_repair_fired(self, plan)

    def _ordered_pending_timers(self) -> list[str]:
        """Pending timer ids in a deterministic, config-declaration order."""
        ordered = [timers.zone_release(z.zone_id) for z in self.config.zones]
        ordered += [timers.external_debounce(s.speaker_id) for s in self.config.speakers]
        ordered.append(timers.GROUP_REPAIR)
        return [t for t in ordered if t in self._pending_timers]

    # ------------------------------------------------------------------
    # Common tail
    # ------------------------------------------------------------------

    def _finish(
        self, plan: Plan, now: float, default_duration: float, overrides: dict[str, float]
    ) -> None:
        """Common tail: refresh derived state, then reconcile."""
        reconcile.update_suppression(self, now)
        zones.sync_fallback(self, now, overrides)
        reconcile.reconcile(self, plan, default_duration, overrides)

    # ------------------------------------------------------------------
    # Small lookups
    # ------------------------------------------------------------------

    def _zone_config(self, zone_id: str) -> ZoneConfig | None:
        return next((z for z in self.config.zones if z.zone_id == zone_id), None)

    def _fallback_zone(self) -> ZoneConfig | None:
        return next((z for z in self.config.zones if z.fallback), None)

    def _is_standalone_speaker(self, speaker_id: str) -> bool:
        zone = self.config.zone_for_speaker(speaker_id)
        if zone is not None:
            return self.state.zones[zone.zone_id].phase is ZonePhase.STANDALONE
        return not self.state.speakers[speaker_id].docked
