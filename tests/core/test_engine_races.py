"""Race regression scenarios R1-R10 from docs/ENGINE_SPEC.md."""

from __future__ import annotations

import pytest

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.effects import CancelTimer, JoinGroup, StartTimer
from custom_components.sonos_conductor.core.events import (
    DockChanged,
    DuckChanged,
    ExternalVolume,
    GroupMembersReported,
    SetMaster,
    SetMute,
    SetTvSoloMode,
    TvPlayingChanged,
)
from custom_components.sonos_conductor.core.model import TvSoloMode, ZonePhase
from custom_components.sonos_conductor.core.volume_math import implied_master

from .harness import (
    ALL_SPEAKERS,
    DOOR,
    FLOOR,
    KJOKKEN,
    SOFAKROK,
    SPISEBORD,
    STUE_2,
    Harness,
    expect_no_ramp,
    expect_ramp,
    join_effects,
    ramp_for,
    ramps,
    timer_starts,
)


def test_race_r1_fade_out_during_master_ramp() -> None:
    h = Harness()
    h.occupy("kjokken", at=0.0)
    h.vacate("kjokken", at=5.0)  # hold timer, 60 s
    h.fire(SetMaster(0.6), at=10.0)  # mid-hold master change: ramp to 0.72
    effects = h.fire_timer(timers.zone_release("kjokken"))  # t=65
    ramp = ramp_for(effects, KJOKKEN)
    assert ramp is not None and ramp.target == FLOOR  # single final ramp to the floor
    assert ramp.duration == 5.0
    assert h.state.speakers[KJOKKEN].commanded == FLOOR  # commanded consistent
    expect_ramp(effects, SOFAKROK, 0.6, duration=3.0)  # fallback at new master


def test_race_r2_occupancy_flicker_within_hold_is_silent() -> None:
    h = Harness()
    h.occupy("kjokken", at=0.0)
    off_effects = h.vacate("kjokken", at=5.0)
    on_effects = h.occupy("kjokken", at=7.0)
    assert ramps(off_effects) == [] and ramps(on_effects) == []
    assert on_effects == [CancelTimer(timers.zone_release("kjokken"))]


def test_race_r3_door_during_fade_in() -> None:
    h = Harness()
    before = expect_ramp(h.occupy("kjokken", at=0.0), KJOKKEN, 0.36, duration=3.0)
    # Door opens 1 s into the 3 s fade-in: cap applies immediately.
    effects = h.fire(DuckChanged(DOOR, True), at=1.0)
    expect_ramp(effects, KJOKKEN, 0.05, duration=0.0)
    # Door closes: the exact pre-duck target is restored.
    effects = h.fire(DuckChanged(DOOR, False), at=4.0)
    restored = expect_ramp(effects, KJOKKEN, 0.36, duration=2.0)
    assert restored.target == before.target


def test_race_r4_external_report_in_suppression_window() -> None:
    h = Harness()
    h.occupy("kjokken", at=0.0)  # transition at t=0
    effects = h.fire(ExternalVolume(KJOKKEN, 0.5), at=4.0)
    assert effects == []  # discarded: no debounce, no master change
    assert h.state.master == pytest.approx(0.3)
    assert h.state.speakers[KJOKKEN].pending_external is None


def test_race_r5_debounced_reports_single_master_update() -> None:
    h = Harness()
    h.occupy("sofakrok", at=0.0)  # forced -> owned: no transition stamp
    h.occupy("spisebord", at=0.0)  # transition at t=0
    values = (0.35, 0.38, 0.42, 0.45, 0.48)
    for i, value in enumerate(values):
        effects = h.fire(ExternalVolume(SOFAKROK, value), at=20.0 + 0.2 * i)
        assert effects == [StartTimer(timers.external_debounce(SOFAKROK), 1.5)]
    effects = h.fire_timer(timers.external_debounce(SOFAKROK))  # t=22.3
    assert h.state.master == pytest.approx(implied_master(0.48, 1.0, STUE_2))
    # Exactly one rebalance ramp (spisebord); the reporter is left alone.
    assert [r.speaker_id for r in ramps(effects)] == [SPISEBORD]
    expect_ramp(effects, SPISEBORD, 0.48 * 1.1, duration=2.0)


