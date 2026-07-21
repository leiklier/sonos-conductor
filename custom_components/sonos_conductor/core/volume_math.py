"""Pure volume arithmetic. Every function is total and side-effect free."""

from __future__ import annotations

from collections.abc import Iterable
from math import sqrt

#: Reports closer than this to a commanded value are considered equal
#: (Sonos quantizes volume to 1/100 steps; HA floats wobble below this).
VOLUME_EPSILON = 0.005

#: Silent targets land here instead of a true zero: Sonos turns its status
#: LED green while a speaker sits at volume 0, which is distracting when
#: zones dim in and out all day. Device volume 1/100 is inaudible in
#: practice. Equal to the reverse-sync hard-zero guard (rule 4.1), so a
#: floored speaker's own report is never mistaken for a user volume change.
VOLUME_FLOOR = 0.01


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def room_scale(zone_levels: Iterable[float], tv_active_in_room: bool) -> float:
    """Loudness compensation for acoustically linked zones.

    Each zone contributes its relative level: 1.0 while audible, its
    idle-bed fraction while idle (rule 3.4), 0.0 while silent. Perceived
    total loudness follows the power sum of the levels, so the scale is
    1/sqrt(sum of squared levels) — for N fully audible zones the classic
    1/sqrt(N). The scale never boosts: a room summing to less than one full
    zone keeps scale 1.0. While a TV plays in the room the scale is forced
    to 1.0 to preserve a 1:1 Apple TV remote volume mapping.
    """
    if tv_active_in_room:
        return 1.0
    power = sum(level * level for level in zone_levels)
    if power <= 1.0:
        return 1.0
    return 1.0 / sqrt(power)


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
