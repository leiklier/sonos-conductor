"""Rule 3.3 / 4.5: the night-mode volume ceiling and its reverse-sync guard."""

from __future__ import annotations

import pytest

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.events import (
    DuckChanged,
    ExternalVolume,
    SetEnabled,
    SetMaster,
    SetNightMode,
    SetTvSoloMode,
    TvPlayingChanged,
)
from custom_components.sonos_conductor.core.model import TvSoloMode, ZonePhase

from .harness import (
    DOOR,
    KJOKKEN,
    SOFAKROK,
    SPISEBORD,
    Harness,
    expect_no_ramp,
    expect_ramp,
    make_snapshot,
    ramps,
    timer_starts,
)


class TestRule33Basics:
    def test_engage_caps_audible_zones_with_rebalance_fade(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)  # kjokken at 0.36, fallback retired
        effects = h.fire(SetNightMode(True), at=1.0)
        expect_ramp(effects, KJOKKEN, 0.15, duration=2.0)  # rebalance_fade
        assert len(ramps(effects)) == 1  # silent speakers stay at 0
        assert h.state.night_mode is True

    def test_release_restores_exact_targets(self) -> None:
        h = Harness()
        before = expect_ramp(h.occupy("kjokken", at=0.0), KJOKKEN, 0.36, duration=3.0)
        h.fire(SetNightMode(True), at=1.0)
        effects = h.fire(SetNightMode(False), at=2.0)
        restored = expect_ramp(effects, KJOKKEN, 0.36, duration=2.0)  # rebalance_fade
        assert restored.target == before.target  # bit-exact, no float dust
        assert h.state.night_mode is False

    def test_night_caps_fallback_zone_too(self) -> None:
        h = Harness()  # sofakrok forced audible at 0.3
        effects = h.fire(SetNightMode(True), at=0.0)
        expect_ramp(effects, SOFAKROK, 0.15, duration=2.0)

    def test_repeated_event_is_noop(self) -> None:
        h = Harness()
        h.fire(SetNightMode(True), at=0.0)
        assert h.fire(SetNightMode(True), at=1.0) == []

    def test_master_above_cap_changes_nothing_while_night(self) -> None:
        h = Harness()
        h.fire(SetNightMode(True), at=0.0)  # sofakrok capped at 0.15
        assert h.fire(SetMaster(0.5), at=1.0) == []  # 3.3: cap holds, stored only
        effects = h.fire(SetNightMode(False), at=2.0)
        expect_ramp(effects, SOFAKROK, 0.5, duration=2.0)  # new master applies

    def test_master_below_cap_still_tracks(self) -> None:
        # The cap is a ceiling, not a level: quieter targets pass through.
        h = Harness()
        h.fire(SetNightMode(True), at=0.0)
        effects = h.fire(SetMaster(0.1), at=1.0)
        expect_ramp(effects, SOFAKROK, 0.1, duration=0.0)  # master_fade

    def test_zone_activation_fades_in_to_cap(self) -> None:
        h = Harness()
        h.fire(SetNightMode(True), at=0.0)
        effects = h.occupy("kjokken", at=1.0)
        expect_ramp(effects, KJOKKEN, 0.15, duration=3.0)  # fade_in, capped
        expect_ramp(effects, SOFAKROK, 0.0, duration=5.0)  # fallback retires


