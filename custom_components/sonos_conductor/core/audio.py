"""Master volume, mute, reverse sync, ducking, and trims (spec rules 3-5, 10.1).

Everything that turns user or external audio input into state: the master
setter (rule 3), external volume reports and their debounced reverse sync
(rule 4), global mute and its fan-out (rule 5), duck inputs, and runtime
trim adjustment (rule 10.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import reconcile, timers
from .events import (
    DuckChanged,
    ExternalMute,
    ExternalVolume,
    SetMaster,
    SetMute,
    SetTrim,
)
from .volume_math import clamp, implied_master

if TYPE_CHECKING:
    from .engine import ConductorEngine
    from .plan import Plan

#: External volume reports at or below this are never trusted for reverse
#: sync (rule 4.1 hard-zero guard).
_HARD_ZERO = 0.01

# ---------------------------------------------------------------------
# Master / mute (rules 3, 5)
# ---------------------------------------------------------------------


def on_set_master(engine: ConductorEngine, event: SetMaster, plan: Plan) -> None:
    engine.state.master = clamp(event.value)  # 3.1
    if not engine.state.enabled or engine.state.muted:
        return  # store only; reconcile happens on unmute / enable
    reconcile.reconcile(engine, plan, engine.config.tunables.master_fade)


def on_set_mute(engine: ConductorEngine, event: SetMute, plan: Plan) -> None:
    if not engine.state.enabled:
        engine.state.muted = event.muted  # state stays fresh (8.1)
        return
    apply_global_mute(engine, event.muted, plan)


def on_external_mute(engine: ConductorEngine, event: ExternalMute, plan: Plan) -> None:
    speaker_state = engine.state.speakers.get(event.speaker_id)
    if speaker_state is None:  # 10.4
        return
    speaker_state.muted = event.muted
    if (
        not engine.state.enabled
        or engine._is_standalone_speaker(event.speaker_id)
        or event.muted == engine.state.muted
    ):
        return  # 5.3 applies only to differing non-STANDALONE reports
    apply_global_mute(engine, event.muted, plan)


def apply_global_mute(engine: ConductorEngine, muted: bool, plan: Plan) -> None:
    engine.state.muted = muted
    for speaker in engine.config.speakers:  # 5.1/5.2 fan-out, config order
        if engine._is_standalone_speaker(speaker.speaker_id):
            continue
        plan.mute(speaker.speaker_id, muted)
        engine.state.speakers[speaker.speaker_id].muted = muted
    if not muted:
        # 5.2: master may have changed while muted.
        reconcile.reconcile(engine, plan, engine.config.tunables.rebalance_fade)


# ---------------------------------------------------------------------
# External volume reports / reverse sync (rule 4)
# ---------------------------------------------------------------------


def on_external_volume(
    engine: ConductorEngine, event: ExternalVolume, now: float, plan: Plan
) -> None:
    speaker_state = engine.state.speakers.get(event.speaker_id)
    if speaker_state is None:  # 10.4
        return
    speaker_state.volume = event.volume  # 4.1: always update
    if not sync_allowed(engine, event.speaker_id, event.volume, now):
        return  # report discarded
    speaker_state.pending_external = event.volume  # 4.2: debounce
    plan.start_timer(
        timers.external_debounce(event.speaker_id),
        engine.config.tunables.external_debounce,
    )


def sync_allowed(engine: ConductorEngine, speaker_id: str, volume: float, now: float) -> bool:
    """Rule 4.1 acceptance conditions (also re-checked at fire time)."""
    state = engine.state
    if not state.enabled or state.muted:
        return False
    if volume <= _HARD_ZERO:
        return False
    if engine._is_standalone_speaker(speaker_id):
        return False
    zone = engine.config.zone_for_speaker(speaker_id)
    if zone is None or not reconcile.is_audible(engine, zone.zone_id):
        return False
    if any(state.duck_active.get(d.input_id) for d in engine.config.duck_inputs):
        return False
    last = engine._mode_change_at
    for zone_state in state.zones.values():
        last = max(last, zone_state.last_transition)
    return now - last >= engine.config.tunables.transition_suppression


def on_debounce_fired(engine: ConductorEngine, speaker_id: str, now: float, plan: Plan) -> None:
    speaker_state = engine.state.speakers.get(speaker_id)
    if speaker_state is None:  # 10.4
        return
    volume = speaker_state.pending_external
    speaker_state.pending_external = None
    if volume is None:  # cleared by a reconciliation write (4.4)
        return
    if not sync_allowed(engine, speaker_id, volume, now):  # 4.3 re-check
        return
    zone = engine.config.zone_for_speaker(speaker_id)
    if zone is None:  # unreachable: sync_allowed guarantees a zone
        return
    implied = implied_master(
        volume, engine._trims[speaker_id], reconcile.room_scale(engine, zone.room_id)
    )
    if abs(implied - engine.state.master) <= engine.config.tunables.sync_threshold:
        return
    engine.state.master = implied
    # The reporting speaker is already at v: adopt, no ramp (4.3). The
    # 0-duration override only fires if clamping made desired differ.
    speaker_state.commanded = volume
    reconcile.reconcile(engine, plan, engine.config.tunables.rebalance_fade, {speaker_id: 0.0})


# ---------------------------------------------------------------------
# Duck inputs
# ---------------------------------------------------------------------


def on_duck(engine: ConductorEngine, event: DuckChanged, now: float, plan: Plan) -> None:
    duck = next((d for d in engine.config.duck_inputs if d.input_id == event.input_id), None)
    if duck is None:  # 10.4
        return
    changed = bool(engine.state.duck_active.get(duck.input_id, False)) != event.active
    engine.state.duck_active[duck.input_id] = event.active
    if not changed:
        return
    engine._mode_change_at = now  # duck change (rule 4.1)
    if not engine.state.enabled:
        return
    duration = duck.engage_fade if event.active else duck.release_fade
    reconcile.reconcile(engine, plan, duration)


# ---------------------------------------------------------------------
# Trim (rule 10.1)
# ---------------------------------------------------------------------


def on_set_trim(engine: ConductorEngine, event: SetTrim, plan: Plan) -> None:
    if event.speaker_id not in engine._trims:  # 10.4
        return
    engine._trims[event.speaker_id] = max(0.0, event.trim)  # 10.1
    reconcile.reconcile(engine, plan, engine.config.tunables.rebalance_fade)
