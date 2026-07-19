"""The zone FSM: lifecycle, docking, and fallback (spec rules 1, 2, 6.3).

Owns every phase transition: occupancy/TV-driven IDLE/ACTIVE/RELEASING
moves (rule 1), dock-driven STANDALONE moves (rule 2 — STANDALONE is a
:class:`~.model.ZonePhase`, so docking lives here deliberately), fallback
forcing (rule 1.5), and the TV-solo mode setting (rule 6.3) that feeds
zone suppression.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import reconcile, timers
from .events import (
    ActivityChanged,
    DockChanged,
    HomePresenceChanged,
    OccupancyChanged,
    SetFollowMode,
    SetTvSoloMode,
    TvPlayingChanged,
)
from .grouping import evaluate_group_repair
from .model import (
    FollowMode,
    PresenceActivity,
    SpeakerState,
    ZoneConfig,
    ZonePhase,
    ZoneState,
    max_activity,
)
from .reconcile import AUDIBLE_PHASES

if TYPE_CHECKING:
    from .engine import ConductorEngine
    from .plan import Plan

# ---------------------------------------------------------------------
# Zone lifecycle (rule 1) + TV occupancy (1.4/6.1)
# ---------------------------------------------------------------------


def on_occupancy(engine: ConductorEngine, event: OccupancyChanged, now: float, plan: Plan) -> None:
    zone = engine._zone_config(event.zone_id)
    if zone is None:  # 10.4
        return
    engine.state.zones[zone.zone_id].occupied = event.occupied
    # Under PER_ROOM one zone's occupancy changes its room-mates' effective
    # occupancy (rule 1.9), so every zone is re-evaluated, not just this one.
    # For PER_ZONE/ALL_SPEAKERS the extra passes are idempotent no-ops.
    if not engine.state.enabled:
        recompute_all_phases(engine, now)
        return
    overrides: dict[str, float] = {}
    apply_all_zone_inputs(engine, now, plan, overrides)
    engine._finish(plan, now, engine.config.tunables.rebalance_fade, overrides)


def on_activity(engine: ConductorEngine, event: ActivityChanged, now: float) -> None:
    """Rule 1.7: track activity and raise the episode peak while audible.

    State-only — activity never changes audibility by itself (occupancy
    does); it selects the hold-time scale when release begins (rule 1.2).
    """
    zone = engine._zone_config(event.zone_id)
    if zone is None:  # 10.4
        return
    zone_state = engine.state.zones[zone.zone_id]
    zone_state.activity = event.activity
    if zone_state.phase is ZonePhase.ACTIVE:
        zone_state.episode_peak = max_activity(zone_state.episode_peak, event.activity)


def on_home_presence(
    engine: ConductorEngine, event: HomePresenceChanged, now: float, plan: Plan
) -> None:
    """Rule 1.8: home-level presence gates fallback forcing."""
    engine.state.anyone_home = event.present
    if not engine.state.enabled:
        return
    engine._finish(plan, now, engine.config.tunables.rebalance_fade, {})


def on_tv_playing(engine: ConductorEngine, event: TvPlayingChanged, now: float, plan: Plan) -> None:
    zone = engine._zone_config(event.zone_id)
    if zone is None:  # 10.4
        return
    zone_state = engine.state.zones[zone.zone_id]
    if zone_state.tv_playing != event.playing:
        zone_state.tv_playing = event.playing
        engine._mode_change_at = now  # TV-mode change (rules 4.1, 6.2)
    if not engine.state.enabled:
        recompute_phase(engine, zone, now)
        reconcile.update_suppression(engine, now)
        return
    overrides: dict[str, float] = {}
    apply_zone_inputs(engine, zone, now, plan, overrides)
    engine._finish(plan, now, engine.config.tunables.rebalance_fade, overrides)


def follow_occupied(engine: ConductorEngine, zone: ZoneConfig) -> bool:
    """Effective occupancy that drives a zone ACTIVE (rules 1.4, 1.9).

    A zone's own ``tv_playing`` always counts (rule 1.4), independent of the
    follow mode. Otherwise the follow mode selects the granularity:

    - PER_ZONE: the zone's own ``occupied`` (per-speaker follow-me).
    - PER_ROOM: any zone sharing this zone's ``room_id`` is ``occupied``.
    - ALL_SPEAKERS: always audible.
    """
    zone_state = engine.state.zones[zone.zone_id]
    if zone_state.tv_playing:
        return True
    mode = engine.state.follow_mode
    if mode is FollowMode.ALL_SPEAKERS:
        return True
    if mode is FollowMode.PER_ROOM:
        return any(
            engine.state.zones[z.zone_id].occupied
            for z in engine.config.zones_in_room(zone.room_id)
        )
    return zone_state.occupied  # PER_ZONE


def apply_all_zone_inputs(
    engine: ConductorEngine, now: float, plan: Plan, overrides: dict[str, float]
) -> None:
    """Re-run the FSM transitions for every zone against its effective
    occupancy. Needed because follow mode (rule 1.9) lets one zone's inputs
    change another zone's audibility; idempotent for unaffected zones."""
    for zone in engine.config.zones:
        apply_zone_inputs(engine, zone, now, plan, overrides)


