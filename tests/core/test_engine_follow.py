"""Rule 1.9: follow mode (per-zone / per-room / all-speakers).

Follow mode selects which presence a zone follows to become audible. It is
orthogonal to TV-solo suppression (rule 6.2): suppression is applied on top
of audibility, so a soloing TV silences other zones even in ALL_SPEAKERS.
"""

from __future__ import annotations

from custom_components.sonos_conductor.core import reconcile, timers
from custom_components.sonos_conductor.core.events import (
    SetEnabled,
    SetFollowMode,
    TvPlayingChanged,
)
from custom_components.sonos_conductor.core.model import FollowMode, TvSoloMode, ZonePhase

from .harness import (
    KJOKKEN,
    SOFAKROK,
    SPISEBORD,
    STUE_2,
    Harness,
    expect_no_volume_effects,
    expect_ramp,
    make_snapshot,
    ramps,
    timer_starts,
)


def _audible(h: Harness, zone_id: str) -> bool:
    return reconcile.is_audible(h.engine, zone_id)


class TestDefault:
    def test_default_follow_mode_is_per_zone(self) -> None:
        h = Harness()
        assert h.state.follow_mode is FollowMode.PER_ZONE


class TestPerZone:
    def test_occupancy_stays_within_its_own_zone(self) -> None:
        """PER_ZONE (default): occupying spisebord leaves its room-mate
        sofakrok idle — the fallback simply hands over 1:1."""
        h = Harness()  # sofakrok forced ACTIVE at seed
        effects = h.occupy("spisebord", at=1.0)
        assert h.state.zones["spisebord"].phase is ZonePhase.ACTIVE
        assert h.state.zones["sofakrok"].phase is ZonePhase.IDLE  # fallback retires
        # Sole audible zone in stue -> full scale, no 1/sqrt(2) split.
        expect_ramp(effects, SPISEBORD, 0.3 * 1.1, duration=3.0)
        expect_ramp(effects, SOFAKROK, 0.0, duration=5.0)


class TestPerRoom:
    def test_occupancy_lights_the_whole_room(self) -> None:
        """PER_ROOM: occupying spisebord also makes its room-mate sofakrok
        audible (on its own merits, not forced)."""
        h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.PER_ROOM))
        effects = h.occupy("spisebord", at=1.0)
        assert h.state.zones["spisebord"].phase is ZonePhase.ACTIVE
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE
        assert not h.engine._fallback_forced  # sofakrok earns its audibility
        # Both stue zones audible -> each scaled by 1/sqrt(2).
        expect_ramp(effects, SPISEBORD, 0.3 * 1.1 * STUE_2, duration=3.0)
        expect_ramp(effects, SOFAKROK, 0.3 * 1.0 * STUE_2, duration=2.0)  # rebalance

    def test_other_rooms_are_unaffected(self) -> None:
        """PER_ROOM couples zones by room only: the kitchen stays out of it."""
        h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.PER_ROOM))
        h.occupy("spisebord", at=1.0)
        assert h.state.zones["kjokken"].phase is ZonePhase.IDLE

    def test_vacating_the_room_releases_every_zone_in_it(self) -> None:
        """When the whole room empties, all its zones release together."""
        h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.PER_ROOM))
        h.occupy("spisebord", at=1.0)
        effects = h.vacate("spisebord", at=2.0)
        assert h.state.zones["spisebord"].phase is ZonePhase.RELEASING
        assert h.state.zones["sofakrok"].phase is ZonePhase.RELEASING
        started = {t.timer_id for t in timer_starts(effects)}
        assert started == {timers.zone_release("spisebord"), timers.zone_release("sofakrok")}
        expect_no_volume_effects(effects)  # RELEASING keeps volume

    def test_seeds_the_room_active_from_a_single_occupant(self) -> None:
        """Seeding PER_ROOM with one occupant lights the whole room."""
        h = Harness(
            snapshot=make_snapshot(follow_mode=FollowMode.PER_ROOM, occupancy={"spisebord": True})
        )
        assert h.state.zones["spisebord"].phase is ZonePhase.ACTIVE
        assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE
        assert h.state.zones["kjokken"].phase is ZonePhase.IDLE


