"""Rule 7: group repair."""

from __future__ import annotations

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.effects import CancelTimer, JoinGroup, StartTimer
from custom_components.sonos_conductor.core.events import (
    DockChanged,
    GroupMembersReported,
    SetKeepGrouped,
    TimerFired,
)

from .harness import (
    ALL_SPEAKERS,
    KJOKKEN,
    SOFAKROK,
    SPISEBORD,
    Harness,
    join_effects,
    make_config,
    make_snapshot,
    timer_starts,
)


class TestRule72Scheduling:
    def test_rule_7_2_deviation_schedules_repair_timer(self) -> None:
        h = Harness()
        effects = h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK, SPISEBORD)), at=0.0)
        assert effects == [StartTimer(timers.GROUP_REPAIR, 15.0)]

    def test_rule_7_2_matching_report_cancels_pending_timer(self) -> None:
        h = Harness()
        h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK, SPISEBORD)), at=0.0)
        effects = h.fire(GroupMembersReported(SOFAKROK, ALL_SPEAKERS), at=1.0)
        assert effects == [CancelTimer(timers.GROUP_REPAIR)]

    def test_rule_7_2_matching_report_without_pending_is_silent(self) -> None:
        h = Harness()
        assert h.fire(GroupMembersReported(SOFAKROK, ALL_SPEAKERS), at=0.0) == []

    def test_rule_7_2_deviating_report_restarts_timer(self) -> None:
        h = Harness()
        h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK, SPISEBORD)), at=0.0)
        effects = h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK,)), at=5.0)
        assert effects == [StartTimer(timers.GROUP_REPAIR, 15.0)]  # restart

    def test_rule_7_2_keep_grouped_off_ignores_reports(self) -> None:
        h = Harness(snapshot=make_snapshot(keep_grouped=False))
        assert h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK,)), at=0.0) == []


class TestRule73Fire:
    def test_rule_7_3_fire_emits_single_join_for_missing(self) -> None:
        h = Harness()
        h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK, SPISEBORD)), at=0.0)
        effects = h.fire_timer(timers.GROUP_REPAIR)  # t=15
        assert effects == [JoinGroup(SOFAKROK, (KJOKKEN,))]

    def test_rule_7_3_missing_members_in_config_order(self) -> None:
        h = Harness()
        h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK,)), at=0.0)
        effects = h.fire_timer(timers.GROUP_REPAIR)
        assert effects == [JoinGroup(SOFAKROK, (KJOKKEN, SPISEBORD))]

    def test_rule_7_3_healed_before_fire_means_no_join(self) -> None:
        h = Harness()
        h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK,)), at=0.0)
        h.fire(GroupMembersReported(SOFAKROK, ALL_SPEAKERS), at=5.0)  # cancels
        assert h.fire(TimerFired(timers.GROUP_REPAIR), at=15.0) == []  # stale fire

    def test_rule_7_3_failed_join_retries_via_next_report(self) -> None:
        h = Harness()
        h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK, SPISEBORD)), at=0.0)
        assert len(join_effects(h.fire_timer(timers.GROUP_REPAIR))) == 1
        # The join failed; a later report still shows the deviation.
        effects = h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK, SPISEBORD)), at=20.0)
        assert effects == [StartTimer(timers.GROUP_REPAIR, 15.0)]
        assert join_effects(h.fire_timer(timers.GROUP_REPAIR)) == [JoinGroup(SOFAKROK, (KJOKKEN,))]


class TestRule74KeepGrouped:
    def test_rule_7_4_disabling_keep_grouped_cancels_repair(self) -> None:
        h = Harness()
        h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK,)), at=0.0)
        effects = h.fire(SetKeepGrouped(False), at=1.0)
        assert effects == [CancelTimer(timers.GROUP_REPAIR)]
        assert h.fire(TimerFired(timers.GROUP_REPAIR), at=16.0) == []

    def test_rule_7_2_enabling_keep_grouped_evaluates(self) -> None:
        h = Harness(snapshot=make_snapshot(keep_grouped=False))
        h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK,)), at=0.0)  # stored only
        effects = h.fire(SetKeepGrouped(True), at=1.0)
        assert effects == [StartTimer(timers.GROUP_REPAIR, 15.0)]


class TestRule71Topology:
    def test_rule_7_1_leader_standalone_skips_repair(self) -> None:
        h = Harness()
        h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK,)), at=0.0)  # timer pending
        effects = h.fire(DockChanged(SOFAKROK, False), at=1.0)
        assert CancelTimer(timers.GROUP_REPAIR) in effects
        # Reports while the leader is standalone schedule nothing.
        assert timer_starts(h.fire(GroupMembersReported(KJOKKEN, (KJOKKEN,)), at=2.0)) == []

    def test_rule_7_1_observed_via_member_report_mentioning_leader(self) -> None:
        h = Harness(
            snapshot=make_snapshot(
                group_members={
                    KJOKKEN: (),
                    SPISEBORD: (SOFAKROK, SPISEBORD),
                    SOFAKROK: (),
                }
            )
        )
        assert timer_starts(h.start_effects) == [StartTimer(timers.GROUP_REPAIR, 15.0)]

    def test_rule_7_1_unknown_topology_schedules_nothing(self) -> None:
        h = Harness(snapshot=make_snapshot(group_members={}))
        assert timer_starts(h.start_effects) == []

    def test_rule_7_1_standalone_member_is_not_expected(self) -> None:
        h = Harness()
        h.fire(DockChanged(KJOKKEN, False), at=0.0)
        # Kjokken absent from the group is fine while it is standalone.
        assert h.fire(GroupMembersReported(SOFAKROK, (SOFAKROK, SPISEBORD)), at=1.0) == []

    def test_rule_7_1_membership_not_leadership(self) -> None:
        # A group led by someone else is fine as long as everyone is in it.
        h = Harness()
        assert h.fire(GroupMembersReported(SOFAKROK, (KJOKKEN, SOFAKROK, SPISEBORD)), at=0.0) == []

    def test_primary_speaker_overrides_leader(self) -> None:
        h = Harness(config=make_config(primary_speaker_id=KJOKKEN))
        h.fire(GroupMembersReported(KJOKKEN, (KJOKKEN,)), at=0.0)
        effects = h.fire_timer(timers.GROUP_REPAIR)
        assert effects == [JoinGroup(KJOKKEN, (SPISEBORD, SOFAKROK))]

    def test_group_report_for_unknown_speaker_ignored(self) -> None:
        h = Harness()
        assert h.fire(GroupMembersReported("media_player.bogus", (SOFAKROK,)), at=0.0) == []
