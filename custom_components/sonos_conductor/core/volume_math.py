"""Pure volume arithmetic. Every function is total and side-effect free."""

from __future__ import annotations

from math import sqrt

#: Reports closer than this to a commanded value are considered equal
#: (Sonos quantizes volume to 1/100 steps; HA floats wobble below this).
VOLUME_EPSILON = 0.005


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def room_scale(audible_zones_in_room: int, tv_active_in_room: bool) -> float:
    """Loudness compensation for acoustically linked zones.

    N zones sharing a room each play at 1/sqrt(N) so total perceived
    loudness stays constant. While a TV plays in the room the scale is
    forced to 1.0 to preserve a 1:1 Apple TV remote volume mapping.
    """
    if tv_active_in_room or audible_zones_in_room <= 1:
        return 1.0
    return 1.0 / sqrt(audible_zones_in_room)


def speaker_target(master: float, trim: float, scale: float) -> float:
    """Forward mapping: master volume -> a speaker's device volume."""
    return clamp(master * trim * scale)


def implied_master(volume: float, trim: float, scale: float) -> float:
    """Reverse mapping: a speaker's device volume -> implied master volume."""
    effective = trim * scale
    if effective <= 0.0:
        return 0.0
    return clamp(volume / effective)


def volumes_equal(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) < VOLUME_EPSILON