class TestNightDuckInteraction:
    def test_duck_floor_below_cap_wins_while_engaged(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(SetNightMode(True), at=1.0)  # capped at 0.15
        effects = h.fire(DuckChanged(DOOR, True), at=2.0)
        expect_ramp(effects, KJOKKEN, 0.05, duration=0.0)  # duck 0.05 < cap
        effects = h.fire(DuckChanged(DOOR, False), at=3.0)
        expect_ramp(effects, KJOKKEN, 0.15, duration=2.0)  # back to the cap only

    def test_night_engage_while_ducked_is_silent(self) -> None:
        h = Harness()
        h.fire(DuckChanged(DOOR, True), at=0.0)  # sofakrok at 0.05
        assert h.fire(SetNightMode(True), at=1.0) == []  # duck already lower
        effects = h.fire(DuckChanged(DOOR, False), at=2.0)
        expect_ramp(effects, SOFAKROK, 0.15, duration=2.0)  # rises only to the cap


class TestNightSeeding:
    def test_snapshot_seeds_night_mode(self) -> None:
        h = Harness(snapshot=make_snapshot(night_mode=True, volumes={SOFAKROK: 0.15}))
        assert h.state.night_mode is True
        assert h.start_effects == []  # already at the cap: adopted silently

    def test_startup_converges_to_cap(self) -> None:
        h = Harness(snapshot=make_snapshot(night_mode=True))  # sofakrok at 0.3
        expect_ramp(h.start_effects, SOFAKROK, 0.15, duration=2.0)


class TestNightDisabled:
    def test_night_while_disabled_stores_and_applies_on_enable(self) -> None:
        h = Harness()
        h.fire(SetEnabled(False), at=0.0)
        assert h.fire(SetNightMode(True), at=1.0) == []
        assert h.state.night_mode is True
        effects = h.fire(SetEnabled(True), at=2.0)
        expect_ramp(effects, SOFAKROK, 0.15, duration=2.0)  # capped on re-enable

    def test_report_above_cap_while_disabled_is_ignored(self) -> None:
        h = Harness()
        h.fire(SetEnabled(False), at=0.0)
        h.fire(SetNightMode(True), at=1.0)
        assert h.fire(ExternalVolume(SOFAKROK, 0.4), at=20.0) == []
        assert h.state.speakers[SOFAKROK].volume == 0.4  # state stays fresh


class TestRule45ReverseSync:
    def test_rule_4_5_report_above_cap_pulled_back_no_sync(self) -> None:
        """R11: knob above the cap -> one corrective ramp, master untouched."""
        h = Harness()
        h.fire(SetNightMode(True), at=0.0)  # sofakrok commanded 0.15
        effects = h.fire(ExternalVolume(SOFAKROK, 0.4), at=20.0)
        assert timer_starts(effects) == []  # never debounced (4.5)
        expect_ramp(effects, SOFAKROK, 0.15, duration=2.0)  # pulled back to the cap
        assert h.state.master == pytest.approx(0.3)  # never corrupted
        assert h.state.speakers[SOFAKROK].pending_external is None
        assert h.state.speakers[SOFAKROK].commanded == pytest.approx(0.15)

    def test_rule_4_5_no_ping_pong(self) -> None:
        """R11: convergence reports cause nothing; a knob fight re-corrects."""
        h = Harness()
        h.fire(SetNightMode(True), at=0.0)
        h.fire(ExternalVolume(SOFAKROK, 0.4), at=20.0)  # pulled back
        # The corrective ramp's own echo is adapter-suppressed; even if it
        # leaked through, a report at the cap is discarded (4.1).
        assert h.fire(ExternalVolume(SOFAKROK, 0.15), at=25.0) == []
        # The user fights back: exactly one corrective ramp again.
        effects = h.fire(ExternalVolume(SOFAKROK, 0.5), at=30.0)
        assert ramps(effects) == [r for r in ramps(effects) if r.speaker_id == SOFAKROK]
        expect_ramp(effects, SOFAKROK, 0.15, duration=2.0)
        assert h.state.master == pytest.approx(0.3)

    def test_rule_4_1_report_below_cap_discarded_while_night(self) -> None:
        h = Harness()
        h.fire(SetNightMode(True), at=0.0)
        assert h.fire(ExternalVolume(SOFAKROK, 0.10), at=20.0) == []
        assert h.state.master == pytest.approx(0.3)
        assert h.state.speakers[SOFAKROK].volume == 0.10  # 4.1: always updated

    def test_rule_4_5_pull_back_targets_desired_not_cap(self) -> None:
        # With master below the cap, the pull-back lands on the real target.
        h = Harness()
        h.fire(SetMaster(0.1), at=0.0)  # sofakrok at 0.1
        h.fire(SetNightMode(True), at=1.0)  # no write: already under the cap
        effects = h.fire(ExternalVolume(SOFAKROK, 0.3), at=20.0)
        expect_ramp(effects, SOFAKROK, 0.1, duration=2.0)
        assert h.state.master == pytest.approx(0.1)

    def test_rule_4_5_ignores_non_audible_zone(self) -> None:
        h = Harness()
        h.fire(SetNightMode(True), at=0.0)
        assert h.fire(ExternalVolume(KJOKKEN, 0.4), at=20.0) == []  # kjokken IDLE
        assert h.state.speakers[KJOKKEN].volume == 0.4

    def test_rule_4_5_ignores_standalone_speaker(self) -> None:
        h = Harness(snapshot=make_snapshot(night_mode=True, docked={KJOKKEN: False}))
        assert h.state.zones["kjokken"].phase is ZonePhase.STANDALONE
        assert h.fire(ExternalVolume(KJOKKEN, 0.9), at=20.0) == []

    def test_night_change_opens_suppression_window(self) -> None:
        h = Harness()
        h.fire(SetNightMode(True), at=0.0)
        h.fire(SetNightMode(False), at=1.0)  # mode change at t=1
        assert h.fire(ExternalVolume(SOFAKROK, 0.4), at=5.0) == []
        assert timer_starts(h.fire(ExternalVolume(SOFAKROK, 0.4), at=11.5)) != []

    def test_pending_debounce_discarded_when_night_engages(self) -> None:
        h = Harness()
        h.fire(SetMaster(0.1), at=0.0)  # sofakrok at 0.1, under the cap
        effects = h.fire(ExternalVolume(SOFAKROK, 0.14), at=20.0)
        assert timer_starts(effects) != []  # accepted, debouncing
        assert h.fire(SetNightMode(True), at=20.5) == []  # no write: pending survives
        effects = h.fire_timer(timers.external_debounce(SOFAKROK))  # 4.3 re-check
        assert effects == []
        assert h.state.master == pytest.approx(0.1)
        assert h.state.speakers[SOFAKROK].pending_external is None  # consumed


class TestNightTvSoloComposition:
    def test_suppressed_zones_stay_silent_audible_zones_capped(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=1.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)  # kjokken suppressed
        effects = h.fire(SetNightMode(True), at=3.0)
        expect_ramp(effects, SOFAKROK, 0.15, duration=2.0)  # TV zone capped
        expect_no_ramp(effects, KJOKKEN)  # suppressed: stays at 0
        assert len(ramps(effects)) == 1

    def test_tv_stop_restores_suppressed_zone_at_cap(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=1.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)
        h.fire(SetNightMode(True), at=3.0)
        effects = h.fire(TvPlayingChanged("sofakrok", False), at=30.0)
        expect_ramp(effects, KJOKKEN, 0.15, duration=2.0)  # restored, capped
        expect_no_ramp(effects, SOFAKROK)  # RELEASING: still at the cap

    def test_solo_mode_relax_brings_zone_in_at_cap(self) -> None:
        """Movie night in night mode: relaxing TV_ZONE -> SAME_ROOM fades the
        same-room zone in at the night cap, never its full target."""
        h = Harness()
        h.occupy("sofakrok", at=0.0)
        h.occupy("spisebord", at=0.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=1.0)
        h.fire(SetNightMode(True), at=2.0)
        h.fire(SetTvSoloMode(TvSoloMode.TV_ZONE), at=3.0)  # spisebord silenced

        effects = h.fire(SetTvSoloMode(TvSoloMode.SAME_ROOM), at=10.0)
        # Uncapped target would be 0.3 * 1.1 * 1.0 (TV forces unity scale) =
        # 0.33; the night ceiling wins.
        expect_ramp(effects, SPISEBORD, 0.15, duration=2.0)
        expect_no_ramp(effects, SOFAKROK)  # already at the cap
        assert len(ramps(effects)) == 1