class TestAllSpeakers:
    def test_every_zone_is_audible_without_presence(self) -> None:
        h = Harness()  # PER_ZONE, only the forced fallback is audible
        effects = h.fire(SetFollowMode(FollowMode.ALL_SPEAKERS), at=1.0)
        for zone_id in ("kjokken", "spisebord", "sofakrok"):
            assert h.state.zones[zone_id].phase is ZonePhase.ACTIVE
        expect_ramp(effects, KJOKKEN, 0.3 * 1.2, duration=3.0)  # own room, fade_in
        expect_ramp(effects, SPISEBORD, 0.3 * 1.1 * STUE_2, duration=3.0)
        expect_ramp(effects, SOFAKROK, 0.3 * 1.0 * STUE_2, duration=2.0)  # rebalance down

    def test_tv_solo_takes_precedence_over_all_speakers(self) -> None:
        """The user's key requirement: even in ALL_SPEAKERS, a soloing TV
        (tv_zone) silences every zone but the TV's own."""
        h = Harness(snapshot=make_snapshot(tv_solo_mode=TvSoloMode.TV_ZONE))
        h.fire(TvPlayingChanged("sofakrok", True), at=1.0)
        h.fire(SetFollowMode(FollowMode.ALL_SPEAKERS), at=2.0)
        # All three zones are ACTIVE per follow mode...
        for zone_id in ("kjokken", "spisebord", "sofakrok"):
            assert h.state.zones[zone_id].phase is ZonePhase.ACTIVE
        # ...but suppression keeps everything but the TV zone silent.
        assert h.state.suppressed == frozenset({"kjokken", "spisebord"})
        assert _audible(h, "sofakrok")
        assert not _audible(h, "kjokken")
        assert not _audible(h, "spisebord")
        # Desired volumes: only the TV zone plays; the others stay at zero
        # regardless of the follow mode.
        assert reconcile.desired(h.engine, KJOKKEN) == 0.0
        assert reconcile.desired(h.engine, SPISEBORD) == 0.0
        assert reconcile.desired(h.engine, SOFAKROK) == 0.3 * 1.0  # sole audible stue zone

    def test_same_room_solo_still_narrows_within_all_speakers(self) -> None:
        h = Harness(snapshot=make_snapshot(tv_solo_mode=TvSoloMode.SAME_ROOM))
        h.fire(SetFollowMode(FollowMode.ALL_SPEAKERS), at=1.0)
        h.fire(TvPlayingChanged("sofakrok", True), at=2.0)
        # SAME_ROOM: only the kitchen (no TV in its room) is suppressed; the
        # stue keeps both zones.
        assert h.state.suppressed == frozenset({"kjokken"})
        assert _audible(h, "spisebord")
        assert _audible(h, "sofakrok")
        assert not _audible(h, "kjokken")


class TestSwitchingBack:
    def test_all_speakers_to_per_zone_releases_the_unoccupied_zones(self) -> None:
        """Leaving ALL_SPEAKERS with nobody home releases the extra zones
        gracefully (hold timers), not abruptly."""
        h = Harness()
        h.fire(SetFollowMode(FollowMode.ALL_SPEAKERS), at=1.0)
        effects = h.fire(SetFollowMode(FollowMode.PER_ZONE), at=2.0)
        assert h.state.zones["kjokken"].phase is ZonePhase.RELEASING
        assert h.state.zones["spisebord"].phase is ZonePhase.RELEASING
        started = {t.timer_id for t in timer_starts(effects)}
        assert timers.zone_release("kjokken") in started
        assert timers.zone_release("spisebord") in started
        expect_no_volume_effects(effects)  # RELEASING keeps volume


class TestDisabled:
    def test_set_follow_mode_while_disabled_is_inert(self) -> None:
        h = Harness(snapshot=make_snapshot(enabled=False))
        effects = h.fire(SetFollowMode(FollowMode.ALL_SPEAKERS), at=1.0)
        assert effects == []  # 8.1: reconcile stays inert
        assert h.state.follow_mode is FollowMode.ALL_SPEAKERS  # state still fresh

    def test_world_model_stays_fresh_for_re_enable(self) -> None:
        """A follow-mode change while disabled is reflected the moment the
        conductor is re-enabled (rule 8.2)."""
        h = Harness(snapshot=make_snapshot(enabled=False))
        h.fire(SetFollowMode(FollowMode.ALL_SPEAKERS), at=1.0)
        effects = h.fire(SetEnabled(True), at=2.0)
        for zone_id in ("kjokken", "spisebord", "sofakrok"):
            assert h.state.zones[zone_id].phase is ZonePhase.ACTIVE
        assert ramps(effects)  # converges the newly-audible zones
