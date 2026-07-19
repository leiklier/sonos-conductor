"""Follow mode (spec rule 1.9): how far a zone's presence spreads audibility.

PER_ZONE is the legacy behavior (a zone follows only its own presence);
PER_ROOM and ALL_SPEAKERS widen the trigger. Follow mode is orthogonal to
TV solo (rule 6.2): TV suppression still applies on top of whatever the
follow mode makes audible.

The legacy topology (see harness): kjokken (room kjokken), spisebord and
sofakrok (both room stue); sofakrok is the fallback zone and has the TV.
"""

from __future__ import annotations

from custom_components.sonos_conductor.core import reconcile, timers
from custom_components.sonos_conductor.core.events import (
    DockChanged,
    DuckChanged,
    HomePresenceChanged,
    SetFollowMode,
    TvPlayingChanged,
)
from custom_components.sonos_conductor.core.model import FollowMode, TvSoloMode, ZonePhase
from tests.core.harness import (
    DOOR,
    KJOKKEN,
    SOFAKROK,
    SPISEBORD,
    STUE_2,
    Harness,
    expect_no_ramp,
    expect_no_volume_effects,
    expect_ramp,
    make_snapshot,
)

MASTER = 0.3  # make_snapshot default


def _audible(harness: Harness, zone_id: str) -> bool:
    return reconcile.is_audible(harness.engine, zone_id)


def _phase(harness: Harness, zone_id: str) -> ZonePhase:
    return harness.state.zones[zone_id].phase


# ---------------------------------------------------------------------
# PER_ZONE: the default is unchanged (a zone follows only itself)
# ---------------------------------------------------------------------


def test_per_zone_does_not_spread() -> None:
    """Occupying one zone never wakes its room-mates in PER_ZONE."""
    h = Harness()  # PER_ZONE default
    h.occupy("spisebord")
    assert _phase(h, "spisebord") is ZonePhase.ACTIVE
    # sofakrok shares the room but must stay put (its forced fallback retires
    # the moment spisebord earns audibility).
    assert _phase(h, "sofakrok") is ZonePhase.IDLE
    assert _phase(h, "kjokken") is ZonePhase.IDLE


# ---------------------------------------------------------------------
# PER_ROOM: presence wakes every zone sharing the acoustic room
# ---------------------------------------------------------------------


def test_per_room_spreads_within_room_only() -> None:
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.PER_ROOM))
    effects = h.occupy("spisebord")

    # Both stue zones wake; the kitchen (another room) stays idle.
    assert _phase(h, "spisebord") is ZonePhase.ACTIVE
    assert _phase(h, "sofakrok") is ZonePhase.ACTIVE
    assert _phase(h, "kjokken") is ZonePhase.IDLE

    # Two audible zones in the stue -> 1/sqrt(2) acoustic split.
    expect_ramp(effects, SPISEBORD, MASTER * 1.1 * STUE_2)
    expect_ramp(effects, SOFAKROK, MASTER * 1.0 * STUE_2)
    expect_no_ramp(effects, KJOKKEN)


def test_per_room_releases_together_then_fallback_returns() -> None:
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.PER_ROOM))
    h.occupy("spisebord")

    # Leaving the room releases every room-mate; both linger during the hold.
    h.vacate("spisebord")
    assert _phase(h, "spisebord") is ZonePhase.RELEASING
    assert _phase(h, "sofakrok") is ZonePhase.RELEASING

    h.fire_timer(timers.zone_release("spisebord"))
    assert _phase(h, "sofakrok") is ZonePhase.RELEASING  # sole survivor holds
    h.fire_timer(timers.zone_release("sofakrok"))

    # Nothing audible -> the fallback zone is forced back on (rule 1.5).
    assert _phase(h, "spisebord") is ZonePhase.IDLE
    assert _phase(h, "sofakrok") is ZonePhase.ACTIVE


# ---------------------------------------------------------------------
# ALL_SPEAKERS: presence anywhere wakes the whole house (presence-gated)
# ---------------------------------------------------------------------


def test_all_speakers_wakes_every_zone() -> None:
    # anyone_home=False so the wake is driven purely by zone presence.
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.ALL_SPEAKERS, anyone_home=False))
    effects = h.occupy("kjokken")

    for zone_id in ("kjokken", "spisebord", "sofakrok"):
        assert _phase(h, zone_id) is ZonePhase.ACTIVE

    # Kitchen alone in its room -> full scale; the two stue zones split.
    expect_ramp(effects, KJOKKEN, MASTER * 1.2)
    expect_ramp(effects, SPISEBORD, MASTER * 1.1 * STUE_2)
    expect_ramp(effects, SOFAKROK, MASTER * 1.0 * STUE_2)


def test_all_speakers_no_home_input_keeps_whole_house_on() -> None:
    """anyone_home=None behaves as present (rule 1.8 fail-safe): with no
    home input at all, ALL_SPEAKERS keeps every zone audible — the
    whole-house analogue of the fallback's "music never dies"."""
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.ALL_SPEAKERS))  # anyone_home=None
    for zone_id in ("kjokken", "spisebord", "sofakrok"):
        assert _phase(h, zone_id) is ZonePhase.ACTIVE


