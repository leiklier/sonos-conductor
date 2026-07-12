"""Reconciliation and derived state (spec section 0, plus 6.2 suppression).

The only path that emits volume effects: every event handler updates state,
then reconciles desired-vs-commanded for all speakers. Also home to the
derived predicates that reconciliation is defined over — audibility, room
scale, duck cap, and the TV-solo suppression set (rule 6.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import timers, volume_math
from .model import TvSoloMode, ZonePhase

if TYPE_CHECKING:
    from .engine import ConductorEngine
    from .plan import Plan

AUDIBLE_PHASES = frozenset((ZonePhase.ACTIVE, ZonePhase.RELEASING))


def reconcile(
    engine: ConductorEngine,
    plan: Plan,
    default_duration: float,
    overrides: dict[str, float] | None = None,
) -> None:
    """Emit RampVolume for every speaker whose desired != commanded."""
    if not engine.state.enabled:
        return  # inert while disabled (8.1)
    for speaker in engine.config.speakers:  # 10.5: config declaration order
        sid = speaker.speaker_id
        target = desired(engine, sid)
        if target is None:
            continue
        speaker_state = engine.state.speakers[sid]
        if volume_math.volumes_equal(target, speaker_state.commanded):
            continue
        duration = default_duration
        if overrides is not None:
            duration = overrides.get(sid, default_duration)
        plan.ramp(sid, target, duration)
        speaker_state.commanded = target
        # 4.4: our write supersedes any stale external report.
        speaker_state.pending_external = None
        plan.cancel_timer(timers.external_debounce(sid))


def desired(engine: ConductorEngine, speaker_id: str) -> float | None:
    """Desired volume per spec section 0 (None = do not touch)."""
    if not engine.state.enabled:
        return None
    zone = engine.config.zone_for_speaker(speaker_id)
    if zone is None:
        return None  # unmanaged speaker: never touch it
    if engine.state.zones[zone.zone_id].phase is ZonePhase.STANDALONE:
        return None
    if not is_audible(engine, zone.zone_id):
        return 0.0
    target = volume_math.speaker_target(
        engine.state.master, engine._trims[speaker_id], room_scale(engine, zone.room_id)
    )
    cap = duck_cap(engine)
    return target if cap is None else min(target, cap)


def is_audible(engine: ConductorEngine, zone_id: str) -> bool:
    """audible(zone) per spec section 0: phase in {ACTIVE, RELEASING} and
    not solo-suppressed. Fallback forcing is materialized in phase."""
    zone_state = engine.state.zones[zone_id]
    return zone_state.phase in AUDIBLE_PHASES and zone_id not in engine._suppressed


def room_scale(engine: ConductorEngine, room_id: str) -> float:
    audible = [z for z in engine.config.zones_in_room(room_id) if is_audible(engine, z.zone_id)]
    tv = any(engine.state.zones[z.zone_id].tv_playing for z in audible)
    return volume_math.room_scale(len(audible), tv)


def duck_cap(engine: ConductorEngine) -> float | None:
    caps = [
        d.duck_volume for d in engine.config.duck_inputs if engine.state.duck_active.get(d.input_id)
    ]
    return min(caps) if caps else None


def compute_suppressed(engine: ConductorEngine) -> frozenset[str]:
    """Zone ids solo-suppressed per rule 6.2 (empty unless a TV is playing)."""
    mode = engine.state.tv_solo_mode
    if mode is TvSoloMode.OFF:
        return frozenset()
    tv_zones = {z.zone_id for z in engine.config.zones if engine.state.zones[z.zone_id].tv_playing}
    if not tv_zones:
        return frozenset()
    if mode is TvSoloMode.TV_ZONE:
        return frozenset(z.zone_id for z in engine.config.zones if z.zone_id not in tv_zones)
    tv_rooms = {z.room_id for z in engine.config.zones if z.zone_id in tv_zones}  # SAME_ROOM
    return frozenset(z.zone_id for z in engine.config.zones if z.room_id not in tv_rooms)


def update_suppression(engine: ConductorEngine, now: float) -> None:
    suppressed = compute_suppressed(engine)
    if suppressed != engine._suppressed:
        engine._suppressed = suppressed
        engine._mode_change_at = now  # counts as a transition (6.2)