def test_race_r6_undock_mid_fade_then_redock() -> None:
    h = Harness()
    h.occupy("kjokken", at=0.0)  # 3 s fade-in starts
    effects = h.fire(DockChanged(KJOKKEN, False), at=1.0)  # undock mid-fade
    expect_no_ramp(effects, KJOKKEN)
    # User plays with the speaker while standalone: state only.
    assert h.fire(ExternalVolume(KJOKKEN, 0.15), at=2.0) == []
    # Redock while the kitchen is still occupied: fade-in to the correct
    # target at the current room scale.
    effects = h.fire(DockChanged(KJOKKEN, True), at=30.0)
    expect_ramp(effects, KJOKKEN, 0.36, duration=3.0)
    expect_ramp(effects, SOFAKROK, FLOOR, duration=5.0)  # fallback yields again


def test_race_r7_simultaneous_same_room_zones() -> None:
    h = Harness()
    h.occupy("sofakrok", at=0.0)
    effects = h.occupy("spisebord", at=0.1)
    # Both end at master * trim / sqrt(2).
    expect_ramp(effects, SPISEBORD, 0.3 * 1.1 * STUE_2, duration=3.0)
    expect_ramp(effects, SOFAKROK, 0.3 * 1.0 * STUE_2, duration=2.0)
    # Deactivating one rebalances the other back up.
    h.vacate("spisebord", at=10.0)
    effects = h.fire_timer(timers.zone_release("spisebord"))  # t=25
    expect_ramp(effects, SPISEBORD, FLOOR, duration=5.0)
    expect_ramp(effects, SOFAKROK, 0.3, duration=2.0)


def test_race_r8_spontaneous_dissolve_single_join_no_loop() -> None:
    h = Harness()
    effects = h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK, SPISEBORD)), at=0.0)
    assert effects == [StartTimer(timers.GROUP_REPAIR, 15.0)]
    effects = h.fire_timer(timers.GROUP_REPAIR)  # t=15
    assert effects == [JoinGroup(SOFAKROK, (KJOKKEN,))]
    # The join succeeds; its echo report must not re-trigger a repair.
    effects = h.fire(GroupMembersReported(SOFAKROK, ALL_SPEAKERS), at=16.0)
    assert effects == []
    assert join_effects(effects) == []


def test_race_r9_master_moved_while_muted() -> None:
    h = Harness()
    h.fire(SetMute(True), at=0.0)
    assert h.fire(SetMaster(0.55), at=1.0) == []  # stored only
    effects = h.fire(SetMute(False), at=2.0)
    expect_ramp(effects, SOFAKROK, 0.55, duration=2.0)  # matches the new master
    assert h.state.speakers[SOFAKROK].commanded == pytest.approx(0.55)


def test_race_r10_tv_solo_full_script() -> None:
    h = Harness()
    h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=0.0)
    # TV starts: sofakrok (fallback, already audible) keeps its volume;
    # the kitchen room is now suppressed.
    assert h.fire(TvPlayingChanged("sofakrok", True), at=1.0) == []
    # Walking into the kitchen while the TV plays keeps the kitchen silent.
    effects = h.occupy("kjokken", at=2.0)
    assert ramps(effects) == []
    assert h.state.zones["kjokken"].phase is ZonePhase.ACTIVE  # FSM ran anyway
    # TV stops: the kitchen fades in (it is occupied); sofakrok releases.
    effects = h.fire(TvPlayingChanged("sofakrok", False), at=30.0)
    expect_ramp(effects, KJOKKEN, 0.36, duration=2.0)
    assert timer_starts(effects) == [StartTimer(timers.zone_release("sofakrok"), 15.0)]
    effects = h.fire_timer(timers.zone_release("sofakrok"))  # t=45
    expect_ramp(effects, SOFAKROK, FLOOR, duration=5.0)
