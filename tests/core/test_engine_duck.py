"""Duck inputs: caps, fades, stacking, and interaction with reverse sync."""

from __future__ import annotations

from custom_components.sonos_conductor.core.events import DuckChanged, ExternalVolume, SetEnabled
from custom_components.sonos_conductor.core.model import DuckInputConfig
from custom_components.sonos_conductor.core.volume_math import speaker_target

from .harness import (
    DOOR,
    KJOKKEN,
    SOFAKROK,
    Harness,
    expect_ramp,
    make_config,
    ramps,
    timer_starts,
)

WINDOW = "binary_sensor.window"


def _two_duck_config():
    return make_config(
        duck_inputs=(
            DuckInputConfig(DOOR, "Door", duck_volume=0.05, engage_fade=0.0, release_fade=2.0),
            DuckInputConfig(WINDOW, "Window", duck_volume=0.10, engage_fade=1.0, release_fade=3.0),
        )
    )


class TestDuckBasics:
    def test_duck_engages_with_engage_fade(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.fire(DuckChanged(DOOR, True), at=1.0)
        expect_ramp(effects, KJOKKEN, 0.05, duration=0.0)
        assert len(ramps(effects)) == 1  # silent speakers stay at 0

    def test_duck_release_restores_exact_targets(self) -> None:
        h = Harness()
        before = expect_ramp(h.occupy("kjokken", at=0.0), KJOKKEN, 0.36, duration=3.0)
        h.fire(DuckChanged(DOOR, True), at=1.0)
        effects = h.fire(DuckChanged(DOOR, False), at=2.0)
        restored = expect_ramp(effects, KJOKKEN, 0.36, duration=2.0)  # release_fade
        assert restored.target == before.target  # bit-exact, no float dust
        assert restored.target == speaker_target(0.3, 1.2, 1.0)

    def test_duck_caps_fallback_zone_too(self) -> None:
        h = Harness()  # sofakrok forced audible at 0.3
        effects = h.fire(DuckChanged(DOOR, True), at=0.0)
        expect_ramp(effects, SOFAKROK, 0.05, duration=0.0)

    def test_repeated_duck_event_is_noop(self) -> None:
        h = Harness()
        h.fire(DuckChanged(DOOR, True), at=0.0)
        assert h.fire(DuckChanged(DOOR, True), at=1.0) == []

    def test_unknown_duck_input_ignored(self) -> None:
        h = Harness()
        assert h.fire(DuckChanged("binary_sensor.bogus", True), at=0.0) == []
        assert "binary_sensor.bogus" not in h.state.duck_active


class TestDuckStacking:
    def test_lowest_active_cap_wins(self) -> None:
        h = Harness(config=_two_duck_config())
        h.occupy("kjokken", at=0.0)
        effects = h.fire(DuckChanged(WINDOW, True), at=1.0)
        expect_ramp(effects, KJOKKEN, 0.10, duration=1.0)  # window engage_fade
        effects = h.fire(DuckChanged(DOOR, True), at=2.0)
        expect_ramp(effects, KJOKKEN, 0.05, duration=0.0)  # door is lower

    def test_release_steps_back_through_caps(self) -> None:
        h = Harness(config=_two_duck_config())
        h.occupy("kjokken", at=0.0)
        h.fire(DuckChanged(WINDOW, True), at=1.0)
        h.fire(DuckChanged(DOOR, True), at=2.0)
        effects = h.fire(DuckChanged(DOOR, False), at=3.0)
        expect_ramp(effects, KJOKKEN, 0.10, duration=2.0)  # door release_fade
        effects = h.fire(DuckChanged(WINDOW, False), at=4.0)
        expect_ramp(effects, KJOKKEN, 0.36, duration=3.0)  # window release_fade


class TestDuckInteractions:
    def test_active_duck_blocks_external_sync(self) -> None:
        h = Harness()
        h.fire(DuckChanged(DOOR, True), at=0.0)
        assert timer_starts(h.fire(ExternalVolume(SOFAKROK, 0.4), at=20.0)) == []

    def test_duck_change_opens_suppression_window(self) -> None:
        h = Harness()
        h.fire(DuckChanged(DOOR, True), at=0.0)
        h.fire(DuckChanged(DOOR, False), at=1.0)  # mode change at t=1
        assert h.fire(ExternalVolume(SOFAKROK, 0.4), at=5.0) == []
        assert timer_starts(h.fire(ExternalVolume(SOFAKROK, 0.4), at=11.5)) != []

    def test_duck_while_disabled_stores_and_applies_on_enable(self) -> None:
        h = Harness()
        h.fire(SetEnabled(False), at=0.0)
        assert h.fire(DuckChanged(DOOR, True), at=1.0) == []
        assert h.state.duck_active[DOOR] is True
        effects = h.fire(SetEnabled(True), at=2.0)
        expect_ramp(effects, SOFAKROK, 0.05, duration=2.0)  # capped on re-enable
