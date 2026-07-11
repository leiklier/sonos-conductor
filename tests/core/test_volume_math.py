"""Unit tests for the pure volume arithmetic."""

from __future__ import annotations

import math

import pytest

from custom_components.sonos_conductor.core.volume_math import (
    clamp,
    implied_master,
    room_scale,
    speaker_target,
    volumes_equal,
)


class TestRoomScale:
    def test_single_zone_is_unity(self) -> None:
        assert room_scale(1, tv_active_in_room=False) == 1.0

    def test_zero_zones_is_unity(self) -> None:
        assert room_scale(0, tv_active_in_room=False) == 1.0

    def test_two_zones_share_loudness(self) -> None:
        assert room_scale(2, tv_active_in_room=False) == pytest.approx(1 / math.sqrt(2))

    def test_tv_forces_unity(self) -> None:
        assert room_scale(2, tv_active_in_room=True) == 1.0


class TestMapping:
    def test_forward_applies_trim_and_scale(self) -> None:
        assert speaker_target(0.2, trim=1.2, scale=0.5) == pytest.approx(0.12)

    def test_forward_clamps_to_one(self) -> None:
        assert speaker_target(0.95, trim=1.2, scale=1.0) == 1.0

    def test_roundtrip(self) -> None:
        master = 0.34
        for trim in (1.0, 1.1, 1.2):
            for zones in (1, 2, 3):
                scale = room_scale(zones, tv_active_in_room=False)
                volume = speaker_target(master, trim, scale)
                assert implied_master(volume, trim, scale) == pytest.approx(master, abs=1e-9)

    def test_reverse_zero_effective_ratio_is_zero(self) -> None:
        assert implied_master(0.5, trim=0.0, scale=1.0) == 0.0

    def test_reverse_clamps(self) -> None:
        assert implied_master(0.9, trim=0.5, scale=1.0) == 1.0


class TestHelpers:
    def test_clamp(self) -> None:
        assert clamp(-0.1) == 0.0
        assert clamp(1.7) == 1.0
        assert clamp(0.5) == 0.5

    def test_volumes_equal_epsilon(self) -> None:
        assert volumes_equal(0.2, 0.206) is False
        assert volumes_equal(0.2, 0.2001) is True
        assert volumes_equal(None, None) is True
        assert volumes_equal(None, 0.2) is False
