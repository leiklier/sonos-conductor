"""Idle attenuation: the background bed for non-audible zones (rule 3.4).

Topology reminder (harness): kjokken is alone in its room (trim 1.2),
spisebord (1.1) and sofakrok (1.0, fallback, TV) share the stue room.
Default snapshot: nobody home, so the fallback (sofakrok) is forced ACTIVE
at master 0.3 and everything else is silent.
"""

from __future__ import annotations

import math

import pytest

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.events import (
    DockChanged,
    DuckChanged,
    ExternalVolume,
    HomePresenceChanged,
    OccupancyChanged,
    SetIdleAttenuation,
    SetNightMode,
    SetTvSoloMode,
    TvPlayingChanged,
)
from custom_components.sonos_conductor.core.model import IdleAttenuation, TvSoloMode, ZonePhase

from .harness import (
    DOOR,
    FLOOR,
    KJOKKEN,
    SOFAKROK,
    SPISEBORD,
    Harness,
    expect_no_ramp,
    expect_no_volume_effects,
    expect_ramp,
    make_config,
    make_snapshot,
    timer_starts,
)

MASTER = 0.3


#: Stue room scale with one audible zone and one bed at the given level.
def stue_scale(bed: float) -> float:
    return 1.0 / math.sqrt(1.0 + bed * bed)


GENTLE = 0.5  # Tunables.idle_gentle_level default
BALANCED = 0.25  # Tunables.idle_balanced_level default


class TestBedLevels:
    def test_max_is_legacy_silence(self) -> None:
        h = Harness()
        effects = h.fire(SetIdleAttenuation(IdleAttenuation.MAX))
        expect_no_volume_effects(effects)  # no-op: MAX is already the default
        effects = h.fire(OccupancyChanged("kjokken", True), at=1.0)
        expect_ramp(effects, KJOKKEN, MASTER * 1.2)
        expect_no_ramp(effects, SPISEBORD)  # idle zones stay hard-silent

    def test_gentle_beds_fade_in_on_mode_change(self) -> None:
        h = Harness()
        effects = h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE))
        scale = stue_scale(GENTLE)
        # kjokken is alone in its room: bed power 0.25 <= 1, scale stays 1.
        expect_ramp(effects, KJOKKEN, GENTLE * MASTER * 1.2, duration=2.0)
        expect_ramp(effects, SPISEBORD, GENTLE * MASTER * 1.1 * scale, duration=2.0)
        # The audible fallback shares its room's power with the new bed.
        expect_ramp(effects, SOFAKROK, MASTER * scale, duration=2.0)

    def test_balanced_beds(self) -> None:
        h = Harness()
        effects = h.fire(SetIdleAttenuation(IdleAttenuation.BALANCED))
        scale = stue_scale(BALANCED)
        expect_ramp(effects, KJOKKEN, BALANCED * MASTER * 1.2)
        expect_ramp(effects, SPISEBORD, BALANCED * MASTER * 1.1 * scale)
        expect_ramp(effects, SOFAKROK, MASTER * scale)

    def test_custom_gentle_level_tunable(self) -> None:
        h = Harness(config=make_config(idle_gentle_level=0.7))
        effects = h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE))
        expect_ramp(effects, KJOKKEN, 0.7 * MASTER * 1.2)

    def test_room_power_is_constant_with_active_roommate(self) -> None:
        h = Harness()
        h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE))
        effects = h.fire(OccupancyChanged("spisebord", True), at=1.0)
        # spisebord audible unforces the fallback; sofakrok becomes a bed.
        scale = stue_scale(GENTLE)
        spisebord = expect_ramp(effects, SPISEBORD, MASTER * 1.1 * scale)
        sofakrok = expect_ramp(effects, SOFAKROK, GENTLE * MASTER * 1.0 * scale)
        # The invariant behind the weighting: total room power (normalized by
        # trim) equals one full zone.
        power = (spisebord.target / 1.1) ** 2 + (sofakrok.target / 1.0) ** 2
        assert power == pytest.approx(MASTER**2)

    def test_bed_quieter_than_one_zone_is_not_boosted(self) -> None:
        h = Harness()
        h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE))
        effects = h.fire(OccupancyChanged("kjokken", True), at=1.0)
        # Both stue zones are now beds (0.5 each): power 0.5 <= 1, scale 1.
        expect_ramp(effects, SPISEBORD, GENTLE * MASTER * 1.1)
        expect_ramp(effects, SOFAKROK, GENTLE * MASTER * 1.0)

    def test_release_lands_at_bed_level(self) -> None:
        h = Harness()
        h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE))
        h.fire(OccupancyChanged("kjokken", True), at=1.0)
        h.fire(OccupancyChanged("kjokken", False), at=10.0)
        effects = h.fire_timer(timers.zone_release("kjokken"))
        # The fade-out lands on the bed, not silence; the fallback re-forces.
        scale = stue_scale(GENTLE)
        expect_ramp(effects, KJOKKEN, GENTLE * MASTER * 1.2, duration=5.0)
        expect_ramp(effects, SOFAKROK, MASTER * scale)
        expect_ramp(effects, SPISEBORD, GENTLE * MASTER * 1.1 * scale)


