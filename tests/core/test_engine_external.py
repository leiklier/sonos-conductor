"""Rule 4: external volume reports and reverse sync."""

from __future__ import annotations

import pytest

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.effects import CancelTimer, RampVolume, StartTimer
from custom_components.sonos_conductor.core.events import (
    ExternalVolume,
    SetEnabled,
    SetMaster,
    TvPlayingChanged,
)
from custom_components.sonos_conductor.core.volume_math import implied_master

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

DEBOUNCE_SOFA = timers.external_debounce(SOFAKROK)


class TestRule41Acceptance:
    def test_rule_4_1_volume_always_updated_even_when_discarded(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.fire(ExternalVolume(KJOKKEN, 0.5), at=4.0)  # in suppression window
        assert effects == []
        assert h.state.speakers[KJOKKEN].volume == 0.5
        assert h.state.speakers[KJOKKEN].pending_external is None

    def test_rule_4_1_zone_not_audible_discards(self) -> None:
        h = Harness()
        assert h.fire(ExternalVolume(KJOKKEN, 0.5), at=0.0) == []  # kjokken IDLE

    def test_rule_4_1_disabled_discards(self) -> None:
        h = Harness()
        h.fire(SetEnabled(False), at=0.0)
        assert h.fire(ExternalVolume(SOFAKROK, 0.5), at=1.0) == []
        assert h.state.speakers[SOFAKROK].volume == 0.5

    def test_rule_4_1_hard_zero_guard(self) -> None:
        h = Harness()
        assert h.fire(ExternalVolume(SOFAKROK, 0.01), at=0.0) == []
        assert h.fire(ExternalVolume(SOFAKROK, 0.005), at=1.0) == []
        # Just above the guard: accepted.
        effects = h.fire(ExternalVolume(SOFAKROK, 0.011), at=2.0)
        assert timer_starts(effects) == [StartTimer(DEBOUNCE_SOFA, 1.5)]

    def test_rule_4_1_suppression_window_boundary(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)  # zone transitions at t=0
        assert h.fire(ExternalVolume(KJOKKEN, 0.5), at=9.9) == []
        # now - last == transition_suppression: window has passed.
        effects = h.fire(ExternalVolume(KJOKKEN, 0.5), at=10.0)
        assert timer_starts(effects) == [StartTimer(timers.external_debounce(KJOKKEN), 1.5)]

    def test_rule_4_1_tv_mode_change_suppresses(self) -> None:
        h = Harness()
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)  # no phase change (forced)
        assert h.fire(ExternalVolume(SOFAKROK, 0.4), at=5.0) == []
        effects = h.fire(ExternalVolume(SOFAKROK, 0.4), at=12.5)
        assert timer_starts(effects) == [StartTimer(DEBOUNCE_SOFA, 1.5)]


class TestRule42Debounce:
    def test_rule_4_2_accepted_report_debounces(self) -> None:
        h = Harness()
        effects = h.fire(ExternalVolume(SOFAKROK, 0.4), at=0.0)
        assert effects == [StartTimer(DEBOUNCE_SOFA, 1.5)]
        assert h.state.speakers[SOFAKROK].pending_external == 0.4
        assert ramps(effects) == []  # nothing applied yet

    def test_rule_4_2_new_report_restarts_debounce(self) -> None:
        h = Harness()
        h.fire(ExternalVolume(SOFAKROK, 0.4), at=0.0)
        effects = h.fire(ExternalVolume(SOFAKROK, 0.45), at=0.5)
        assert effects == [StartTimer(DEBOUNCE_SOFA, 1.5)]
        assert h.state.speakers[SOFAKROK].pending_external == 0.45


class TestRule43Apply:
    def test_rule_4_3_fire_applies_implied_master_and_rebalances(self) -> None:
        h = Harness()
        h.occupy("sofakrok", at=0.0)  # no phase change: fallback already ACTIVE
        h.occupy("spisebord", at=0.0)  # transition at t=0
        h.fire(ExternalVolume(SOFAKROK, 0.4), at=20.0)
        effects = h.fire_timer(DEBOUNCE_SOFA)  # t=21.5
        expected_master = implied_master(0.4, 1.0, STUE_2)
        assert h.state.master == pytest.approx(expected_master)
        expect_no_ramp(effects, SOFAKROK)  # reporter already there
        assert h.state.speakers[SOFAKROK].commanded == pytest.approx(0.4)
        expect_ramp(effects, SPISEBORD, 0.4 * 1.1, duration=2.0)  # rebalance

    def test_rule_4_3_below_threshold_no_change(self) -> None:
        h = Harness()
        h.fire(ExternalVolume(SOFAKROK, 0.315), at=0.0)  # implies 0.315, delta 0.015
        effects = h.fire_timer(DEBOUNCE_SOFA)
        assert effects == []
        assert h.state.master == pytest.approx(0.3)
        assert h.state.speakers[SOFAKROK].pending_external is None  # consumed

    def test_rule_4_3_conditions_rechecked_at_fire_time(self) -> None:
        h = Harness()
        h.occupy("sofakrok", at=0.0)
        h.fire(ExternalVolume(SOFAKROK, 0.4), at=20.0)
        h.occupy("kjokken", at=20.5)  # transition during the debounce window
        effects = h.fire_timer(DEBOUNCE_SOFA)  # t=21.5: suppressed now
        assert effects == []
        assert h.state.master == pytest.approx(0.3)

    def test_rule_4_3_clamped_implied_corrects_reporter_immediately(self) -> None:
        h = Harness()
        h.occupy("sofakrok", at=0.0)
        h.occupy("spisebord", at=0.0)
        h.fire(ExternalVolume(SPISEBORD, 0.9), at=20.0)  # implies > 1.0
        effects = h.fire_timer(timers.external_debounce(SPISEBORD))
        assert h.state.master == 1.0
        # Reporter cannot stay at 0.9: pulled to its achievable target, now.
        expect_ramp(effects, SPISEBORD, 1.1 * STUE_2, duration=0.0)
        expect_ramp(effects, SOFAKROK, STUE_2, duration=2.0)


class TestRule44Supersede:
    def test_rule_4_4_reconcile_write_clears_pending_and_cancels_timer(self) -> None:
        h = Harness()
        h.fire(ExternalVolume(SOFAKROK, 0.4), at=0.0)
        effects = h.fire(SetMaster(0.5), at=0.5)
        assert effects == [
            CancelTimer(DEBOUNCE_SOFA),  # 10.5: cancels first
            RampVolume(SOFAKROK, 0.5, 0.0),
        ]
        assert h.state.speakers[SOFAKROK].pending_external is None
        # The superseded timer firing later is ignored.
        assert h.fire_timer(DEBOUNCE_SOFA, at=1.5) == []

    def test_reconcile_without_write_keeps_pending(self) -> None:
        h = Harness()
        h.fire(ExternalVolume(SOFAKROK, 0.4), at=0.0)
        h.fire(SetMaster(0.3), at=0.5)  # no-op reconcile: no write
        assert h.state.speakers[SOFAKROK].pending_external == 0.4