def apply_zone_inputs(
    engine: ConductorEngine, zone: ZoneConfig, now: float, plan: Plan, overrides: dict[str, float]
) -> None:
    """Run the IDLE/ACTIVE/RELEASING transitions for effective occupancy.

    Effective occupancy is ``follow_occupied`` (rules 1.4, 1.9).
    """
    zone_state = engine.state.zones[zone.zone_id]
    if zone_state.phase is ZonePhase.STANDALONE:  # dock rules own this phase
        return
    if follow_occupied(engine, zone):
        _activate(engine, zone, zone_state, now, plan, overrides)
    else:
        _begin_release(engine, zone, zone_state, now, plan)


def _activate(
    engine: ConductorEngine,
    zone: ZoneConfig,
    zone_state: ZoneState,
    now: float,
    plan: Plan,
    overrides: dict[str, float],
) -> None:
    """Effective occupancy present: IDLE/RELEASING move to ACTIVE."""
    if zone_state.phase is ZonePhase.IDLE:
        plan.cancel_timer(timers.zone_release(zone.zone_id))  # 1.1 (idempotent)
        set_phase(engine, zone.zone_id, ZonePhase.ACTIVE, now)
        zone_state.episode_peak = zone_state.activity  # new episode (1.7)
        overrides[zone.speaker_id] = engine.config.tunables.fade_in
    elif zone_state.phase is ZonePhase.RELEASING:
        plan.cancel_timer(timers.zone_release(zone.zone_id))  # 1.1
        set_phase(engine, zone.zone_id, ZonePhase.ACTIVE, now)  # no volume effect
        # Same audible episode: the peak carries over the flicker (1.7).
        zone_state.episode_peak = max_activity(zone_state.episode_peak, zone_state.activity)
    if zone.fallback:
        # A forced-active fallback zone that becomes occupied (or
        # gets a TV) now holds its audibility on its own merits.
        engine._fallback_forced = False


def _begin_release(
    engine: ConductorEngine, zone: ZoneConfig, zone_state: ZoneState, now: float, plan: Plan
) -> None:
    """Effective occupancy lost: an ACTIVE zone starts its hold timer."""
    if zone_state.phase is not ZonePhase.ACTIVE:
        return
    if zone.fallback and engine._fallback_forced:
        return
    set_phase(engine, zone.zone_id, ZonePhase.RELEASING, now)  # 1.2
    hold = zone.hold_seconds * _hold_scale(engine, zone_state)
    plan.start_timer(timers.zone_release(zone.zone_id), hold)


def _hold_scale(engine: ConductorEngine, zone_state: ZoneState) -> float:
    """Activity-scaled hold time (rule 1.2): the episode peak decides how
    long a vacated zone stays audible. No activity information = 1.0."""
    peak = zone_state.episode_peak
    if peak is PresenceActivity.SETTLED:
        return engine.config.tunables.hold_settled_scale
    if peak is PresenceActivity.PASSING:
        return engine.config.tunables.hold_passing_scale
    return 1.0


def on_release_fired(engine: ConductorEngine, zone_id: str, now: float, plan: Plan) -> None:
    zone = engine._zone_config(zone_id)
    if zone is None:  # 10.2/10.4
        return
    if engine.state.zones[zone_id].phase is not ZonePhase.RELEASING:
        return  # stale release timer (1.3)
    set_phase(engine, zone_id, ZonePhase.IDLE, now)
    overrides = {zone.speaker_id: engine.config.tunables.fade_out}
    engine._finish(plan, now, engine.config.tunables.rebalance_fade, overrides)


