"""Effect accumulation for one ``handle()`` call (spec rule 10.5 ordering)."""

from __future__ import annotations

from .effects import (
    CancelTimer,
    Effect,
    JoinGroup,
    RampVolume,
    SetSpeakerMute,
    StartTimer,
)


class Plan:
    """Accumulates the effects of one ``handle()`` call in spec 10.5 order.

    ``CancelTimer`` first, then mute effects, then volume effects, then
    ``StartTimer``, then ``JoinGroup``. It shares the engine's pending-timer
    registry so cancellations are only emitted for timers the engine
    actually believes are running (cancel stays idempotent regardless).
    """

    __slots__ = ("_cancels", "_joins", "_mutes", "_pending", "_starts", "_volumes")

    def __init__(self, pending: set[str]) -> None:
        self._pending = pending
        self._cancels: list[Effect] = []
        self._mutes: list[Effect] = []
        self._volumes: list[Effect] = []
        self._starts: list[Effect] = []
        self._joins: list[Effect] = []

    def cancel_timer(self, timer_id: str) -> None:
        if timer_id in self._pending:
            self._pending.discard(timer_id)
            self._cancels.append(CancelTimer(timer_id))

    def start_timer(self, timer_id: str, delay: float) -> None:
        # StartTimer with a pending id restarts it (effects contract).
        self._pending.add(timer_id)
        self._starts.append(StartTimer(timer_id, delay))

    def mute(self, speaker_id: str, muted: bool) -> None:
        self._mutes.append(SetSpeakerMute(speaker_id, muted))

    def ramp(self, speaker_id: str, target: float, duration: float) -> None:
        self._volumes.append(RampVolume(speaker_id, target, duration))

    def join(self, leader_id: str, member_ids: tuple[str, ...]) -> None:
        self._joins.append(JoinGroup(leader_id, member_ids))

    def build(self) -> list[Effect]:
        return [*self._cancels, *self._mutes, *self._volumes, *self._starts, *self._joins]
