"""The conductor engine: a pure, synchronous event processor.

``handle(event, now)`` mutates :class:`~.model.EngineState` and returns the
effects the adapter must execute. It never performs I/O, never reads a
clock, and never sleeps — see docs/ENGINE_SPEC.md for the full behavioral
contract this class implements.

Design notes (spec references in parentheses):

- Reconciliation (§0) is the only path that emits volume effects. Every
  event handler updates state, then calls :meth:`ConductorEngine._reconcile`
  (directly or via :meth:`ConductorEngine._finish`).
- Engine-internal bookkeeping (mutable trim map, duck/TV-mode-change
  timestamp, forced-fallback flag, pending-timer registry) lives in private
  attributes; the published :class:`~.model.EngineState` is untouched.
- The fallback rule (1.5) is materialized into the published zone phase by
  :meth:`ConductorEngine._sync_fallback` after every state update, so
  audibility stays a simple predicate over phase + solo suppression.
"""

from __future__ import annotations

from statistics import median

from . import timers
from .effects import (
    CancelTimer,
    Effect,
    JoinGroup,
    RampVolume,
    SetSpeakerMute,
    StartTimer,
)
from .events import (
    DockChanged,
    DuckChanged,
    Event,
    ExternalMute,
    ExternalVolume,
    GroupMembersReported,
    OccupancyChanged,
    PlaybackChanged,
    SetEnabled,
    SetKeepGrouped,
    SetMaster,
    SetMute,
    SetTrim,
    SetTvSolo,
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
from .volume_math import clamp, implied_master, room_scale, speaker_target, volumes_equal

_AUDIBLE_PHASES = frozenset((ZonePhase.ACTIVE, ZonePhase.RELEASING))

#: External volume reports at or below this are never trusted for reverse
#: sync (rule 4.1 hard-zero guard).
_HARD_ZERO = 0.01

_NEG_INF = float("-inf")


class _Plan:
    """Accumulates the effects of one ``handle()`` call in spec 10.5 order.

    ``CancelTimer`` first, then mute effects, then volume effects, then
    ``StartTimer``, then ``JoinGroup``. It shares the engine's pending-timer
    registry so cancellations are only emitted for timers the engine
    actually believes are running (cancel stays idempotent regardless).
    """

    __slots__ = ("_cancels", "_joins", "_mutes", "_pending", "_starts", "_volumes")

    def __init__(self, pending: set[str]) -> None:
        self._pending = pending
        self._cancels: list[Effect] = []
        self._mutes: list[Effect] = []
        self._volumes: list[Effect] = []
        self._starts: list[Effect] = []
        self._joins: list[Effect] = []

    def cancel_timer(self, timer_id: str) -> None:
        if timer_id in self._pending:
            self._pending.discard(timer_id)
            self._cancels.append(CancelTimer(timer_id))

    def start_timer(self, timer_id: str, delay: float) -> None:
        # StartTimer with a pending id restarts it (effects contract).
        self._pending.add(timer_id)
        self._starts.append(StartTimer(timer_id, delay))

    def mute(self, speaker_id: str, muted: bool) -> None:
        self._mutes.append(SetSpeakerMute(speaker_id, muted))

    def ramp(self, speaker_id: str, target: float, duration: float) -> None:
        self._volumes.append(RampVolume(speaker_id, target, duration))

    def join(self, leader_id: str, member_ids: tuple[str, ...]) -> None:
        self._joins.append(JoinGroup(leader_id, member_ids))

    def build(self) -> list[Effect]:
        return [*self._cancels, *self._mutes, *self._volumes, *self._starts, *self._joins]


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
        #: Zone ids currently solo-suppressed (rule 6.2), kept fresh.
        self._suppressed: frozenset[str] = frozenset()
        self._seed(snapshot)

    # ------------------------------------------------------------------
    # Startup (spec section 9)
    # ------------------------------------------------------------------

    def _seed(self, snapshot: InitialSnapshot) -> None:
        """Seed all state from the snapshot (rules 9.1, 9.2)."""
        state = self.state
        state.muted = snapshot.mute
        state.enabled = snapshot.enabled
        state.tv_solo = snapshot.tv_solo
        state.keep_grouped = snapshot.keep_grouped
        for speaker in self.config.speakers:
            sid = speaker.speaker_id
            state.speakers[sid] = SpeakerState(
                volume=snapshot.volumes.get(sid),
                muted=snapshot.muted.get(sid, False),
                playing=snapshot.playing.get(sid, False),
                docked=snapshot.docked.get(sid, True),
                group_members=tuple(snapshot.group_members.get(sid, ())),
            )
        for zone in self.config.zones:
            occupied = snapshot.occupancy.get(zone.zone_id, False)
            tv = snapshot.tv_playing.get(zone.zone_id, False)
            if not state.speakers[zone.speaker_id].docked:
                phase = ZonePhase.STANDALONE
            elif occupied or tv:
                phase = ZonePhase.ACTIVE
            else:
                # No hold timers pending at startup: unoccupied = IDLE (9.1).
                phase = ZonePhase.IDLE
            state.zones[zone.zone_id] = ZoneState(phase=phase, occupied=occupied, tv_playing=tv)
        for duck in self.config.duck_inputs:
            state.duck_active[duck.input_id] = bool(snapshot.duck_active.get(duck.input_id, False))
        self._suppressed = self._compute_suppressed()
        # Fallback forcing (rule 1.5) applies to the seeded phases too.
        fallback = self._fallback_zone()
        if (
            state.enabled
            and fallback is not None
            and state.zones[fallback.zone_id].phase is ZonePhase.IDLE
            and not any(self._is_audible(z.zone_id) for z in self.config.zones)
        ):
            state.zones[fallback.zone_id].phase = ZonePhase.ACTIVE
            self._fallback_forced = True
        if snapshot.master is not None:
            state.master = clamp(snapshot.master)
        else:
            # 9.2: median implied master over audible zones with known volume.
            implied = [
                implied_master(volume, self._trims[z.speaker_id], self._room_scale(z.room_id))
                for z in self.config.zones
                if self._is_audible(z.zone_id)
                and (volume := state.speakers[z.speaker_id].volume) is not None
            ]
            if implied:
                state.master = float(median(implied))

    def start(self, now: float) -> list[Effect]:
        """Adopt the snapshot and return gentle startup effects (section 9).

        Never causes audible volume jumps: current volumes within
        ``startup_tolerance`` of their computed targets are adopted as-is.
        """
        plan = _Plan(self._pending_timers)
        if not self.state.enabled:
            return plan.build()
        tolerance = self.config.tunables.startup_tolerance
        for speaker in self.config.speakers:
            desired = self._desired(speaker.speaker_id)
            if desired is None:  # STANDALONE or unmanaged: emit nothing (9.3)
                continue
            speaker_state = self.state.speakers[speaker.speaker_id]
            volume = speaker_state.volume
            if volume is not None and abs(volume - desired) <= tolerance:
                speaker_state.commanded = volume  # adopt as-is
            else:
                speaker_state.commanded = desired
                plan.ramp(speaker.speaker_id, desired, self.config.tunables.rebalance_fade)
        self._evaluate_group_repair(plan)  # 9.4
        return plan.build()

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    def handle(self, event: Event, now: float) -> list[Effect]:
        """Process one event and return the effects to execute."""
        plan = _Plan(self._pending_timers)
        match event:
            case OccupancyChanged():
                self._on_occupancy(event, now, plan)
            case TvPlayingChanged():
                self._on_tv_playing(event, now, plan)
            case DockChanged():
                self._on_dock(event, now, plan)
            case DuckChanged():
                self._on_duck(event, now, plan)
            case ExternalVolume():
                self._on_external_volume(event, now, plan)
            case ExternalMute():
                self._on_external_mute(event, plan)
            case PlaybackChanged():
                self._on_playback(event)
            case GroupMembersReported():
                self._on_group_members(event, plan)
            case SetMaster():
                self._on_set_master(event, plan)
            case SetMute():
                self._on_set_mute(event, plan)
            case SetEnabled():
                self._on_set_enabled(event, now, plan)
            case SetTvSolo():
                self._on_set_tv_solo(event, now, plan)
            case SetKeepGrouped():
                self._on_set_keep_grouped(event, plan)
            case SetTrim():
                self._on_set_trim(event, plan)
            case TimerFired():
                self._on_timer_fired(event, now, plan)
            case _:  # unknown event types are ignored (10.4)
                pass
        return plan.build()

    # ------------------------------------------------------------------
    # Zone lifecycle (rule 1) + TV occupancy (1.4/6.1)
    # ------------------------------------------------------------------

    def _on_occupancy(self, event: OccupancyChanged, now: float, plan: _Plan) -> None:
        zone = self._zone_config(event.zone_id)
        if zone is None:  # 10.4
            return
        self.state.zones[zone.zone_id].occupied = event.occupied
        if not self.state.enabled:
            self._recompute_phase(zone, now)
            return
        overrides: dict[str, float] = {}
        self._apply_zone_inputs(zone, now, plan, overrides)
        self._finish(plan, now, self.config.tunables.rebalance_fade, overrides)

    def _on_tv_playing(self, event: TvPlayingChanged, now: float, plan: _Plan) -> None:
        zone = self._zone_config(event.zone_id)
        if zone is None:  # 10.4
            return
        zone_state = self.state.zones[zone.zone_id]
        if zone_state.tv_playing != event.playing:
            zone_state.tv_playing = event.playing
            self._mode_change_at = now  # TV-mode change (rules 4.1, 6.2)
        if not self.state.enabled:
            self._recompute_phase(zone, now)
            self._update_suppression(now)
            return
        overrides: dict[str, float] = {}
        self._apply_zone_inputs(zone, now, plan, overrides)
        self._finish(plan, now, self.config.tunables.rebalance_fade, overrides)

    def _apply_zone_inputs(
        self, zone: ZoneConfig, now: float, plan: _Plan, overrides: dict[str, float]
    ) -> None:
        """Run the IDLE/ACTIVE/RELEASING transitions for effective occupancy.

        Effective occupancy is ``occupied or tv_playing`` (rule 1.4).
        """
        zone_state = self.state.zones[zone.zone_id]
        if zone_state.phase is ZonePhase.STANDALONE:  # dock rules own this phase
            return
        effective = zone_state.occupied or zone_state.tv_playing
        if effective:
            if zone_state.phase is ZonePhase.IDLE:
                plan.cancel_timer(timers.zone_release(zone.zone_id))  # 1.1 (idempotent)
                self._set_phase(zone.zone_id, ZonePhase.ACTIVE, now)
                overrides[zone.speaker_id] = self.config.tunables.fade_in
            elif zone_state.phase is ZonePhase.RELEASING:
                plan.cancel_timer(timers.zone_release(zone.zone_id))  # 1.1
                self._set_phase(zone.zone_id, ZonePhase.ACTIVE, now)  # no volume effect
            if zone.fallback:
                # A forced-active fallback zone that becomes occupied (or
                # gets a TV) now holds its audibility on its own merits.
                self._fallback_forced = False
        elif zone_state.phase is ZonePhase.ACTIVE and not (zone.fallback and self._fallback_forced):
            self._set_phase(zone.zone_id, ZonePhase.RELEASING, now)  # 1.2
            plan.start_timer(timers.zone_release(zone.zone_id), zone.hold_seconds)

    def _on_release_fired(self, zone_id: str, now: float, plan: _Plan) -> None:
        zone = self._zone_config(zone_id)
        if zone is None:  # 10.2/10.4
            return
        if self.state.zones[zone_id].phase is not ZonePhase.RELEASING:
            return  # stale release timer (1.3)
        self._set_phase(zone_id, ZonePhase.IDLE, now)
        overrides = {zone.speaker_id: self.config.tunables.fade_out}
        self._finish(plan, now, self.config.tunables.rebalance_fade, overrides)

    # ------------------------------------------------------------------
    # Dock / standalone (rule 2)
    # ------------------------------------------------------------------

    def _on_dock(self, event: DockChanged, now: float, plan: _Plan) -> None:
        speaker_state = self.state.speakers.get(event.speaker_id)
        if speaker_state is None:  # 10.4
            return
        speaker_state.docked = event.docked
        zone = self.config.zone_for_speaker(event.speaker_id)
        if not self.state.enabled:
            if zone is not None:
                self._recompute_phase(zone, now)
            return
        overrides: dict[str, float] = {}
        if zone is not None:
            zone_state = self.state.zones[zone.zone_id]
            if not event.docked:
                if zone_state.phase is not ZonePhase.STANDALONE:
                    plan.cancel_timer(timers.zone_release(zone.zone_id))  # 2.1
                    self._set_phase(zone.zone_id, ZonePhase.STANDALONE, now)
                    if zone.fallback:
                        self._fallback_forced = False
                    # The user takes the speaker over at its current volume;
                    # we no longer own a commanded target for it (2.1).
                    speaker_state.commanded = None
            elif zone_state.phase is ZonePhase.STANDALONE:
                # 2.2: recompute from current inputs as if they just changed.
                speaker_state.commanded = None  # take ownership back fresh
                if zone_state.occupied or zone_state.tv_playing:
                    self._set_phase(zone.zone_id, ZonePhase.ACTIVE, now)
                    overrides[zone.speaker_id] = self.config.tunables.fade_in
                else:
                    self._set_phase(zone.zone_id, ZonePhase.IDLE, now)
        self._finish(plan, now, self.config.tunables.rebalance_fade, overrides)
        self._evaluate_group_repair(plan)  # 7.2 trigger

    # ------------------------------------------------------------------
    # Master / mute (rules 3, 5)
    # ------------------------------------------------------------------

    def _on_set_master(self, event: SetMaster, plan: _Plan) -> None:
        self.state.master = clamp(event.value)  # 3.1
        if not self.state.enabled or self.state.muted:
            return  # store only; reconcile happens on unmute / enable
        self._reconcile(plan, self.config.tunables.master_fade)

    def _on_set_mute(self, event: SetMute, plan: _Plan) -> None:
        if not self.state.enabled:
            self.state.muted = event.muted  # state stays fresh (8.1)
            return
        self._apply_global_mute(event.muted, plan)

    def _on_external_mute(self, event: ExternalMute, plan: _Plan) -> None:
        speaker_state = self.state.speakers.get(event.speaker_id)
        if speaker_state is None:  # 10.4
            return
        speaker_state.muted = event.muted
        if (
            not self.state.enabled
            or self._is_standalone_speaker(event.speaker_id)
            or event.muted == self.state.muted
        ):
            return  # 5.3 applies only to differing non-STANDALONE reports
        self._apply_global_mute(event.muted, plan)

    def _apply_global_mute(self, muted: bool, plan: _Plan) -> None:
        self.state.muted = muted
        for speaker in self.config.speakers:  # 5.1/5.2 fan-out, config order
            if self._is_standalone_speaker(speaker.speaker_id):
                continue
            plan.mute(speaker.speaker_id, muted)
            self.state.speakers[speaker.speaker_id].muted = muted
        if not muted:
            # 5.2: master may have changed while muted.
            self._reconcile(plan, self.config.tunables.rebalance_fade)

    # ------------------------------------------------------------------
    # External volume reports / reverse sync (rule 4)
    # ------------------------------------------------------------------

    def _on_external_volume(self, event: ExternalVolume, now: float, plan: _Plan) -> None:
        speaker_state = self.state.speakers.get(event.speaker_id)
        if speaker_state is None:  # 10.4
            return
        speaker_state.volume = event.volume  # 4.1: always update
        if not self._sync_allowed(event.speaker_id, event.volume, now):
            return  # report discarded
        speaker_state.pending_external = event.volume  # 4.2: debounce
        plan.start_timer(
            timers.external_debounce(event.speaker_id),
            self.config.tunables.external_debounce,
        )

    def _sync_allowed(self, speaker_id: str, volume: float, now: float) -> bool:
        """Rule 4.1 acceptance conditions (also re-checked at fire time)."""
        state = self.state
        if not state.enabled or state.muted:
            return False
        if volume <= _HARD_ZERO:
            return False
        if self._is_standalone_speaker(speaker_id):
            return False
        zone = self.config.zone_for_speaker(speaker_id)
        if zone is None or not self._is_audible(zone.zone_id):
            return False
        if any(state.duck_active.get(d.input_id) for d in self.config.duck_inputs):
            return False
        last = self._mode_change_at
        for zone_state in state.zones.values():
            last = max(last, zone_state.last_transition)
        return now - last >= self.config.tunables.transition_suppression

    def _on_debounce_fired(self, speaker_id: str, now: float, plan: _Plan) -> None:
        speaker_state = self.state.speakers.get(speaker_id)
        if speaker_state is None:  # 10.4
            return
        volume = speaker_state.pending_external
        speaker_state.pending_external = None
        if volume is None:  # cleared by a reconciliation write (4.4)
            return
        if not self._sync_allowed(speaker_id, volume, now):  # 4.3 re-check
            return
        zone = self.config.zone_for_speaker(speaker_id)
        if zone is None:  # unreachable: _sync_allowed guarantees a zone
            return
        implied = implied_master(volume, self._trims[speaker_id], self._room_scale(zone.room_id))
        if abs(implied - self.state.master) <= self.config.tunables.sync_threshold:
            return
        self.state.master = implied
        # The reporting speaker is already at v: adopt, no ramp (4.3). The
        # 0-duration override only fires if clamping made desired differ.
        speaker_state.commanded = volume
        self._reconcile(plan, self.config.tunables.rebalance_fade, {speaker_id: 0.0})

    # ------------------------------------------------------------------
    # Duck inputs
    # ------------------------------------------------------------------

    def _on_duck(self, event: DuckChanged, now: float, plan: _Plan) -> None:
        duck = next((d for d in self.config.duck_inputs if d.input_id == event.input_id), None)
        if duck is None:  # 10.4
            return
        changed = bool(self.state.duck_active.get(duck.input_id, False)) != event.active
        self.state.duck_active[duck.input_id] = event.active
        if not changed:
            return
        self._mode_change_at = now  # duck change (rule 4.1)
        if not self.state.enabled:
            return
        duration = duck.engage_fade if event.active else duck.release_fade
        self._reconcile(plan, duration)

    # ------------------------------------------------------------------
    # TV solo / trim / playback / keep-grouped
    # ------------------------------------------------------------------

    def _on_set_tv_solo(self, event: SetTvSolo, now: float, plan: _Plan) -> None:
        self.state.tv_solo = event.enabled  # 6.3
        if not self.state.enabled:
            self._update_suppression(now)
            return
        self._finish(plan, now, self.config.tunables.rebalance_fade, {})

    def _on_set_trim(self, event: SetTrim, plan: _Plan) -> None:
        if event.speaker_id not in self._trims:  # 10.4
            return
        self._trims[event.speaker_id] = max(0.0, event.trim)  # 10.1
        self._reconcile(plan, self.config.tunables.rebalance_fade)

    def _on_playback(self, event: PlaybackChanged) -> None:
        speaker_state = self.state.speakers.get(event.speaker_id)
        if speaker_state is not None:  # 10.3: diagnostics only
            speaker_state.playing = event.playing

    def _on_set_keep_grouped(self, event: SetKeepGrouped, plan: _Plan) -> None:
        self.state.keep_grouped = event.enabled
        if not self.state.enabled:
            return
        if event.enabled:
            self._evaluate_group_repair(plan)  # 7.2
        else:
            plan.cancel_timer(timers.GROUP_REPAIR)  # 7.4

    # ------------------------------------------------------------------
    # Group repair (rule 7)
    # ------------------------------------------------------------------

    def _on_group_members(self, event: GroupMembersReported, plan: _Plan) -> None:
        speaker_state = self.state.speakers.get(event.speaker_id)
        if speaker_state is None:  # 10.4
            return
        speaker_state.group_members = tuple(event.members)
        self._evaluate_group_repair(plan)  # 7.2 (self-gates)

    def _evaluate_group_repair(self, plan: _Plan) -> None:
        """Rule 7.2: schedule or cancel the repair timer from observed topology."""
        if not self.state.enabled or not self.state.keep_grouped:
            return
        if self._group_missing():
            plan.start_timer(timers.GROUP_REPAIR, self.config.tunables.group_repair_delay)
        else:
            plan.cancel_timer(timers.GROUP_REPAIR)

    def _on_repair_fired(self, plan: _Plan) -> None:
        if not self.state.enabled or not self.state.keep_grouped:
            return
        missing = self._group_missing()
        if missing:
            plan.join(self.config.leader_id(), missing)  # 7.3, once

    def _group_missing(self) -> tuple[str, ...] | None:
        """Expected members missing from the leader's observed group.

        Returns ``None`` when repair is not applicable: the leader is
        STANDALONE (skip entirely) or the topology is unknown (no report
        mentions the leader). Only membership matters, not who leads (7.1).
        """
        leader = self.config.leader_id()
        if leader not in self.state.speakers or self._is_standalone_speaker(leader):
            return None
        observed: frozenset[str] | None = None
        leader_members = self.state.speakers[leader].group_members
        if leader_members:
            observed = frozenset(leader_members) | {leader}
        else:
            for speaker in self.config.speakers:
                members = self.state.speakers[speaker.speaker_id].group_members
                if leader in members:
                    observed = frozenset(members) | {speaker.speaker_id}
                    break
        if observed is None:
            return None
        return tuple(
            speaker.speaker_id
            for speaker in self.config.speakers
            if speaker.speaker_id != leader
            and not self._is_standalone_speaker(speaker.speaker_id)
            and speaker.speaker_id not in observed
        )

    # ------------------------------------------------------------------
    # Enable / disable (rule 8)
    # ------------------------------------------------------------------

    def _on_set_enabled(self, event: SetEnabled, now: float, plan: _Plan) -> None:
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
                    self._recompute_phase(zone, now)
            return
        self.state.enabled = True
        # 8.2: recompute all zone phases from current inputs.
        for zone in self.config.zones:
            self._recompute_phase(zone, now)
        # Adopt reality before converging: the world may have drifted while
        # we were not writing (external reports updated .volume only).
        for speaker_state in self.state.speakers.values():
            speaker_state.commanded = speaker_state.volume
        self._update_suppression(now)
        self._sync_fallback(now, {})
        self._reconcile(plan, self.config.tunables.rebalance_fade)  # uniform (8.2)
        self._evaluate_group_repair(plan)  # re-arm

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def _on_timer_fired(self, event: TimerFired, now: float, plan: _Plan) -> None:
        timer_id = event.timer_id
        if timer_id not in self._pending_timers:
            return  # unknown or stale timer id (10.2)
        self._pending_timers.discard(timer_id)
        if timer_id.startswith(timers.ZONE_RELEASE_PREFIX):
            self._on_release_fired(timer_id.removeprefix(timers.ZONE_RELEASE_PREFIX), now, plan)
        elif timer_id.startswith(timers.EXTERNAL_DEBOUNCE_PREFIX):
            self._on_debounce_fired(
                timer_id.removeprefix(timers.EXTERNAL_DEBOUNCE_PREFIX), now, plan
            )
        elif timer_id == timers.GROUP_REPAIR:
            self._on_repair_fired(plan)

    def _ordered_pending_timers(self) -> list[str]:
        """Pending timer ids in a deterministic, config-declaration order."""
        ordered = [timers.zone_release(z.zone_id) for z in self.config.zones]
        ordered += [timers.external_debounce(s.speaker_id) for s in self.config.speakers]
        ordered.append(timers.GROUP_REPAIR)
        return [t for t in ordered if t in self._pending_timers]

    # ------------------------------------------------------------------
    # Reconciliation (spec section 0) and derived state
    # ------------------------------------------------------------------

    def _finish(
        self, plan: _Plan, now: float, default_duration: float, overrides: dict[str, float]
    ) -> None:
        """Common tail: refresh derived state, then reconcile."""
        self._update_suppression(now)
        self._sync_fallback(now, overrides)
        self._reconcile(plan, default_duration, overrides)

    def _reconcile(
        self, plan: _Plan, default_duration: float, overrides: dict[str, float] | None = None
    ) -> None:
        """Emit RampVolume for every speaker whose desired != commanded."""
        if not self.state.enabled:
            return  # inert while disabled (8.1)
        for speaker in self.config.speakers:  # 10.5: config declaration order
            sid = speaker.speaker_id
            desired = self._desired(sid)
            if desired is None:
                continue
            speaker_state = self.state.speakers[sid]
            if volumes_equal(desired, speaker_state.commanded):
                continue
            duration = default_duration
            if overrides is not None:
                duration = overrides.get(sid, default_duration)
            plan.ramp(sid, desired, duration)
            speaker_state.commanded = desired
            # 4.4: our write supersedes any stale external report.
            speaker_state.pending_external = None
            plan.cancel_timer(timers.external_debounce(sid))

    def _desired(self, speaker_id: str) -> float | None:
        """Desired volume per spec section 0 (None = do not touch)."""
        if not self.state.enabled:
            return None
        zone = self.config.zone_for_speaker(speaker_id)
        if zone is None:
            return None  # unmanaged speaker: never touch it
        if self.state.zones[zone.zone_id].phase is ZonePhase.STANDALONE:
            return None
        if not self._is_audible(zone.zone_id):
            return 0.0
        target = speaker_target(
            self.state.master, self._trims[speaker_id], self._room_scale(zone.room_id)
        )
        cap = self._duck_cap()
        return target if cap is None else min(target, cap)

    def _is_audible(self, zone_id: str) -> bool:
        """audible(zone) per spec section 0: phase in {ACTIVE, RELEASING} and
        not solo-suppressed. Fallback forcing is materialized in phase."""
        zone_state = self.state.zones[zone_id]
        return zone_state.phase in _AUDIBLE_PHASES and zone_id not in self._suppressed

    def _room_scale(self, room_id: str) -> float:
        audible = [z for z in self.config.zones_in_room(room_id) if self._is_audible(z.zone_id)]
        tv = any(self.state.zones[z.zone_id].tv_playing for z in audible)
        return room_scale(len(audible), tv)

    def _duck_cap(self) -> float | None:
        caps = [
            d.duck_volume for d in self.config.duck_inputs if self.state.duck_active.get(d.input_id)
        ]
        return min(caps) if caps else None

    def _compute_suppressed(self) -> frozenset[str]:
        """Zone ids solo-suppressed per rule 6.2."""
        if not self.state.tv_solo:
            return frozenset()
        tv_rooms = {z.room_id for z in self.config.zones if self.state.zones[z.zone_id].tv_playing}
        if not tv_rooms:
            return frozenset()
        return frozenset(z.zone_id for z in self.config.zones if z.room_id not in tv_rooms)

    def _update_suppression(self, now: float) -> None:
        suppressed = self._compute_suppressed()
        if suppressed != self._suppressed:
            self._suppressed = suppressed
            self._mode_change_at = now  # counts as a transition (6.2)

    def _sync_fallback(self, now: float, overrides: dict[str, float]) -> None:
        """Rule 1.5: materialize fallback forcing into the published phase."""
        zone = self._fallback_zone()
        if zone is None or not self.state.enabled:
            return
        zone_state = self.state.zones[zone.zone_id]
        if zone_state.phase is ZonePhase.STANDALONE:
            self._fallback_forced = False
            return
        effective = zone_state.occupied or zone_state.tv_playing
        others_audible = any(
            self._is_audible(z.zone_id) for z in self.config.zones if z.zone_id != zone.zone_id
        )
        if zone_state.phase is ZonePhase.IDLE and not others_audible:
            self._set_phase(zone.zone_id, ZonePhase.ACTIVE, now)
            self._fallback_forced = not effective
            overrides.setdefault(zone.speaker_id, self.config.tunables.fade_in)
        elif zone_state.phase is ZonePhase.ACTIVE and self._fallback_forced:
            if effective:
                self._fallback_forced = False  # earned its audibility
            elif others_audible:
                # Returns to IDLE the moment another zone becomes audible.
                self._set_phase(zone.zone_id, ZonePhase.IDLE, now)
                self._fallback_forced = False
                overrides.setdefault(zone.speaker_id, self.config.tunables.fade_out)

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _set_phase(self, zone_id: str, phase: ZonePhase, now: float) -> None:
        """Set a zone phase; stamp last_transition only when audibility flips
        (rule 1.1: RELEASING<->ACTIVE does not count)."""
        zone_state = self.state.zones[zone_id]
        if zone_state.phase is phase:
            return
        was_audible = zone_state.phase in _AUDIBLE_PHASES
        zone_state.phase = phase
        if was_audible != (phase in _AUDIBLE_PHASES):
            zone_state.last_transition = now

    def _recompute_phase(self, zone: ZoneConfig, now: float) -> None:
        """Canonical phase from current inputs (8.2 enable, and while
        disabled so the world model stays fresh). No RELEASING here: hold
        timers cannot run in these modes."""
        zone_state = self.state.zones[zone.zone_id]
        if not self.state.speakers[zone.speaker_id].docked:
            self._set_phase(zone.zone_id, ZonePhase.STANDALONE, now)
        elif zone_state.occupied or zone_state.tv_playing:
            self._set_phase(zone.zone_id, ZonePhase.ACTIVE, now)
        else:
            self._set_phase(zone.zone_id, ZonePhase.IDLE, now)

    def _zone_config(self, zone_id: str) -> ZoneConfig | None:
        return next((z for z in self.config.zones if z.zone_id == zone_id), None)

    def _fallback_zone(self) -> ZoneConfig | None:
        return next((z for z in self.config.zones if z.fallback), None)

    def _is_standalone_speaker(self, speaker_id: str) -> bool:
        zone = self.config.zone_for_speaker(speaker_id)
        if zone is not None:
            return self.state.zones[zone.zone_id].phase is ZonePhase.STANDALONE
        return not self.state.speakers[speaker_id].docked