# ---------------------------------------------------------------------
# Dock / standalone (rule 2)
# ---------------------------------------------------------------------


def on_dock(engine: ConductorEngine, event: DockChanged, now: float, plan: Plan) -> None:
    speaker_state = engine.state.speakers.get(event.speaker_id)
    if speaker_state is None:  # 10.4
        return
    speaker_state.docked = event.docked
    zone = engine.config.zone_for_speaker(event.speaker_id)
    if not engine.state.enabled:
        if zone is not None:
            recompute_phase(engine, zone, now)
        return
    overrides: dict[str, float] = {}
    if zone is not None:
        if event.docked:
            _redock(engine, zone, speaker_state, now, overrides)
        else:
            _undock(engine, zone, speaker_state, now, plan)
    engine._finish(plan, now, engine.config.tunables.rebalance_fade, overrides)
    evaluate_group_repair(engine, plan)  # 7.2 trigger


def _undock(
    engine: ConductorEngine,
    zone: ZoneConfig,
    speaker_state: SpeakerState,
    now: float,
    plan: Plan,
) -> None:
    """Undocked speaker leaves conductor control (rule 2.1)."""
    if engine.state.zones[zone.zone_id].phase is ZonePhase.STANDALONE:
        return
    plan.cancel_timer(timers.zone_release(zone.zone_id))  # 2.1
    set_phase(engine, zone.zone_id, ZonePhase.STANDALONE, now)
    if zone.fallback:
        engine._fallback_forced = False
    # The user takes the speaker over at its current volume;
    # we no longer own a commanded target for it (2.1).
    speaker_state.commanded = None


def _redock(
    engine: ConductorEngine,
    zone: ZoneConfig,
    speaker_state: SpeakerState,
    now: float,
    overrides: dict[str, float],
) -> None:
    """Re-docked speaker rejoins conductor control (rule 2.2)."""
    zone_state = engine.state.zones[zone.zone_id]
    if zone_state.phase is not ZonePhase.STANDALONE:
        return
    # 2.2: recompute from current inputs as if they just changed.
    speaker_state.commanded = None  # take ownership back fresh
    if follow_occupied(engine, zone):
        set_phase(engine, zone.zone_id, ZonePhase.ACTIVE, now)
        # A redock starts a new activity episode (1.7): whatever peaked
        # before the undock belongs to a visit the conductor did not own.
        zone_state.episode_peak = zone_state.activity
        overrides[zone.speaker_id] = engine.config.tunables.fade_in
    else:
        set_phase(engine, zone.zone_id, ZonePhase.IDLE, now)


# ---------------------------------------------------------------------
# Fallback forcing (rule 1.5)
# ---------------------------------------------------------------------


def sync_fallback(engine: ConductorEngine, now: float, overrides: dict[str, float]) -> None:
    """Rule 1.5: materialize fallback forcing into the published phase."""
    zone = engine._fallback_zone()
    if zone is None or not engine.state.enabled:
        return
    zone_state = engine.state.zones[zone.zone_id]
    if zone_state.phase is ZonePhase.STANDALONE:
        engine._fallback_forced = False
        return
    if zone_state.phase is ZonePhase.IDLE:
        _force_fallback_active(engine, zone, zone_state, now, overrides)
    elif zone_state.phase is ZonePhase.ACTIVE and engine._fallback_forced:
        _retire_forced_fallback(engine, zone, zone_state, now, overrides)


def _others_audible(engine: ConductorEngine, zone: ZoneConfig) -> bool:
    """Is any zone other than ``zone`` audible?"""
    return any(
        reconcile.is_audible(engine, z.zone_id)
        for z in engine.config.zones
        if z.zone_id != zone.zone_id
    )


def _force_fallback_active(
    engine: ConductorEngine,
    zone: ZoneConfig,
    zone_state: ZoneState,
    now: float,
    overrides: dict[str, float],
) -> None:
    """An IDLE fallback zone goes ACTIVE when nothing else is audible."""
    if engine.state.anyone_home is False:  # 1.8: empty home, no forcing
        return
    if _others_audible(engine, zone):
        return
    effective = follow_occupied(engine, zone)
    set_phase(engine, zone.zone_id, ZonePhase.ACTIVE, now)
    engine._fallback_forced = not effective
    overrides.setdefault(zone.speaker_id, engine.config.tunables.fade_in)


