"""Input events for the conductor engine.

The adapter is responsible for:

- aggregating multiple occupancy sensors / TV players per zone into a single
  boolean before emitting ``OccupancyChanged`` / ``TvPlayingChanged``;
- echo suppression: volume/mute reports caused by the conductor's own writes
  must be swallowed by the adapter's write ledger and never reach the engine.
  ``ExternalVolume`` / ``ExternalMute`` therefore always mean "someone else
  did this" (Sonos app, speaker touch controls, Apple TV remote, ...);
- monotonic time: every call to ``ConductorEngine.handle`` passes ``now``
  (monotonic seconds). Events carry no timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import TvSoloMode


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for all engine input events."""


# --- world changes -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class OccupancyChanged(Event):
    zone_id: str
    occupied: bool


@dataclass(frozen=True, slots=True)
class TvPlayingChanged(Event):
    zone_id: str
    playing: bool


@dataclass(frozen=True, slots=True)
class DockChanged(Event):
    speaker_id: str
    docked: bool


@dataclass(frozen=True, slots=True)
class DuckChanged(Event):
    input_id: str
    active: bool


@dataclass(frozen=True, slots=True)
class ExternalVolume(Event):
    """A speaker's volume changed and it was not one of our writes."""

    speaker_id: str
    volume: float


@dataclass(frozen=True, slots=True)
class ExternalMute(Event):
    """A speaker's mute state changed and it was not one of our writes."""

    speaker_id: str
    muted: bool


@dataclass(frozen=True, slots=True)
class PlaybackChanged(Event):
    speaker_id: str
    playing: bool


@dataclass(frozen=True, slots=True)
class GroupMembersReported(Event):
    """A speaker's ``group_members`` attribute changed (echo-filtered)."""

    speaker_id: str
    members: tuple[str, ...]


# --- user commands (from conductor entities / services) -----------------


@dataclass(frozen=True, slots=True)
class SetMaster(Event):
    value: float  # 0.0 .. 1.0
    source: str = "user"


@dataclass(frozen=True, slots=True)
class SetMute(Event):
    muted: bool
    source: str = "user"


@dataclass(frozen=True, slots=True)
class SetEnabled(Event):
    enabled: bool


@dataclass(frozen=True, slots=True)
class SetTvSoloMode(Event):
    mode: TvSoloMode


@dataclass(frozen=True, slots=True)
class SetKeepGrouped(Event):
    enabled: bool


@dataclass(frozen=True, slots=True)
class SetTrim(Event):
    """Runtime adjustment of a speaker's loudness trim."""

    speaker_id: str
    trim: float


# --- timers --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimerFired(Event):
    """A timer previously requested via a ``StartTimer`` effect expired.

    ``timer_id`` values are produced exclusively by :mod:`.timers`.
    """

    timer_id: str
