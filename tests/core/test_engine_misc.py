"""Rule 10 miscellany plus edge cases: unknown ids, ordering, boundaries."""

from __future__ import annotations

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.effects import (
    CancelTimer,
    RampVolume,
    SetSpeakerMute,
    StartTimer,
)
from custom_components.sonos_conductor.core.events import (
    DockChanged,
    DuckChanged,
    Event,
    ExternalMute,
    ExternalVolume,
    OccupancyChanged,
    PlaybackChanged,
    SetMaster,
    SetMute,
    SetTrim,
    SetTvSolo,
    TimerFired,
    TvPlayingChanged,
)
from custom_components.sonos_conductor.core.model import (
    ConductorConfig,
    InitialSnapshot,
    SpeakerConfig,
    Tunables,
    ZoneConfig,
    ZonePhase,
)

from .harness import (
    DOOR,
    KJOKKEN,
    SOFAKROK,
    Harness,
    expect_ramp,
    make_config_with_extra_speaker,
    ramps,
    timer_starts,
)


class TestRule101Trim:
    def test_rule_10_1_set_trim_reconciles_with_rebalance(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.fire(SetTrim(KJOKKEN, 1.0), at=1.0)
        expect_ramp(effects, KJOKKEN, 0.3, duration=2.0)

    def test_rule_10_1_unknown_speaker_ignored(self) -> None:
        h = Harness()
        assert h.fire(SetTrim("media_player.bogus", 2.0), at=0.0) == []

    def test_rule_10_1_negative_trim_clamped_to_zero(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.fire(SetTrim(KJOKKEN, -1.0), at=1.0)
        expect_ramp(effects, KJOKKEN, 0.0, duration=2.0)

    def test_trim_above_one_clamps_target_not_trim(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)  # trim 1.2
        effects = h.fire(SetMaster(0.9), at=1.0)
        expect_ramp(effects, KJOKKEN, 1.0, duration=0.0)  # 1.08 clamped

    def test_set_trim_same_value_is_silent(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        assert h.fire(SetTrim(KJOKKEN, 1.2), at=1.0) == []


class TestRule102To104Robustness:
    def test_rule_10_2_unknown_timer_ids_ignored(self) -> None:
        h = Harness()
        assert h.fire(TimerFired("bogus"), at=0.0) == []
        assert h.fire(TimerFired(timers.zone_release("bogus")), at=1.0) == []
        assert h.fire(TimerFired(timers.external_debounce("bogus")), at=2.0) == []
        assert h.fire(TimerFired(timers.GROUP_REPAIR), at=3.0) == []  # never started

    def test_rule_10_3_playback_updates_state_only(self) -> None:
        h = Harness()
        assert h.fire(PlaybackChanged(SOFAKROK, True), at=0.0) == []
        assert h.state.speakers[SOFAKROK].playing is True

    def test_rule_10_4_unknown_ids_never_raise(self) -> None:
        h = Harness()
        events = [
            OccupancyChanged("garage", True),
            TvPlayingChanged("garage", True),
            DockChanged("media_player.bogus", False),
            DuckChanged("binary_sensor.bogus", True),
            ExternalVolume("media_player.bogus", 0.5),
            ExternalMute("media_player.bogus", True),
            PlaybackChanged("media_player.bogus", True),
            SetTrim("media_player.bogus", 1.0),
        ]
        for i, event in enumerate(events):
            assert h.fire(event, at=float(i)) == [], event


class TestRule105Ordering:
    def test_rule_10_5_cancels_then_mutes_then_ramps(self) -> None:
        h = Harness()
        h.occupy("sofakrok", at=0.0)
        h.fire(ExternalVolume(SOFAKROK, 0.4), at=1.0)  # debounce pending
        h.fire(SetMute(True), at=2.0)  # pending survives (no reconcile write)
        h.fire(SetMaster(0.6), at=3.0)  # stored only while muted
        effects = h.fire(SetMute(False), at=4.0)
        assert [type(e) for e in effects] == [
            CancelTimer,  # superseded debounce
            SetSpeakerMute,
            SetSpeakerMute,
            SetSpeakerMute,
            RampVolume,
        ]
        assert effects[0] == CancelTimer(timers.external_debounce(SOFAKROK))
        assert effects[-1] == RampVolume(SOFAKROK, 0.6, 2.0)

    def test_rule_10_5_ramps_before_start_timers(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        h.fire(SetTvSolo(True), at=0.5)
        h.fire(TvPlayingChanged("sofakrok", True), at=1.0)  # kjokken suppressed
        # TV stops: kjokken restore ramp + sofakrok hold timer, in that order.
        effects = h.fire(TvPlayingChanged("sofakrok", False), at=2.0)
        kinds = [type(e) for e in effects]
        assert kinds.index(RampVolume) < kinds.index(StartTimer)


class TestBoundaries:
    def test_master_zero_silences_audible_zones(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.fire(SetMaster(0.0), at=1.0)
        expect_ramp(effects, KJOKKEN, 0.0, duration=0.0)

    def test_float_dust_never_causes_spurious_ramps(self) -> None:
        h = Harness()
        first = expect_ramp(h.occupy("kjokken", at=0.0), KJOKKEN, 0.36, duration=3.0)
        assert h.fire(SetMaster(0.3), at=1.0) == []
        h.fire(DuckChanged(DOOR, True), at=2.0)
        restored = expect_ramp(h.fire(DuckChanged(DOOR, False), at=3.0), KJOKKEN, 0.36)
        assert restored.target == first.target  # exact
        assert h.fire(SetTrim(KJOKKEN, 1.2), at=4.0) == []
        assert h.fire(SetMaster(0.3), at=5.0) == []

    def test_zone_without_occupancy_sensors_is_tv_driven(self) -> None:
        # The engine is agnostic: a zone that never sees occupancy events
        # lives entirely off TvPlayingChanged.
        h = Harness()
        effects = h.fire(TvPlayingChanged("kjokken", True), at=0.0)
        expect_ramp(effects, KJOKKEN, 0.36, duration=3.0)
        expect_ramp(effects, SOFAKROK, 0.0, duration=5.0)  # fallback yields
        effects = h.fire(TvPlayingChanged("kjokken", False), at=1.0)
        assert timer_starts(effects) == [StartTimer(timers.zone_release("kjokken"), 60.0)]


def _single_speaker(fallback: bool) -> tuple[ConductorConfig, InitialSnapshot]:
    speaker = "media_player.solo"
    config = ConductorConfig(
        speakers=(SpeakerConfig(speaker, "Solo"),),
        zones=(
            ZoneConfig(
                "solo", "Solo", speaker, room_id="solo", hold_seconds=10.0, fallback=fallback
            ),
        ),
        tunables=Tunables(),
    )
    snapshot = InitialSnapshot(
        occupancy={},
        tv_playing={},
        docked={},
        volumes={speaker: 0.3 if fallback else 0.0},
        muted={},
        playing={},
        group_members={},
        duck_active={},
        master=0.3,
    )
    return config, snapshot


class TestSingleSpeaker:
    SPEAKER = "media_player.solo"

    def test_single_speaker_lifecycle(self) -> None:
        config, snapshot = _single_speaker(fallback=False)
        h = Harness(config=config, snapshot=snapshot)
        assert h.start_effects == []
        effects = h.occupy("solo", at=0.0)
        expect_ramp(effects, self.SPEAKER, 0.3, duration=3.0)
        effects = h.vacate("solo", at=5.0)
        assert timer_starts(effects) == [StartTimer(timers.zone_release("solo"), 10.0)]
        effects = h.fire_timer(timers.zone_release("solo"))
        expect_ramp(effects, self.SPEAKER, 0.0, duration=5.0)

    def test_single_fallback_zone_is_always_audible(self) -> None:
        config, snapshot = _single_speaker(fallback=True)
        h = Harness(config=config, snapshot=snapshot)
        assert h.start_effects == []
        assert h.state.zones["solo"].phase is ZonePhase.ACTIVE
        h.occupy("solo", at=0.0)
        h.vacate("solo", at=5.0)
        effects = h.fire_timer(timers.zone_release("solo"))
        assert ramps(effects) == []  # re-forced: never fades out
        assert h.state.zones["solo"].phase is ZonePhase.ACTIVE


class TestUnmanagedSpeaker:
    """A configured speaker without a zone is never volume-managed."""

    EXTRA = "media_player.kontor"

    def _harness(self) -> Harness:
        base = make_config_with_extra_speaker(self.EXTRA)
        snapshot = InitialSnapshot(
            occupancy={},
            tv_playing={},
            docked={},
            volumes={self.EXTRA: 0.8},
            muted={},
            playing={},
            group_members={},
            duck_active={},
            master=0.3,
        )
        return Harness(config=base, snapshot=snapshot)

    def test_zone_less_speaker_never_ramped(self) -> None:
        h = self._harness()
        assert not [r for r in ramps(h.start_effects) if r.speaker_id == self.EXTRA]
        h.occupy("kjokken", at=0.0)
        effects = h.fire(SetMaster(0.6), at=1.0)
        assert not [r for r in ramps(effects) if r.speaker_id == self.EXTRA]

    def test_zone_less_speaker_still_gets_mute_fanout(self) -> None:
        h = self._harness()
        effects = h.fire(SetMute(True), at=0.0)
        assert any(isinstance(e, SetSpeakerMute) and e.speaker_id == self.EXTRA for e in effects)


class TestDefensiveGuards:
    """Adapter-race guards: states that only a misbehaving adapter produces."""

    def test_unknown_event_type_is_ignored(self) -> None:
        class Mystery(Event):
            pass

        h = Harness()
        assert h.fire(Mystery(), at=0.0) == []

    def test_pending_release_timer_with_non_releasing_zone(self) -> None:
        h = Harness()
        h.occupy("spisebord", at=0.0)
        h.vacate("spisebord", at=5.0)  # timer pending, phase RELEASING
        h.state.zones["spisebord"].phase = ZonePhase.ACTIVE  # simulated race
        assert h.fire_timer(timers.zone_release("spisebord")) == []

    def test_pending_debounce_with_cleared_value(self) -> None:
        h = Harness()
        h.fire(ExternalVolume(SOFAKROK, 0.4), at=0.0)
        h.state.speakers[SOFAKROK].pending_external = None  # simulated race
        assert h.fire_timer(timers.external_debounce(SOFAKROK)) == []