def _retire_forced_fallback(
    engine: ConductorEngine,
    zone: ZoneConfig,
    zone_state: ZoneState,
    now: float,
    overrides: dict[str, float],
) -> None:
    """A forced-ACTIVE fallback zone earns its audibility or steps aside."""
    if follow_occupied(engine, zone):
        engine._fallback_forced = False  # earned its audibility
        return
    # 1.8: forcing is unearned audibility; an empty home retires it even
    # though nothing else is audible (silence is the point).
    if engine.state.anyone_home is not False and not _others_audible(engine, zone):
        return
    # Returns to IDLE the moment another zone becomes audible.
    set_phase(engine, zone.zone_id, ZonePhase.IDLE, now)
    engine._fallback_forced = False
    overrides.setdefault(zone.speaker_id, engine.config.tunables.fade_out)


# ---------------------------------------------------------------------
# TV solo (rule 6.3)
# ---------------------------------------------------------------------


def on_set_tv_solo_mode(
    engine: ConductorEngine, event: SetTvSoloMode, now: float, plan: Plan
) -> None:
    engine.state.tv_solo_mode = event.mode  # 6.3
    if not engine.state.enabled:
        reconcile.update_suppression(engine, now)
        return
    engine._finish(plan, now, engine.config.tunables.rebalance_fade, {})


# ---------------------------------------------------------------------
# Follow mode (rule 1.9)
# ---------------------------------------------------------------------


def on_set_follow_mode(
    engine: ConductorEngine, event: SetFollowMode, now: float, plan: Plan
) -> None:
    """Rule 1.9: switch the presence granularity a zone follows.

    Zones that gain effective occupancy fade in; zones that lose it start
    the normal hold/RELEASING flow. Orthogonal to TV-solo suppression.
    """
    engine.state.follow_mode = event.mode
    if not engine.state.enabled:
        recompute_all_phases(engine, now)  # keep the world model fresh (8.1)
        return
    overrides: dict[str, float] = {}
    apply_all_zone_inputs(engine, now, plan, overrides)
    engine._finish(plan, now, engine.config.tunables.rebalance_fade, overrides)


# ---------------------------------------------------------------------
# Phase bookkeeping
# ---------------------------------------------------------------------


def set_phase(engine: ConductorEngine, zone_id: str, phase: ZonePhase, now: float) -> None:
    """Set a zone phase; stamp last_transition only when audibility flips
    (rule 1.1: RELEASING<->ACTIVE does not count)."""
    zone_state = engine.state.zones[zone_id]
    if zone_state.phase is phase:
        return
    was_audible = zone_state.phase in AUDIBLE_PHASES
    zone_state.phase = phase
    if was_audible != (phase in AUDIBLE_PHASES):
        zone_state.last_transition = now


def recompute_all_phases(engine: ConductorEngine, now: float) -> None:
    """Canonical phase for every zone (follow mode couples zones)."""
    for zone in engine.config.zones:
        recompute_phase(engine, zone, now)


def recompute_phase(engine: ConductorEngine, zone: ZoneConfig, now: float) -> None:
    """Canonical phase from current inputs (8.2 enable, and while
    disabled so the world model stays fresh). No RELEASING here: hold
    timers cannot run in these modes."""
    zone_state = engine.state.zones[zone.zone_id]
    if not engine.state.speakers[zone.speaker_id].docked:
        set_phase(engine, zone.zone_id, ZonePhase.STANDALONE, now)
    elif follow_occupied(engine, zone):
        was_audible = zone_state.phase in AUDIBLE_PHASES
        set_phase(engine, zone.zone_id, ZonePhase.ACTIVE, now)
        # Episode-peak bookkeeping mirrors _activate (rule 1.7).
        if was_audible:
            zone_state.episode_peak = max_activity(zone_state.episode_peak, zone_state.activity)
        else:
            zone_state.episode_peak = zone_state.activity
    else:
        set_phase(engine, zone.zone_id, ZonePhase.IDLE, now)