def test_all_speakers_respects_empty_home() -> None:
    """anyone_home=False empties the house even in ALL_SPEAKERS."""
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.ALL_SPEAKERS, anyone_home=False))
    for zone_id in ("kjokken", "spisebord", "sofakrok"):
        assert _phase(h, zone_id) is ZonePhase.IDLE


def test_all_speakers_whole_house_releases_when_empty() -> None:
    # anyone_home=False: the only presence is the kitchen zone's own sensor.
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.ALL_SPEAKERS, anyone_home=False))
    h.occupy("kjokken")
    h.vacate("kjokken")
    for zone_id in ("kjokken", "spisebord", "sofakrok"):
        assert _phase(h, zone_id) is ZonePhase.RELEASING


def test_all_speakers_home_presence_alone_keeps_whole_house_on() -> None:
    """anyone_home=True counts as presence anywhere in ALL_SPEAKERS.

    The point of the mode is sound in every zone while someone is home —
    someone outside every zone (bedroom, bathroom) must not scale the house
    down. Seeds whole-house on, and zone vacancy never starts a release.
    """
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.ALL_SPEAKERS, anyone_home=True))
    for zone_id in ("kjokken", "spisebord", "sofakrok"):
        assert _phase(h, zone_id) is ZonePhase.ACTIVE  # seeded on (9.1)
    h.occupy("kjokken")
    effects = h.vacate("kjokken")
    for zone_id in ("kjokken", "spisebord", "sofakrok"):
        assert _phase(h, zone_id) is ZonePhase.ACTIVE  # anyone_home holds it
    expect_no_volume_effects(effects)


def test_all_speakers_home_departure_releases_whole_house() -> None:
    """anyone_home True->False in ALL_SPEAKERS: the house releases through
    the normal holds, and the fallback is not forced (rule 1.8)."""
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.ALL_SPEAKERS, anyone_home=True))
    h.fire(HomePresenceChanged(False))
    for zone_id in ("kjokken", "spisebord", "sofakrok"):
        assert _phase(h, zone_id) is ZonePhase.RELEASING
        h.fire_timer(timers.zone_release(zone_id))
    for zone_id in ("kjokken", "spisebord", "sofakrok"):
        assert _phase(h, zone_id) is ZonePhase.IDLE  # empty home: silence


def test_all_speakers_home_arrival_fades_whole_house_in() -> None:
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.ALL_SPEAKERS, anyone_home=False))
    effects = h.fire(HomePresenceChanged(True))
    for zone_id in ("kjokken", "spisebord", "sofakrok"):
        assert _phase(h, zone_id) is ZonePhase.ACTIVE
    expect_ramp(effects, KJOKKEN, MASTER * 1.2)
    expect_ramp(effects, SPISEBORD, MASTER * 1.1 * STUE_2)
    expect_ramp(effects, SOFAKROK, MASTER * 1.0 * STUE_2)


def test_all_speakers_blind_home_sensor_keeps_house_on() -> None:
    """anyone_home True->None (estimator blind): blind is not absent — the
    fail-safe (rule 1.8) keeps the whole house playing, silently."""
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.ALL_SPEAKERS, anyone_home=True))
    effects = h.fire(HomePresenceChanged(None))
    for zone_id in ("kjokken", "spisebord", "sofakrok"):
        assert _phase(h, zone_id) is ZonePhase.ACTIVE
    expect_no_volume_effects(effects)


# ---------------------------------------------------------------------
# TV solo takes precedence over the follow mode (they are orthogonal)
# ---------------------------------------------------------------------


def test_tv_solo_overrides_all_speakers() -> None:
    """ALL_SPEAKERS wakes every FSM, but TV_ZONE still silences all but the TV."""
    h = Harness(
        snapshot=make_snapshot(follow_mode=FollowMode.ALL_SPEAKERS, tv_solo_mode=TvSoloMode.TV_ZONE)
    )
    h.fire(TvPlayingChanged("sofakrok", True))  # movie night on the TV zone
    h.occupy("kjokken")

    # Every zone is ACTIVE (all_speakers) ...
    for zone_id in ("kjokken", "spisebord", "sofakrok"):
        assert _phase(h, zone_id) is ZonePhase.ACTIVE
    # ... but TV solo suppresses everything except the TV zone.
    assert _audible(h, "sofakrok")
    assert not _audible(h, "kjokken")
    assert not _audible(h, "spisebord")
    assert reconcile.desired(h.engine, KJOKKEN) == 0.0
    assert reconcile.desired(h.engine, SPISEBORD) == 0.0
    assert reconcile.desired(h.engine, SOFAKROK) == MASTER * 1.0  # TV forces unity scale


