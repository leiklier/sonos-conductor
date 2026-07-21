"""Spec section 9: startup seeding, master inference, gentle adoption."""

from __future__ import annotations

import pytest

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.effects import StartTimer
from custom_components.sonos_conductor.core.model import TvSoloMode, ZonePhase

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
    make_config,
    make_snapshot,
    ramps,
    timer_starts,
)


class TestSeeding:
    def test_rule_9_1_phases_seed_from_inputs(self) -> None:
        h = Harness(
            snapshot=make_snapshot(
                occupancy={"kjokken": True},
                tv_playing={"sofakrok": True},
                volumes={KJOKKEN: 0.36, SPISEBORD: 0.0, SOFAKROK: 0.3},
            )
        )
        assert h.state.zones["kjokken"].phase is ZonePhase.ACTIVE
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE
        assert h.state.zones["spisebord"].phase is ZonePhase.IDLE

    def test_rule_9_1_unoccupied_zone_starts_idle_not_releasing(self) -> None:
        h = Harness()
        assert h.state.zones["spisebord"].phase is ZonePhase.IDLE
        assert not any(
            isinstance(e, StartTimer) and e.timer_id.startswith(timers.ZONE_RELEASE_PREFIX)
            for e in h.start_effects
        )

    def test_rule_9_1_undocked_speaker_starts_standalone(self) -> None:
        h = Harness(snapshot=make_snapshot(occupancy={"kjokken": True}, docked={KJOKKEN: False}))
        assert h.state.zones["kjokken"].phase is ZonePhase.STANDALONE

    def test_seed_copies_flags_and_speaker_fields(self) -> None:
        h = Harness(
            snapshot=make_snapshot(
                muted={SOFAKROK: True},
                playing={SOFAKROK: True},
                tv_solo_mode=TvSoloMode.SAME_ROOM,
                keep_grouped=False,
                mute=True,
            ),
            auto_start=False,
        )
        assert h.state.tv_solo_mode is TvSoloMode.SAME_ROOM
        assert h.state.keep_grouped is False
        assert h.state.muted is True
        assert h.state.speakers[SOFAKROK].muted is True
        assert h.state.speakers[SOFAKROK].playing is True
        assert h.state.speakers[SOFAKROK].group_members == ALL_SPEAKERS

    def test_rule_1_5_fallback_forced_at_seed(self) -> None:
        h = Harness()
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE
        assert h.state.zones["kjokken"].phase is ZonePhase.IDLE


class TestMasterInference:
    def test_rule_9_2_snapshot_master_used(self) -> None:
        h = Harness(snapshot=make_snapshot(master=0.42, volumes={SOFAKROK: 0.42}))
        assert h.state.master == pytest.approx(0.42)

    def test_rule_9_2_snapshot_master_clamped(self) -> None:
        h = Harness(snapshot=make_snapshot(master=1.7), auto_start=False)
        assert h.state.master == 1.0

    def test_rule_9_2_master_inferred_from_median(self) -> None:
        h = Harness(
            snapshot=make_snapshot(
                master=None,
                occupancy={"kjokken": True, "spisebord": True, "sofakrok": True},
                volumes={
                    KJOKKEN: 0.3 * 1.2,  # implies 0.3
                    SPISEBORD: 0.4 * 1.1 * STUE_2,  # implies 0.4
                    SOFAKROK: 0.5 * 1.0 * STUE_2,  # implies 0.5
                },
            ),
            auto_start=False,
        )
        assert h.state.master == pytest.approx(0.4)

    def test_rule_9_2_median_skips_unknown_volumes(self) -> None:
        h = Harness(
            snapshot=make_snapshot(
                master=None,
                occupancy={"kjokken": True, "spisebord": True, "sofakrok": True},
                volumes={
                    KJOKKEN: None,
                    SPISEBORD: 0.4 * 1.1 * STUE_2,
                    SOFAKROK: 0.5 * 1.0 * STUE_2,
                },
            ),
            auto_start=False,
        )
        assert h.state.master == pytest.approx(0.45)

    def test_rule_9_2_median_ignores_non_audible_zones(self) -> None:
        h = Harness(
            snapshot=make_snapshot(
                master=None,
                occupancy={"spisebord": True, "sofakrok": True},
                volumes={
                    KJOKKEN: 0.9,  # zone IDLE: must not contribute
                    SPISEBORD: 0.4 * 1.1 * STUE_2,
                    SOFAKROK: 0.4 * 1.0 * STUE_2,
                },
            ),
            auto_start=False,
        )
        assert h.state.master == pytest.approx(0.4)

    def test_rule_9_2_no_data_keeps_model_default(self) -> None:
        # Fallback speaker "undocked" -> STANDALONE -> nothing audible.
        h = Harness(
            snapshot=make_snapshot(
                master=None,
                docked={SOFAKROK: False},
                volumes={KJOKKEN: 0.5, SPISEBORD: 0.5, SOFAKROK: 0.5},
            ),
            auto_start=False,
        )
        assert h.state.master == pytest.approx(0.15)


