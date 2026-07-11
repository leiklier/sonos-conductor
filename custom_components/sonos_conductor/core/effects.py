"""Output effects emitted by the conductor engine.

The adapter executes these against Home Assistant. Rules for the executor:

- ``RampVolume`` for a speaker cancels any in-flight ramp for that speaker.
  A ``duration`` of 0 is a single immediate ``media_player.volume_set``.
  Every step written during a ramp must be recorded in the echo-suppression
  ledger before the service call is issued.
- ``StartTimer`` with an already-pending ``timer_id`` restarts that timer.
- After every ``ConductorEngine.handle`` call the adapter refreshes the
  conductor's own entities from ``engine.state`` — there is no dedicated
  "publish state" effect.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Effect:
    """Base class for all engine output effects."""


@dataclass(frozen=True, slots=True)
class RampVolume(Effect):
    """Ramp a speaker's volume to ``target`` over ``duration`` seconds."""

    speaker_id: str
    target: float
    duration: float


@dataclass(frozen=True, slots=True)
class SetSpeakerMute(Effect):
    speaker_id: str
    muted: bool


@dataclass(frozen=True, slots=True)
class StartTimer(Effect):
    timer_id: str
    delay: float


@dataclass(frozen=True, slots=True)
class CancelTimer(Effect):
    timer_id: str


@dataclass(frozen=True, slots=True)
class JoinGroup(Effect):
    """Join ``member_ids`` into ``leader_id``'s group (group repair)."""

    leader_id: str
    member_ids: tuple[str, ...]
