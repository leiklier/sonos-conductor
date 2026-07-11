"""Rule 2: dock / standalone behavior."""

from __future__ import annotations

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.effects import CancelTimer, StartTimer
from custom_components.sonos_conductor.core.events import (
    DockChanged,
    ExternalVolume,
    GroupMembersReported,
    SetMaster,
    SetMute,
)
from custom_components.sonos_conductor.core.model import ZonePhase

from .harness import (
    KJOKKEN,
    SOFAKROK,
    SPISEBORD,
    Harness,
    expect_no_ramp,
    expect_ramp,
    mute_effects,
    ramps,
    timer_starts,
)


class TestRule21Undock:
    def test_rule_2_1_undock_goes_standalone_without_volume_effect(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.fire(DockChanged(KJOKKEN, False), at=1.0)
        expect_no_ramp(effects, KJOKKEN)  # user owns it at its current volume
        assert h.state.zones["kjokken"].phase is ZonePhase.STANDALONE
        assert h.state.speakers[KJOKKEN].commanded is None
        # Nothing else audible anymore -> fallback re-forced.
        expect_ramp(effects, SOFAKROK, 0.3, duration=3.0)

    def test_rule_2_1_undock_cancels_release_timer(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.vacate("kjokken", at=5.0)
        effects = h.fire(DockChanged(KJOKKEN, False), at=6.0)
        assert effects[0] == CancelTimer(timers.zone_release("kjokken"))

    def test_rule_2_1_other_speakers_rebalance_on_departure(self) -> None:
        h = Harness()
        h.occupy("sofakrok", at=0.0)
        h.occupy("spisebord", at=1.0)  # stue at 1/sqrt(2)
        effects = h.fire(DockChanged(SPISEBORD, False), at=2.0)
        expect_no_ramp(effects, SPISEBORD)
        expect_ramp(effects, SOFAKROK, 0.3, duration=2.0)  # back to full scale

    def test_undock_when_already_standalone_is_noop(self) -> None:
        h = Harness()
        h.fire(DockChanged(KJOKKEN, False), at=0.0)
        assert h.fire(DockChanged(KJOKKEN, False), at=1.0) == []

    def test_occupancy_while_standalone_updates_state_only(self) -> None:
        h = Harness()
        h.fire(DockChanged(KJOKKEN, False), at=0.0)
        assert h.occupy("kjokken", at=1.0) == []
        assert h.state.zones["kjokken"].phase is ZonePhase.STANDALONE
        assert h.state.zones["kjokken"].occupied is True
        # ... and is picked up on redock (rule 2.2).
        effects = h.fire(DockChanged(KJOKKEN, True), at=2.0)
        expect_ramp(effects, KJOKKEN, 0.36, duration=3.0)


class TestRule22Redock:
    def test_rule_2_2_redock_occupied_fades_in(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(DockChanged(KJOKKEN, False), at=1.0)
        effects = h.fire(DockChanged(KJOKKEN, True), at=30.0)
        assert h.state.zones["kjokken"].phase is ZonePhase.ACTIVE
        expect_ramp(effects, KJOKKEN, 0.36, duration=3.0)  # fade_in
        expect_ramp(effects, SOFAKROK, 0.0, duration=5.0)  # fallback yields

    def test_rule_2_2_redock_unoccupied_lands_idle_and_silences(self) -> None:
        # Conductor takes ownership back: an idle zone's speaker goes to 0.
        h = Harness()
        h.fire(DockChanged(KJOKKEN, False), at=0.0)
        effects = h.fire(DockChanged(KJOKKEN, True), at=10.0)
        assert h.state.zones["kjokken"].phase is ZonePhase.IDLE
        expect_ramp(effects, KJOKKEN, 0.0, duration=2.0)

    def test_rule_2_2_redock_schedules_group_repair(self) -> None:
        h = Harness()
        h.fire(DockChanged(KJOKKEN, False), at=0.0)
        # While undocked the observed group without kjokken matches.
        assert h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK, SPISEBORD)), at=1.0) == []
        effects = h.fire(DockChanged(KJOKKEN, True), at=5.0)
        assert timer_starts(effects) == [StartTimer(timers.GROUP_REPAIR, 15.0)]
        # 10.5: StartTimer comes after the volume effects.
        assert effects.index(timer_starts(effects)[0]) > effects.index(ramps(effects)[0])

    def test_redock_when_already_docked_is_noop(self) -> None:
        h = Harness()
        assert h.fire(DockChanged(KJOKKEN, True), at=1.0) == []


class TestRule23Invisibility:
    def test_rule_2_3_standalone_invisible_to_master_fanout(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(DockChanged(KJOKKEN, False), at=1.0)
        effects = h.fire(SetMaster(0.5), at=2.0)
        expect_no_ramp(effects, KJOKKEN)
        expect_ramp(effects, SOFAKROK, 0.5, duration=0.0)  # master_fade

    def test_rule_2_3_standalone_invisible_to_mute_fanout(self) -> None:
        h = Harness()
        h.fire(DockChanged(KJOKKEN, False), at=0.0)
        effects = h.fire(SetMute(True), at=1.0)
        assert [m.speaker_id for m in mute_effects(effects)] == [SPISEBORD, SOFAKROK]

    def test_rule_2_3_standalone_external_volume_discarded(self) -> None:
        h = Harness()
        h.fire(DockChanged(KJOKKEN, False), at=0.0)
        assert h.fire(ExternalVolume(KJOKKEN, 0.77), at=1.0) == []
        assert h.state.speakers[KJOKKEN].volume == 0.77  # state still updated
        assert h.state.speakers[KJOKKEN].pending_external is None