class TestAdoption:
    def test_start_when_converged_is_silent(self) -> None:
        h = Harness()
        assert h.start_effects == []

    def test_rule_9_3_adopt_within_tolerance(self) -> None:
        h = Harness(snapshot=make_snapshot(volumes={SOFAKROK: 0.28}))
        assert ramps(h.start_effects) == []
        assert h.state.speakers[SOFAKROK].commanded == pytest.approx(0.28)

    def test_rule_9_3_reconcile_outside_tolerance(self) -> None:
        h = Harness(snapshot=make_snapshot(volumes={SOFAKROK: 0.2}))
        expect_ramp(h.start_effects, SOFAKROK, 0.3, duration=2.0)
        assert h.state.speakers[SOFAKROK].commanded == pytest.approx(0.3)

    def test_rule_9_3_unknown_volume_reconciles(self) -> None:
        h = Harness(snapshot=make_snapshot(volumes={SOFAKROK: None}))
        expect_ramp(h.start_effects, SOFAKROK, 0.3, duration=2.0)

    def test_rule_9_3_standalone_never_emits(self) -> None:
        h = Harness(
            snapshot=make_snapshot(
                docked={KJOKKEN: False},
                volumes={KJOKKEN: 0.9, SPISEBORD: 0.0, SOFAKROK: 0.3},
            )
        )
        expect_no_ramp(h.start_effects, KJOKKEN)
        assert h.state.speakers[KJOKKEN].commanded is None

    def test_start_disabled_emits_nothing(self) -> None:
        h = Harness(snapshot=make_snapshot(enabled=False, volumes={SOFAKROK: 0.9, KJOKKEN: 0.9}))
        assert h.start_effects == []

    def test_startup_duck_active_caps_targets(self) -> None:
        h = Harness(snapshot=make_snapshot(duck_active={DOOR: True}))
        expect_ramp(h.start_effects, SOFAKROK, 0.05, duration=2.0)

    def test_startup_tv_solo_suppresses_at_seed(self) -> None:
        h = Harness(
            snapshot=make_snapshot(
                tv_solo_mode=TvSoloMode.SAME_ROOM,
                tv_playing={"sofakrok": True},
                occupancy={"kjokken": True},
                volumes={KJOKKEN: 0.36, SPISEBORD: 0.0, SOFAKROK: 0.3},
            )
        )
        expect_ramp(h.start_effects, KJOKKEN, FLOOR, duration=2.0)
        expect_no_ramp(h.start_effects, SOFAKROK)

    def test_startup_muted_still_converges(self) -> None:
        # Spec 9.3 has no mute carve-out; ramps while muted are inaudible.
        h = Harness(snapshot=make_snapshot(mute=True, volumes={SOFAKROK: 0.1}))
        expect_ramp(h.start_effects, SOFAKROK, 0.3, duration=2.0)


class TestStartupGroupRepair:
    def test_rule_9_4_group_repair_evaluated_once(self) -> None:
        h = Harness(
            snapshot=make_snapshot(
                group_members={
                    KJOKKEN: (KJOKKEN,),
                    SPISEBORD: (SOFAKROK, SPISEBORD),
                    SOFAKROK: (SOFAKROK, SPISEBORD),
                }
            )
        )
        assert timer_starts(h.start_effects) == [
            StartTimer(timers.GROUP_REPAIR, 15.0),
        ]

    def test_rule_9_4_no_repair_when_matching(self) -> None:
        h = Harness()
        assert timer_starts(h.start_effects) == []

    def test_rule_9_4_no_repair_when_keep_grouped_off(self) -> None:
        h = Harness(
            snapshot=make_snapshot(
                keep_grouped=False,
                group_members={sid: (sid,) for sid in ALL_SPEAKERS},
            )
        )
        assert timer_starts(h.start_effects) == []

    def test_start_is_idempotent(self) -> None:
        # The device never confirmed the ramp (volume still reads 0.2), so a
        # second start emits the identical convergence effects: harmless.
        h = Harness(snapshot=make_snapshot(volumes={SOFAKROK: 0.2}))
        assert h.engine.start(0.0) == h.start_effects


def test_snapshot_master_default_when_config_has_no_fallback() -> None:
    # A no-fallback config with nothing audible keeps the default master.
    config = make_config()
    zones = tuple(
        z
        if not z.fallback
        else type(z)(
            zone_id=z.zone_id,
            name=z.name,
            speaker_id=z.speaker_id,
            room_id=z.room_id,
            hold_seconds=z.hold_seconds,
            fallback=False,
            has_tv=z.has_tv,
        )
        for z in config.zones
    )
    config = type(config)(
        speakers=config.speakers,
        zones=zones,
        duck_inputs=config.duck_inputs,
        tunables=config.tunables,
    )
    h = Harness(config=config, snapshot=make_snapshot(master=None), auto_start=False)
    assert h.state.master == pytest.approx(0.15)
    assert all(z.phase is ZonePhase.IDLE for z in h.state.zones.values())
