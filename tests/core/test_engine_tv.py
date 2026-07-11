"""Rule 6: TV mode (room scale) and TV solo suppression."""

from __future__ import annotations

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.effects import StartTimer
from custom_components.sonos_conductor.core.events import SetTvSolo, TvPlayingChanged
from custom_components.sonos_conductor.core.model import ZonePhase

from .harness import (
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


class TestRule62Solo:
    def test_rule_6_2_solo_suppresses_other_rooms(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(SetTvSolo(True), at=1.0)
        effects = h.fire(TvPlayingChanged("sofakrok", True), at=2.0)
        expect_ramp(effects, KJOKKEN, 0.0, duration=2.0)  # suppressed
        expect_ramp(effects, SOFAKROK, 0.3, duration=3.0)  # fade_in
        assert h.state.zones["kjokken"].phase is ZonePhase.ACTIVE  # FSM untouched

    def test_rule_6_2_fsm_keeps_running_while_suppressed(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(SetTvSolo(True), at=1.0)
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
        h.fire(SetTvSolo(True), at=1.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)
        effects = h.fire(TvPlayingChanged("sofakrok", False), at=10.0)
        expect_ramp(effects, KJOKKEN, 0.36, duration=2.0)  # restored
        assert timer_starts(effects) == [StartTimer(timers.zone_release("sofakrok"), 15.0)]

    def test_rule_6_2_suppression_change_sets_mode_timestamp(self) -> None:
        from custom_components.sonos_conductor.core.events import ExternalVolume

        h = Harness()
        h.fire(SetTvSolo(True), at=0.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)  # suppression set changes
        # Sofakrok is audible but the fleet is "in motion" until t=12.
        assert h.fire(ExternalVolume(SOFAKROK, 0.4), at=5.0) == []

    def test_rule_6_2_same_room_zone_not_suppressed(self) -> None:
        h = Harness()
        h.fire(SetTvSolo(True), at=0.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=1.0)
        effects = h.occupy("spisebord", at=2.0)
        # Same room as the TV: audible, at TV-forced unity scale.
        expect_ramp(effects, SPISEBORD, 0.3 * 1.1, duration=3.0)
        expect_no_ramp(effects, SOFAKROK)


class TestRule63SoloToggle:
    def test_rule_6_3_enabling_solo_reconciles(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=1.0)  # solo off: no suppression
        effects = h.fire(SetTvSolo(True), at=2.0)
        expect_ramp(effects, KJOKKEN, 0.0, duration=2.0)

    def test_rule_6_3_disabling_solo_restores(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(SetTvSolo(True), at=1.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)
        effects = h.fire(SetTvSolo(False), at=3.0)
        expect_ramp(effects, KJOKKEN, 0.36, duration=2.0)

    def test_solo_without_tv_playing_changes_nothing(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        assert h.fire(SetTvSolo(True), at=1.0) == []
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
