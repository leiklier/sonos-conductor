"""Rules 3 (master volume) and 5 (mute)."""

from __future__ import annotations

import pytest

from custom_components.sonos_conductor.core.events import (
    DockChanged,
    ExternalMute,
    ExternalVolume,
    SetMaster,
    SetMute,
)

from .harness import (
    KJOKKEN,
    SOFAKROK,
    SPISEBORD,
    Harness,
    expect_no_volume_effects,
    expect_ramp,
    mute_effects,
    ramps,
    timer_starts,
)


class TestRule3Master:
    def test_rule_3_1_set_master_reconciles_with_master_fade(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.fire(SetMaster(0.5), at=1.0)
        expect_ramp(effects, KJOKKEN, 0.6, duration=0.0)  # 0.5 * 1.2, master_fade=0
        assert h.state.master == 0.5

    def test_rule_3_1_master_clamped_high(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.fire(SetMaster(1.5), at=1.0)
        assert h.state.master == 1.0
        expect_ramp(effects, KJOKKEN, 1.0, duration=0.0)  # 1.0 * 1.2 clamps to 1

    def test_rule_3_1_master_clamped_low(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.fire(SetMaster(-0.3), at=1.0)
        assert h.state.master == 0.0
        expect_ramp(effects, KJOKKEN, 0.0, duration=0.0)

    def test_rule_3_1_while_muted_stores_only(self) -> None:
        h = Harness()
        h.fire(SetMute(True), at=0.0)
        assert h.fire(SetMaster(0.5), at=1.0) == []
        assert h.state.master == 0.5

    def test_rule_3_2_master_change_keeps_last_transition(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(SetMaster(0.5), at=5.0)
        assert h.state.zones["kjokken"].last_transition == 0.0

    def test_set_master_same_value_is_silent(self) -> None:
        h = Harness()
        assert h.fire(SetMaster(0.3), at=1.0) == []


class TestRule5Mute:
    def test_rule_5_1_mute_fans_out_volumes_untouched(self) -> None:
        h = Harness()
        effects = h.fire(SetMute(True), at=0.0)
        assert [(m.speaker_id, m.muted) for m in mute_effects(effects)] == [
            (KJOKKEN, True),
            (SPISEBORD, True),
            (SOFAKROK, True),
        ]
        expect_no_volume_effects(effects)
        assert h.state.muted is True
        assert h.state.master == pytest.approx(0.3)  # untouched
        assert all(s.muted for s in h.state.speakers.values())

    def test_rule_5_2_unmute_fans_out_and_reconciles(self) -> None:
        h = Harness()
        h.fire(SetMute(True), at=0.0)
        h.fire(SetMaster(0.5), at=1.0)  # stored only
        effects = h.fire(SetMute(False), at=2.0)
        assert [(m.speaker_id, m.muted) for m in mute_effects(effects)] == [
            (KJOKKEN, False),
            (SPISEBORD, False),
            (SOFAKROK, False),
        ]
        expect_ramp(effects, SOFAKROK, 0.5, duration=2.0)  # rebalance to new master

    def test_rule_5_2_unmute_without_changes_only_unmutes(self) -> None:
        h = Harness()
        h.fire(SetMute(True), at=0.0)
        effects = h.fire(SetMute(False), at=1.0)
        assert len(mute_effects(effects)) == 3
        expect_no_volume_effects(effects)

    def test_rule_5_3_external_mute_becomes_global(self) -> None:
        h = Harness()
        effects = h.fire(ExternalMute(SOFAKROK, True), at=0.0)
        assert len(mute_effects(effects)) == 3
        assert h.state.muted is True

    def test_rule_5_3_external_mute_matching_global_is_ignored(self) -> None:
        h = Harness()
        h.fire(SetMute(True), at=0.0)
        assert h.fire(ExternalMute(KJOKKEN, True), at=1.0) == []

    def test_rule_5_3_external_mute_from_standalone_is_ignored(self) -> None:
        h = Harness()
        h.fire(DockChanged(KJOKKEN, False), at=0.0)
        assert h.fire(ExternalMute(KJOKKEN, True), at=1.0) == []
        assert h.state.muted is False
        assert h.state.speakers[KJOKKEN].muted is True  # speaker state tracked

    def test_rule_5_4_muted_discards_external_volume(self) -> None:
        h = Harness()
        h.fire(SetMute(True), at=0.0)
        effects = h.fire(ExternalVolume(SOFAKROK, 0.5), at=1.0)
        assert timer_starts(effects) == []
        assert h.state.speakers[SOFAKROK].volume == 0.5

    def test_mute_fanout_is_unconditional(self) -> None:
        # Re-asserting mute repairs a speaker someone unmuted directly.
        h = Harness()
        h.fire(SetMute(True), at=0.0)
        effects = h.fire(SetMute(True), at=1.0)
        assert len(mute_effects(effects)) == 3
        assert ramps(effects) == []