def test_same_room_solo_with_per_room_follow() -> None:
    """PER_ROOM wakes the kitchen; SAME_ROOM TV solo still silences it."""
    h = Harness(
        snapshot=make_snapshot(follow_mode=FollowMode.PER_ROOM, tv_solo_mode=TvSoloMode.SAME_ROOM)
    )
    h.fire(TvPlayingChanged("sofakrok", True))  # TV in the stue
    h.occupy("kjokken")  # kitchen room wakes only the kitchen zone

    assert _phase(h, "kjokken") is ZonePhase.ACTIVE
    assert not _audible(h, "kjokken")  # suppressed: no TV in the kitchen room
    assert _audible(h, "sofakrok")  # the TV room plays


# ---------------------------------------------------------------------
# Ducking and docking compose with the follow mode unchanged
# ---------------------------------------------------------------------


def test_all_speakers_ducking_caps_and_restores_whole_house() -> None:
    """The duck cap applies in desired() (spec section 0), downstream of the
    follow mode: the whole audible house caps and restores together."""
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.ALL_SPEAKERS, anyone_home=True))
    # Whole house on; adopt the startup convergence targets as commanded.
    targets = {
        KJOKKEN: MASTER * 1.2,
        SPISEBORD: MASTER * 1.1 * STUE_2,
        SOFAKROK: MASTER * 1.0 * STUE_2,
    }
    effects = h.fire(DuckChanged(DOOR, True))
    for speaker in (KJOKKEN, SPISEBORD, SOFAKROK):
        expect_ramp(effects, speaker, 0.05, duration=0.0)  # engage_fade
    effects = h.fire(DuckChanged(DOOR, False))
    for speaker, target in targets.items():
        expect_ramp(effects, speaker, target, duration=2.0)  # release_fade


def test_all_speakers_undock_leaves_standalone_redock_rejoins() -> None:
    """STANDALONE wins over the follow mode: all_speakers cannot drag an
    undocked Move back in; redocking rejoins with a fade-in (rule 2.2)."""
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.ALL_SPEAKERS, anyone_home=True))
    effects = h.fire(DockChanged(KJOKKEN, False))
    assert _phase(h, "kjokken") is ZonePhase.STANDALONE
    expect_no_ramp(effects, KJOKKEN)  # handed over untouched (2.1)
    assert reconcile.desired(h.engine, KJOKKEN) is None  # never touch it

    effects = h.fire(DockChanged(KJOKKEN, True))
    assert _phase(h, "kjokken") is ZonePhase.ACTIVE  # anyone home: rejoins
    expect_ramp(effects, KJOKKEN, MASTER * 1.2, duration=3.0)  # fade_in


# ---------------------------------------------------------------------
# Changing the mode at runtime (SetFollowMode)
# ---------------------------------------------------------------------


def test_set_follow_mode_widens_then_narrows() -> None:
    h = Harness()  # PER_ZONE
    h.occupy("spisebord")
    assert _phase(h, "sofakrok") is ZonePhase.IDLE  # room-mate untouched

    # Widen to PER_ROOM: the room-mate fades in.
    effects = h.fire(SetFollowMode(FollowMode.PER_ROOM))
    assert _phase(h, "sofakrok") is ZonePhase.ACTIVE
    expect_ramp(effects, SOFAKROK, MASTER * 1.0 * STUE_2)

    # Narrow back to PER_ZONE: the room-mate loses its trigger and releases
    # gracefully (hold), rather than cutting out.
    h.fire(SetFollowMode(FollowMode.PER_ZONE))
    assert _phase(h, "sofakrok") is ZonePhase.RELEASING
    assert _phase(h, "spisebord") is ZonePhase.ACTIVE  # still occupied


def test_set_follow_mode_noop_when_unchanged() -> None:
    h = Harness()
    h.occupy("kjokken")
    effects = h.fire(SetFollowMode(FollowMode.PER_ZONE))  # already PER_ZONE
    expect_no_volume_effects(effects)


# ---------------------------------------------------------------------
# Seeding (rule 9.1): effective occupancy is resolved across all zones
# ---------------------------------------------------------------------


def test_seed_per_room_audible_from_neighbor() -> None:
    """A zone seeds audible when a room-mate is occupied under PER_ROOM."""
    h = Harness(
        snapshot=make_snapshot(follow_mode=FollowMode.PER_ROOM, occupancy={"spisebord": True})
    )
    assert _phase(h, "spisebord") is ZonePhase.ACTIVE
    assert _phase(h, "sofakrok") is ZonePhase.ACTIVE  # woken by its room-mate
    assert _phase(h, "kjokken") is ZonePhase.IDLE


# ---------------------------------------------------------------------
# Disabled engine keeps the world model fresh (rule 8.1)
# ---------------------------------------------------------------------


def test_disabled_per_room_recomputes_neighbors_without_effects() -> None:
    h = Harness(snapshot=make_snapshot(follow_mode=FollowMode.PER_ROOM, enabled=False))
    effects = h.occupy("spisebord")
    # Phases stay current for both room-mates, but nothing is emitted.
    assert _phase(h, "spisebord") is ZonePhase.ACTIVE
    assert _phase(h, "sofakrok") is ZonePhase.ACTIVE
    expect_no_volume_effects(effects)
