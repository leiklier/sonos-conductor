"""Rule 6: TV mode (room scale) and TV solo suppression."""

from __future__ import annotations

import pytest

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.effects import StartTimer
from custom_components.sonos_conductor.core.events import SetTvSoloMode, TvPlayingChanged
from custom_components.sonos_conductor.core.model import TvSoloMode, ZonePhase

from .harness import (
    FLOOR,
    KJOKKEN,
    SOFAKROK,
    SPISEBORD,
    STUE_2,
    Harness,
    expect_no_ramp,
    expect_ramp,
    ramps,
    timer_starts,
)


class TestRule61TvMode:
    def test_rule_6_1_tv_forces_room_scale_to_unity(self) -> None:
        h = Harness()
        h.occupy("sofakrok", at=0.0)
        h.occupy("spisebord", at=1.0)  # stue at 1/sqrt(2)
        effects = h.fire(TvPlayingChanged("sofakrok", True), at=2.0)
        expect_ramp(effects, SPISEBORD, 0.3 * 1.1, duration=2.0)  # rebalance
        expect_ramp(effects, SOFAKROK, 0.3, duration=2.0)

    def test_rule_6_1_tv_activates_idle_zone_with_fade_in(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)  # fallback yields, sofakrok IDLE
        effects = h.fire(TvPlayingChanged("sofakrok", True), at=1.0)
        expect_ramp(effects, SOFAKROK, 0.3, duration=3.0)

    def test_tv_event_for_unknown_zone_ignored(self) -> None:
        h = Harness()
        assert h.fire(TvPlayingChanged("garage", True), at=0.0) == []

    def test_repeated_tv_event_is_noop(self) -> None:
        h = Harness()
        h.fire(TvPlayingChanged("sofakrok", True), at=0.0)
        assert h.fire(TvPlayingChanged("sofakrok", True), at=1.0) == []


class TestRule62SameRoom:
    def test_rule_6_2_same_room_suppresses_other_rooms(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=1.0)
        effects = h.fire(TvPlayingChanged("sofakrok", True), at=2.0)
        expect_ramp(effects, KJOKKEN, FLOOR, duration=2.0)  # suppressed
        expect_ramp(effects, SOFAKROK, 0.3, duration=3.0)  # fade_in
        assert h.state.zones["kjokken"].phase is ZonePhase.ACTIVE  # FSM untouched

    def test_rule_6_2_fsm_keeps_running_while_suppressed(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=1.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)
        effects = h.vacate("kjokken", at=3.0)
        assert timer_starts(effects) == [StartTimer(timers.zone_release("kjokken"), 60.0)]
        assert ramps(effects) == []  # already at 0
        effects = h.fire_timer(timers.zone_release("kjokken"))
        assert h.state.zones["kjokken"].phase is ZonePhase.IDLE
        assert ramps(effects) == []

    def test_rule_6_2_single_reconcile_restores_on_tv_stop(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=1.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)
        effects = h.fire(TvPlayingChanged("sofakrok", False), at=10.0)
        expect_ramp(effects, KJOKKEN, 0.36, duration=2.0)  # restored
        assert timer_starts(effects) == [StartTimer(timers.zone_release("sofakrok"), 15.0)]

    def test_rule_6_2_suppression_change_sets_mode_timestamp(self) -> None:
        from custom_components.sonos_conductor.core.events import ExternalVolume

        h = Harness()
        h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=0.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)  # suppression set changes
        # Sofakrok is audible but the fleet is "in motion" until t=12.
        assert h.fire(ExternalVolume(SOFAKROK, 0.4), at=5.0) == []

    def test_rule_6_2_same_room_zone_not_suppressed(self) -> None:
        h = Harness()
        h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=0.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=1.0)
        effects = h.occupy("spisebord", at=2.0)
        # Same room as the TV: audible, at TV-forced unity scale.
        expect_ramp(effects, SPISEBORD, 0.3 * 1.1, duration=3.0)
        expect_no_ramp(effects, SOFAKROK)


