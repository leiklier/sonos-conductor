"""Rich presence: activity-scaled holds (1.7 / 1.2) and home gating (1.8)."""

from __future__ import annotations

import pytest

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.events import (
    ActivityChanged,
    DockChanged,
    HomePresenceChanged,
    OccupancyChanged,
    SetEnabled,
)
from custom_components.sonos_conductor.core.model import PresenceActivity, ZonePhase

from .harness import (
    FLOOR,
    KJOKKEN,
    SOFAKROK,
    Harness,
    expect_no_volume_effects,
    expect_ramp,
    make_config,
    make_snapshot,
    timer_starts,
)

PASSING = PresenceActivity.PASSING
ACTIVE = PresenceActivity.ACTIVE
SETTLED = PresenceActivity.SETTLED
EMPTY = PresenceActivity.EMPTY


def hold_delay(effects, zone_id: str) -> float:
    starts = [t for t in timer_starts(effects) if t.timer_id == timers.zone_release(zone_id)]
    assert len(starts) == 1, f"expected one release timer, got {effects}"
    return starts[0].delay


# ---------------------------------------------------------------------
# Rule 1.7 + 1.2: activity-scaled hold times
# ---------------------------------------------------------------------


def test_activity_changed_is_state_only() -> None:
    h = Harness()
    h.occupy("kjokken", at=1.0)
    effects = h.fire(ActivityChanged("kjokken", SETTLED), at=2.0)
    assert effects == []
    assert h.state.zones["kjokken"].activity is SETTLED
    assert h.state.zones["kjokken"].episode_peak is SETTLED


def test_passing_episode_gets_short_hold() -> None:
    h = Harness()
    h.occupy("kjokken", at=1.0)
    h.fire(ActivityChanged("kjokken", PASSING), at=2.0)
    h.fire(ActivityChanged("kjokken", EMPTY), at=5.0)  # walked out
    effects = h.vacate("kjokken", at=5.1)
    # kjokken hold_seconds = 60, hold_passing_scale default 0.3.
    assert hold_delay(effects, "kjokken") == pytest.approx(60.0 * 0.3)


def test_settled_episode_gets_long_hold() -> None:
    h = Harness()
    h.occupy("kjokken", at=1.0)
    h.fire(ActivityChanged("kjokken", SETTLED), at=300.0)
    h.fire(ActivityChanged("kjokken", EMPTY), at=600.0)
    effects = h.vacate("kjokken", at=600.1)
    assert hold_delay(effects, "kjokken") == pytest.approx(60.0 * 4.0)


def test_no_activity_input_keeps_plain_hold() -> None:
    h = Harness()
    h.occupy("kjokken", at=1.0)
    effects = h.vacate("kjokken", at=5.0)
    assert hold_delay(effects, "kjokken") == pytest.approx(60.0)


def test_active_episode_keeps_plain_hold() -> None:
    h = Harness()
    h.occupy("kjokken", at=1.0)
    h.fire(ActivityChanged("kjokken", ACTIVE), at=2.0)
    effects = h.vacate("kjokken", at=5.0)
    assert hold_delay(effects, "kjokken") == pytest.approx(60.0)


def test_hold_scales_are_tunable() -> None:
    h = Harness(config=make_config(hold_passing_scale=0.1, hold_settled_scale=10.0))
    h.occupy("kjokken", at=1.0)
    h.fire(ActivityChanged("kjokken", PASSING), at=2.0)
    effects = h.vacate("kjokken", at=3.0)
    assert hold_delay(effects, "kjokken") == pytest.approx(6.0)


def test_peak_survives_releasing_flicker() -> None:
    """RELEASING→ACTIVE is the same episode: the settled peak carries over."""
    h = Harness()
    h.occupy("kjokken", at=1.0)
    h.fire(ActivityChanged("kjokken", SETTLED), at=100.0)
    h.fire(ActivityChanged("kjokken", EMPTY), at=200.0)
    h.vacate("kjokken", at=200.1)  # RELEASING
    h.occupy("kjokken", at=205.0)  # flicker back, no new activity yet
    h.fire(ActivityChanged("kjokken", EMPTY), at=300.0)
    effects = h.vacate("kjokken", at=300.1)
    assert hold_delay(effects, "kjokken") == pytest.approx(60.0 * 4.0)


def test_new_episode_resets_peak() -> None:
    """After a full release the next visit starts fresh."""
    h = Harness()
    h.occupy("kjokken", at=1.0)
    h.fire(ActivityChanged("kjokken", SETTLED), at=100.0)
    h.fire(ActivityChanged("kjokken", EMPTY), at=200.0)
    h.release("kjokken", at=200.1)  # RELEASING → IDLE
    h.occupy("kjokken", at=500.0)
    h.fire(ActivityChanged("kjokken", PASSING), at=500.5)
    h.fire(ActivityChanged("kjokken", EMPTY), at=503.0)
    effects = h.vacate("kjokken", at=503.1)
    assert hold_delay(effects, "kjokken") == pytest.approx(60.0 * 0.3)


def test_activity_before_occupancy_seeds_the_episode() -> None:
    """The estimator may report activity before the occupancy flip lands."""
    h = Harness()
    h.fire(ActivityChanged("kjokken", PASSING), at=1.0)
    h.occupy("kjokken", at=1.1)
    assert h.state.zones["kjokken"].episode_peak is PASSING
    h.fire(ActivityChanged("kjokken", EMPTY), at=3.0)
    effects = h.vacate("kjokken", at=3.1)
    assert hold_delay(effects, "kjokken") == pytest.approx(60.0 * 0.3)