class TestComposition:
    def test_duck_scales_beds(self) -> None:
        h = Harness()
        h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE))
        effects = h.fire(DuckChanged(DOOR, True), at=1.0)
        # The audible fallback ducks to the cap; beds stay their fraction of
        # the capped level instead of pinning at the cap themselves.
        expect_ramp(effects, SOFAKROK, 0.05)
        expect_ramp(effects, KJOKKEN, GENTLE * 0.05)
        expect_ramp(effects, SPISEBORD, GENTLE * 0.05)

    def test_night_scales_beds(self) -> None:
        h = Harness()
        h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE))
        effects = h.fire(SetNightMode(True), at=1.0)
        # The bed keeps its relative attenuation under the night cap: a
        # gentle bed plays half the capped active level, at any master.
        expect_ramp(effects, SOFAKROK, 0.15)
        expect_ramp(effects, KJOKKEN, GENTLE * 0.15)
        expect_ramp(effects, SPISEBORD, GENTLE * 0.15)

    def test_tv_solo_silences_beds(self) -> None:
        h = Harness()
        h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE))
        h.fire(SetTvSoloMode(TvSoloMode.TV_ZONE), at=1.0)
        effects = h.fire(TvPlayingChanged("sofakrok", True), at=2.0)
        # Solo wins over the bed: suppressed zones go hard-silent.
        expect_ramp(effects, KJOKKEN, FLOOR)
        expect_ramp(effects, SPISEBORD, FLOOR)
        assert h.state.suppressed == {"kjokken", "spisebord"}

    def test_empty_home_silences_beds(self) -> None:
        h = Harness()
        h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE))
        effects = h.fire(HomePresenceChanged(False), at=1.0)
        # Definitively empty: beds and the forced fallback all go silent.
        for speaker in (KJOKKEN, SPISEBORD, SOFAKROK):
            expect_ramp(effects, speaker, FLOOR)
        effects = h.fire(HomePresenceChanged(True), at=2.0)
        scale = stue_scale(GENTLE)
        expect_ramp(effects, KJOKKEN, GENTLE * MASTER * 1.2)
        expect_ramp(effects, SOFAKROK, MASTER * scale)

    def test_blind_home_input_keeps_beds(self) -> None:
        h = Harness()
        h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE))
        effects = h.fire(HomePresenceChanged(None), at=1.0)
        # None = estimator blind: fails safe as present, beds stay (1.8).
        expect_no_volume_effects(effects)

    def test_standalone_speaker_untouched(self) -> None:
        h = Harness()
        h.fire(DockChanged(KJOKKEN, False))
        effects = h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE), at=1.0)
        expect_no_ramp(effects, KJOKKEN)
        assert h.state.zones["kjokken"].phase is ZonePhase.STANDALONE


class TestExternalReports:
    def test_reverse_sync_ignored_from_bed_speaker(self) -> None:
        h = Harness()
        h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE))
        effects = h.fire(ExternalVolume(KJOKKEN, 0.4), at=60.0)
        # A bed zone is not audible: the report never reaches the debounce.
        assert not timer_starts(effects)
        assert h.state.master == MASTER

    def test_mode_change_suppresses_reverse_sync(self) -> None:
        h = Harness()
        h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE), at=60.0)
        # Within transition_suppression of the mode change: discarded even
        # from the audible fallback speaker.
        effects = h.fire(ExternalVolume(SOFAKROK, 0.5), at=61.0)
        assert not timer_starts(effects)

    def test_night_pull_back_applies_to_bed_speaker(self) -> None:
        h = Harness()
        h.fire(SetIdleAttenuation(IdleAttenuation.GENTLE))
        h.fire(SetNightMode(True), at=1.0)
        effects = h.fire(ExternalVolume(KJOKKEN, 0.5), at=2.0)
        # The engine wants the bed (half the night cap): one corrective
        # ramp, no debounce timer, master untouched (rule 4.5).
        expect_ramp(effects, KJOKKEN, GENTLE * 0.15)
        assert not timer_starts(effects)
        assert h.state.master == MASTER

    def test_night_report_from_silent_zone_still_ignored(self) -> None:
        h = Harness()  # MAX: idle zones are wanted silent
        h.fire(SetNightMode(True))
        effects = h.fire(ExternalVolume(KJOKKEN, 0.5), at=1.0)
        expect_no_volume_effects(effects)  # state update only, as before


class TestSeeding:
    def test_seeds_from_snapshot_and_start_converges_beds(self) -> None:
        h = Harness(snapshot=make_snapshot(idle_attenuation=IdleAttenuation.GENTLE))
        assert h.state.idle_attenuation is IdleAttenuation.GENTLE
        scale = stue_scale(GENTLE)
        expect_ramp(h.start_effects, KJOKKEN, GENTLE * MASTER * 1.2)
        expect_ramp(h.start_effects, SPISEBORD, GENTLE * MASTER * 1.1 * scale)
        expect_ramp(h.start_effects, SOFAKROK, MASTER * scale)

    def test_start_adopts_converged_beds(self) -> None:
        scale = stue_scale(GENTLE)
        h = Harness(
            snapshot=make_snapshot(
                idle_attenuation=IdleAttenuation.GENTLE,
                volumes={
                    KJOKKEN: GENTLE * MASTER * 1.2,
                    SPISEBORD: GENTLE * MASTER * 1.1 * scale,
                    SOFAKROK: MASTER * scale,
                },
            )
        )
        expect_no_volume_effects(h.start_effects)
