"""Rule 8: enable / disable."""

from __future__ import annotations

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.effects import CancelTimer, RampVolume, StartTimer
from custom_components.sonos_conductor.core.events import (
    DockChanged,
    DuckChanged,
    ExternalMute,
    ExternalVolume,
    GroupMembersReported,
    SetEnabled,
    SetKeepGrouped,
    SetMaster,
    SetMute,
    SetTrim,
    SetTvSolo,
    TimerFired,
    TvPlayingChanged,
)
from custom_components.sonos_conductor.core.model import ZonePhase

from .harness import (
    DOOR,
    KJOKKEN,
    SOFAKROK,
    SPISEBORD,
    Harness,
    expect_ramp,
    timer_starts,
)


def _pending_all_timer_kinds() -> Harness:
    """A harness with a release, a debounce and a repair timer pending."""
    h = Harness()
    h.occupy("sofakrok", at=0.0)  # no phase change: forced -> owned
    h.fire(ExternalVolume(SOFAKROK, 0.4), at=1.0)  # debounce pending
    h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK, SPISEBORD)), at=1.0)  # repair
    h.occupy("kjokken", at=2.0)
    h.vacate("kjokken", at=3.0)  # release pending
    return h


class TestRule81Disable:
    def test_rule_8_1_disable_cancels_all_timers_only(self) -> None:
        h = _pending_all_timer_kinds()
        effects = h.fire(SetEnabled(False), at=4.0)
        assert effects == [
            CancelTimer(timers.zone_release("kjokken")),
            CancelTimer(timers.external_debounce(SOFAKROK)),
            CancelTimer(timers.GROUP_REPAIR),
        ]
        assert h.state.enabled is False
        assert h.state.speakers[SOFAKROK].pending_external is None

    def test_rule_8_1_phases_recomputed_at_disable(self) -> None:
        h = _pending_all_timer_kinds()
        h.fire(SetEnabled(False), at=4.0)
        assert h.state.zones["kjokken"].phase is ZonePhase.IDLE  # was RELEASING
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE  # occupied

    def test_rule_8_1_events_update_state_but_emit_nothing(self) -> None:
        h = Harness()
        h.fire(SetEnabled(False), at=0.0)
        assert h.occupy("spisebord", at=1.0) == []
        assert h.state.zones["spisebord"].phase is ZonePhase.ACTIVE
        assert h.fire(SetMaster(0.9), at=2.0) == []
        assert h.state.master == 0.9
        assert h.fire(SetMute(True), at=3.0) == []
        assert h.state.muted is True
        assert h.fire(TvPlayingChanged("sofakrok", True), at=4.0) == []
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE
        assert h.fire(DockChanged(KJOKKEN, False), at=5.0) == []
        assert h.state.zones["kjokken"].phase is ZonePhase.STANDALONE
        assert h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK,)), at=6.0) == []
        assert h.fire(DuckChanged(DOOR, True), at=7.0) == []
        assert h.fire(SetTvSolo(True), at=8.0) == []
        assert h.state.tv_solo is True
        assert h.fire(SetKeepGrouped(False), at=9.0) == []
        assert h.state.keep_grouped is False
        assert h.fire(SetTrim(KJOKKEN, 1.0), at=10.0) == []
        assert h.fire(ExternalMute(SPISEBORD, True), at=11.0) == []
        assert h.state.speakers[SPISEBORD].muted is True

    def test_disable_when_already_disabled_is_noop(self) -> None:
        h = Harness()
        h.fire(SetEnabled(False), at=0.0)
        assert h.fire(SetEnabled(False), at=1.0) == []


class TestRule82Enable:
    def test_rule_8_2_enable_recomputes_and_reconciles(self) -> None:
        h = Harness()
        h.fire(SetEnabled(False), at=0.0)
        h.occupy("spisebord", at=1.0)
        h.fire(ExternalVolume(SPISEBORD, 0.7), at=2.0)  # user moved it meanwhile
        effects = h.fire(SetEnabled(True), at=3.0)
        assert h.state.zones["spisebord"].phase is ZonePhase.ACTIVE
        assert h.state.zones["sofakrok"].phase is ZonePhase.IDLE  # other zone audible
        assert effects == [
            RampVolume(SPISEBORD, 0.3 * 1.1, 2.0),  # from adopted 0.7 back to target
            RampVolume(SOFAKROK, 0.0, 2.0),  # no longer forced
        ]

    def test_rule_8_2_enable_rearms_group_repair(self) -> None:
        h = Harness()
        h.fire(SetEnabled(False), at=0.0)
        h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK, SPISEBORD)), at=1.0)
        effects = h.fire(SetEnabled(True), at=2.0)
        assert timer_starts(effects) == [StartTimer(timers.GROUP_REPAIR, 15.0)]

    def test_rule_8_2_enable_reforces_fallback(self) -> None:
        h = Harness()
        h.fire(SetEnabled(False), at=0.0)
        effects = h.fire(SetEnabled(True), at=1.0)
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE
        assert effects == []  # volume already converged at 0.3

    def test_enable_applies_master_changed_while_disabled(self) -> None:
        h = Harness()
        h.fire(SetEnabled(False), at=0.0)
        h.fire(SetMaster(0.5), at=1.0)
        effects = h.fire(SetEnabled(True), at=2.0)
        expect_ramp(effects, SOFAKROK, 0.5, duration=2.0)

    def test_late_timer_after_disable_is_ignored(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.vacate("kjokken", at=5.0)
        h.fire(SetEnabled(False), at=6.0)
        assert h.fire(TimerFired(timers.zone_release("kjokken")), at=65.0) == []