def test_redock_starts_new_episode() -> None:
    """Rule 2.2: a stale pre-undock SETTLED peak must not survive a redock."""
    h = Harness()
    h.occupy("kjokken", at=1.0)
    h.fire(ActivityChanged("kjokken", SETTLED), at=100.0)
    h.fire(DockChanged(KJOKKEN, False), at=200.0)  # STANDALONE
    # Activity keeps updating while standalone (world model stays fresh)…
    h.fire(ActivityChanged("kjokken", PASSING), at=300.0)
    # …and the redock starts a new episode at the current activity.
    h.fire(DockChanged(KJOKKEN, True), at=301.0)
    assert h.state.zones["kjokken"].episode_peak is PASSING
    h.fire(ActivityChanged("kjokken", EMPTY), at=302.0)
    effects = h.vacate("kjokken", at=302.1)
    assert hold_delay(effects, "kjokken") == pytest.approx(60.0 * 0.3)


def test_redock_seeds_current_settled_activity() -> None:
    """Redocking into a settled room adopts that activity as the episode."""
    h = Harness()
    h.occupy("kjokken", at=1.0)
    h.fire(DockChanged(KJOKKEN, False), at=2.0)
    h.fire(ActivityChanged("kjokken", SETTLED), at=100.0)
    h.fire(DockChanged(KJOKKEN, True), at=101.0)
    assert h.state.zones["kjokken"].episode_peak is SETTLED


def test_activity_for_unknown_zone_is_ignored() -> None:
    h = Harness()
    assert h.fire(ActivityChanged("garage", SETTLED), at=1.0) == []


def test_activity_updates_while_disabled() -> None:
    h = Harness()
    h.fire(SetEnabled(False), at=1.0)
    h.fire(OccupancyChanged("kjokken", True), at=2.0)
    h.fire(ActivityChanged("kjokken", SETTLED), at=3.0)
    assert h.state.zones["kjokken"].activity is SETTLED
    # 8.2: re-enabling recomputes phases; the episode starts at the
    # current activity, so the hold is settled-scaled.
    h.fire(SetEnabled(True), at=4.0)
    h.fire(ActivityChanged("kjokken", EMPTY), at=5.0)
    effects = h.vacate("kjokken", at=5.1)
    assert hold_delay(effects, "kjokken") == pytest.approx(60.0 * 4.0)


def test_seeded_audible_zone_starts_episode_at_snapshot_activity() -> None:
    h = Harness(
        snapshot=make_snapshot(
            occupancy={"kjokken": True},
            volumes={KJOKKEN: 0.36, SOFAKROK: 0.0},
            activity={"kjokken": SETTLED},
        )
    )
    assert h.state.zones["kjokken"].episode_peak is SETTLED
    h.fire(ActivityChanged("kjokken", EMPTY), at=1.0)
    effects = h.vacate("kjokken", at=1.1)
    assert hold_delay(effects, "kjokken") == pytest.approx(60.0 * 4.0)


# ---------------------------------------------------------------------
# Rule 1.8: home presence gates fallback forcing
# ---------------------------------------------------------------------


def test_home_empty_retires_forced_fallback() -> None:
    h = Harness()  # quiet house: sofakrok forced ACTIVE at master
    assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE
    effects = h.fire(HomePresenceChanged(False), at=10.0)
    assert h.state.zones["sofakrok"].phase is ZonePhase.IDLE
    expect_ramp(effects, SOFAKROK, FLOOR, duration=5.0)  # fade_out


def test_home_return_resumes_fallback() -> None:
    h = Harness()
    h.fire(HomePresenceChanged(False), at=10.0)
    effects = h.fire(HomePresenceChanged(True), at=20.0)
    assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE
    expect_ramp(effects, SOFAKROK, 0.3, duration=3.0)  # fade_in to master


def test_home_empty_leaves_occupied_zones_alone() -> None:
    h = Harness()
    h.occupy("kjokken", at=1.0)  # sofakrok retires: kjokken is audible
    effects = h.fire(HomePresenceChanged(False), at=10.0)
    expect_no_volume_effects(effects)
    assert h.state.zones["kjokken"].phase is ZonePhase.ACTIVE
    # Occupancy then ends with nobody home: the fallback must NOT re-force.
    h.release("kjokken", at=20.0)
    assert h.state.zones["sofakrok"].phase is ZonePhase.IDLE
    # Someone comes home: fallback greets them.
    effects = h.fire(HomePresenceChanged(True), at=100.0)
    expect_ramp(effects, SOFAKROK, 0.3, duration=3.0)


def test_home_unknown_behaves_as_present() -> None:
    h = Harness()
    effects = h.fire(HomePresenceChanged(None), at=10.0)
    expect_no_volume_effects(effects)
    assert h.state.zones["sofakrok"].phase is ZonePhase.ACTIVE


def test_seed_with_empty_home_does_not_force_fallback() -> None:
    h = Harness(
        snapshot=make_snapshot(volumes={SOFAKROK: 0.0}, anyone_home=False),
    )
    assert h.state.zones["sofakrok"].phase is ZonePhase.IDLE
    expect_no_volume_effects(h.start_effects)


def test_home_presence_stored_while_disabled() -> None:
    h = Harness()
    h.fire(SetEnabled(False), at=1.0)
    effects = h.fire(HomePresenceChanged(False), at=2.0)
    assert effects == []
    assert h.state.anyone_home is False
    # 8.2: enabling with an empty home must not force the fallback.
    h.fire(SetEnabled(True), at=3.0)
    assert h.state.zones["sofakrok"].phase is ZonePhase.IDLE
