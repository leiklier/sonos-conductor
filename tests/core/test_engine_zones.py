"""Rule 1: zone lifecycle FSM, hold timers, TV-as-occupancy, fallback."""

from __future__ import annotations

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.effects import CancelTimer, RampVolume, StartTimer
from custom_components.sonos_conductor.core.events import (
    OccupancyChanged,
    TimerFired,
    TvPlayingChanged,
)
from custom_components.sonos_conductor.core.model import ZonePhase

from .harness import (
    FLOOR,
    KJOKKEN,
    SOFAKROK,
    SPISEBORD,
    Harness,
    expect_no_volume_effects,
    expect_ramp,
    ramps,
    timer_cancels,
    timer_starts,
)


class TestRule11Activation:
    def test_rule_1_1_idle_to_active_fades_in(self) -> None:
        h = Harness()
        effects = h.occupy("kjokken", at=1.0)
        expect_ramp(effects, KJOKKEN, 0.36, duration=3.0)  # 0.3 * 1.2, fade_in
        assert timer_cancels(effects) == []  # no release timer was pending
        assert h.state.zones["kjokken"].phase is ZonePhase.ACTIVE
        assert h.state.zones["kjokken"].last_transition == 1.0

    def test_rule_1_1_releasing_to_active_cancels_hold_no_volume(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.vacate("kjokken", at=5.0)
        effects = h.occupy("kjokken", at=10.0)
        assert effects == [CancelTimer(timers.zone_release("kjokken"))]
        assert h.state.zones["kjokken"].phase is ZonePhase.ACTIVE

    def test_rule_1_1_releasing_reentry_keeps_last_transition(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.vacate("kjokken", at=5.0)
        h.occupy("kjokken", at=10.0)
        # Audibility never changed after the IDLE->ACTIVE at t=0.
        assert h.state.zones["kjokken"].last_transition == 0.0


class TestRule12Release:
    def test_rule_1_2_occupancy_lost_starts_hold_timer(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.vacate("kjokken", at=5.0)
        assert timer_starts(effects) == [StartTimer(timers.zone_release("kjokken"), 60.0)]
        expect_no_volume_effects(effects)  # volume unchanged while RELEASING
        assert h.state.zones["kjokken"].phase is ZonePhase.RELEASING
        assert h.state.zones["kjokken"].last_transition == 0.0  # still audible

    def test_rule_1_2_hold_seconds_is_per_zone(self) -> None:
        h = Harness()
        h.occupy("spisebord", at=0.0)
        effects = h.vacate("spisebord", at=5.0)
        assert timer_starts(effects) == [StartTimer(timers.zone_release("spisebord"), 15.0)]


class TestRule13HoldExpiry:
    def test_rule_1_3_release_timer_fades_out(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.vacate("kjokken", at=5.0)
        effects = h.fire_timer(timers.zone_release("kjokken"))  # at t=65
        assert h.now == 65.0
        expect_ramp(effects, KJOKKEN, FLOOR, duration=5.0)  # fade_out
        expect_ramp(effects, SOFAKROK, 0.3, duration=3.0)  # fallback re-forced
        assert h.state.zones["kjokken"].phase is ZonePhase.IDLE
        assert h.state.zones["kjokken"].last_transition == 65.0

    def test_rule_1_3_stale_release_timer_ignored(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.vacate("kjokken", at=5.0)
        h.occupy("kjokken", at=10.0)  # cancels the hold timer
        assert h.fire(TimerFired(timers.zone_release("kjokken")), at=65.0) == []
        assert h.state.zones["kjokken"].phase is ZonePhase.ACTIVE


class TestRule14TvOccupancy:
    def test_rule_1_4_tv_activates_zone(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)  # fallback yields
        effects = h.fire(TvPlayingChanged("sofakrok", True), at=1.0)
        expect_ramp(effects, SOFAKROK, 0.3, duration=3.0)
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE

    def test_rule_1_4_tv_holds_off_releasing(self) -> None:
        h = Harness()
        h.occupy("sofakrok", at=0.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=1.0)
        effects = h.vacate("sofakrok", at=2.0)
        assert effects == []  # TV still counts as occupancy
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE

    def test_rule_1_4_release_when_both_tv_and_occupancy_gone(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=1.0)
        effects = h.fire(TvPlayingChanged("sofakrok", False), at=2.0)
        assert timer_starts(effects) == [StartTimer(timers.zone_release("sofakrok"), 15.0)]
        assert h.state.zones["sofakrok"].phase is ZonePhase.RELEASING


class TestRule15Fallback:
    def test_rule_1_5_fallback_yields_immediately_when_other_activates(self) -> None:
        h = Harness()  # sofakrok forced ACTIVE at seed
        effects = h.occupy("kjokken", at=1.0)
        # No RELEASING detour for the forced fallback: straight to IDLE.
        assert h.state.zones["sofakrok"].phase is ZonePhase.IDLE
        expect_ramp(effects, SOFAKROK, FLOOR, duration=5.0)  # fade_out
        assert timer_starts(effects) == []

    def test_rule_1_5_fallback_reforced_when_last_zone_releases(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.release("kjokken", at=5.0)
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE
        expect_ramp(effects, SOFAKROK, 0.3, duration=3.0)  # fade_in

    def test_rule_1_5_occupied_fallback_stays_when_other_activates(self) -> None:
        h = Harness()
        h.occupy("sofakrok", at=0.0)  # earns its audibility
        effects = h.occupy("kjokken", at=1.0)
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE
        assert [r.speaker_id for r in ramps(effects)] == [KJOKKEN]

    def test_rule_1_5_forced_fallback_ignores_occupancy_off(self) -> None:
        h = Harness()  # forced ACTIVE, occupied=False
        effects = h.vacate("sofakrok", at=1.0)
        assert effects == []  # no RELEASING churn while forced
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE

    def test_rule_1_5_owned_fallback_releases_then_reforces(self) -> None:
        h = Harness()
        h.occupy("sofakrok", at=0.0)  # forced -> owned
        effects = h.vacate("sofakrok", at=1.0)
        assert timer_starts(effects) == [StartTimer(timers.zone_release("sofakrok"), 15.0)]
        assert h.state.zones["sofakrok"].phase is ZonePhase.RELEASING
        effects = h.fire_timer(timers.zone_release("sofakrok"))  # t=16
        # Nothing else audible: forced straight back to ACTIVE, no ramps.
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE
        expect_no_volume_effects(effects)


class TestRule16AndEdges:
    def test_rule_1_6_phase_change_without_target_change_is_silent(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.vacate("kjokken", at=5.0)  # ACTIVE -> RELEASING
        assert not any(isinstance(e, RampVolume) for e in effects)

    def test_repeated_occupancy_event_is_noop(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        assert h.occupy("kjokken", at=1.0) == []

    def test_occupancy_off_for_idle_zone_is_noop(self) -> None:
        h = Harness()
        assert h.vacate("spisebord", at=1.0) == []
        assert h.state.zones["spisebord"].phase is ZonePhase.IDLE

    def test_occupancy_for_unknown_zone_ignored(self) -> None:
        h = Harness()
        assert h.fire(OccupancyChanged("garage", True), at=1.0) == []

    def test_two_zones_same_room_split_loudness(self) -> None:
        h = Harness()
        h.occupy("sofakrok", at=0.0)
        effects = h.occupy("spisebord", at=1.0)
        # Both stue zones audible: each scaled by 1/sqrt(2).
        expect_ramp(effects, SPISEBORD, 0.3 * 1.1 / 2**0.5, duration=3.0)  # fade_in
        expect_ramp(effects, SOFAKROK, 0.3 * 1.0 / 2**0.5, duration=2.0)  # rebalance