class TestRule62TvZone:
    def test_rule_6_2_tv_zone_suppresses_same_room_zone_too(self) -> None:
        h = Harness()
        h.occupy("spisebord", at=0.0)  # same room as the TV zone
        h.occupy("kjokken", at=1.0)  # other room
        h.fire(SetTvSoloMode(TvSoloMode.TV_ZONE), at=2.0)
        effects = h.fire(TvPlayingChanged("sofakrok", True), at=3.0)
        expect_ramp(effects, SPISEBORD, FLOOR, duration=2.0)  # suppressed despite room
        expect_ramp(effects, KJOKKEN, FLOOR, duration=2.0)  # suppressed
        expect_ramp(effects, SOFAKROK, 0.3, duration=3.0)  # the TV zone fades in
        assert h.state.zones["spisebord"].phase is ZonePhase.ACTIVE  # FSM untouched

    def test_rule_6_2_same_room_keeps_both_stue_zones_audible(self) -> None:
        # Contrast case for the scenario above: SAME_ROOM spares spisebord.
        h = Harness()
        h.occupy("spisebord", at=0.0)
        h.occupy("kjokken", at=1.0)
        h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=2.0)
        effects = h.fire(TvPlayingChanged("sofakrok", True), at=3.0)
        # Spisebord stays audible: already at 0.33, and the TV-forced unity
        # scale keeps its target there — no write at all, let alone a 0.
        expect_no_ramp(effects, SPISEBORD)
        assert h.state.speakers[SPISEBORD].commanded == 0.3 * 1.1
        expect_ramp(effects, KJOKKEN, FLOOR, duration=2.0)  # other room suppressed
        expect_ramp(effects, SOFAKROK, 0.3, duration=3.0)

    def test_rule_6_2_switching_same_room_to_tv_zone_single_reconcile(self) -> None:
        h = Harness()
        h.occupy("spisebord", at=0.0)
        h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=1.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)  # spisebord stays audible
        effects = h.fire(SetTvSoloMode(TvSoloMode.TV_ZONE), at=3.0)
        expect_ramp(effects, SPISEBORD, FLOOR, duration=2.0)  # now suppressed
        expect_no_ramp(effects, SOFAKROK)
        effects = h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=20.0)
        expect_ramp(effects, SPISEBORD, 0.3 * 1.1, duration=2.0)  # restored

    def test_rule_6_2_tv_zone_restores_on_tv_stop(self) -> None:
        h = Harness()
        h.occupy("spisebord", at=0.0)
        h.fire(SetTvSoloMode(TvSoloMode.TV_ZONE), at=1.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)
        effects = h.fire(TvPlayingChanged("sofakrok", False), at=10.0)
        # Single reconcile restores spisebord at the current room scale:
        # sofakrok is RELEASING (still audible), so the stue splits 1/sqrt(2).
        expect_ramp(effects, SPISEBORD, 0.3 * 1.1 * STUE_2, duration=2.0)
        expect_ramp(effects, SOFAKROK, 0.3 * STUE_2, duration=2.0)  # unity scale gone
        assert timer_starts(effects) == [StartTimer(timers.zone_release("sofakrok"), 15.0)]

    def test_rule_6_2_tv_zone_mode_change_sets_mode_timestamp(self) -> None:
        from custom_components.sonos_conductor.core.events import ExternalVolume

        h = Harness()
        h.occupy("spisebord", at=0.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=1.0)
        # t=1 TV-mode stamp; switching modes at t=15 re-stamps the window.
        h.fire(SetTvSoloMode(TvSoloMode.TV_ZONE), at=15.0)  # suppression set changes
        assert h.fire(ExternalVolume(SOFAKROK, 0.4), at=20.0) == []  # still in motion


class TestRule63SoloMode:
    def test_rule_6_3_enabling_solo_reconciles(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=1.0)  # mode off: no suppression
        effects = h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=2.0)
        expect_ramp(effects, KJOKKEN, FLOOR, duration=2.0)

    def test_rule_6_3_disabling_solo_restores(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=1.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)
        effects = h.fire(SetTvSoloMode(TvSoloMode.OFF), at=3.0)
        expect_ramp(effects, KJOKKEN, 0.36, duration=2.0)

    @pytest.mark.parametrize("mode", [TvSoloMode.OFF, TvSoloMode.SAME_ROOM, TvSoloMode.TV_ZONE])
    def test_solo_without_tv_playing_changes_nothing(self, mode: TvSoloMode) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        assert h.fire(SetTvSoloMode(mode), at=1.0) == []
        effects = h.occupy("spisebord", at=2.0)
        expect_ramp(effects, SPISEBORD, 0.3 * 1.1, duration=3.0)  # not suppressed


def test_tv_stop_with_two_stue_zones_restores_sqrt_scaling() -> None:
    h = Harness()
    h.occupy("sofakrok", at=0.0)
    h.occupy("spisebord", at=1.0)
    h.fire(TvPlayingChanged("sofakrok", True), at=2.0)  # unity scale
    effects = h.fire(TvPlayingChanged("sofakrok", False), at=10.0)
    # Sofakrok still occupied: no release; both scale back to 1/sqrt(2).
    assert timer_starts(effects) == []
    expect_ramp(effects, SPISEBORD, 0.3 * 1.1 * STUE_2, duration=2.0)
    expect_ramp(effects, SOFAKROK, 0.3 * STUE_2, duration=2.0)
