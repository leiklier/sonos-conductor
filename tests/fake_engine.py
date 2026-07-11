"""A scriptable engine double for controller/entity tests.

The real ``ConductorEngine`` is implemented on a parallel branch (its
constructor raises ``NotImplementedError`` here), so these tests use a
duck-typed stand-in exposing the same surface: ``state``, ``start(now)``,
``handle(event, now)``. It records every event it receives; the effects it
returns are scripted per-call (``script(...)`` queue) or computed by an
optional ``responder`` callable.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable

from custom_components.sonos_conductor.core.effects import Effect
from custom_components.sonos_conductor.core.events import Event
from custom_components.sonos_conductor.core.model import (
    ConductorConfig,
    EngineState,
    InitialSnapshot,
    SpeakerState,
    ZonePhase,
    ZoneState,
)


class FakeEngine:
    """Records events, returns scripted effects, seeds state from snapshot."""

    def __init__(self, config: ConductorConfig, snapshot: InitialSnapshot) -> None:
        self.config = config
        self.snapshot = snapshot
        self.state = EngineState()
        if snapshot.master is not None:
            self.state.master = snapshot.master
        for zone in config.zones:
            occupied = bool(snapshot.occupancy.get(zone.zone_id, False))
            tv_playing = bool(snapshot.tv_playing.get(zone.zone_id, False))
            self.state.zones[zone.zone_id] = ZoneState(
                phase=ZonePhase.ACTIVE if (occupied or tv_playing) else ZonePhase.IDLE,
                occupied=occupied,
                tv_playing=tv_playing,
            )
        for speaker in config.speakers:
            self.state.speakers[speaker.speaker_id] = SpeakerState(
                volume=snapshot.volumes.get(speaker.speaker_id),
                muted=bool(snapshot.muted.get(speaker.speaker_id, False)),
                playing=bool(snapshot.playing.get(speaker.speaker_id, False)),
                docked=bool(snapshot.docked.get(speaker.speaker_id, True)),
                group_members=tuple(snapshot.group_members.get(speaker.speaker_id, ())),
            )
        for duck in config.duck_inputs:
            self.state.duck_active[duck.input_id] = bool(
                snapshot.duck_active.get(duck.input_id, False)
            )

        #: Every event received, in order.
        self.events: list[Event] = []
        #: ``now`` values passed to start().
        self.start_calls: list[float] = []
        #: Effects returned by start().
        self.start_effects: list[Effect] = []
        #: Optional per-event effect factory (takes precedence over script()).
        self.responder: Callable[[Event], Iterable[Effect] | None] | None = None
        self._scripted: deque[list[Effect]] = deque()
        self._processing = False

    def script(self, effects: Iterable[Effect]) -> None:
        """Queue the effect list to return from the next handle() call."""
        self._scripted.append(list(effects))

    def events_of(self, event_type: type) -> list[Event]:
        return [event for event in self.events if isinstance(event, event_type)]

    # -- engine surface -----------------------------------------------------

    def start(self, now: float) -> list[Effect]:
        self.start_calls.append(now)
        return list(self.start_effects)

    def handle(self, event: Event, now: float) -> list[Effect]:
        assert not self._processing, "engine.handle re-entered — actor serialization broken"
        self._processing = True
        try:
            self.events.append(event)
            if self.responder is not None:
                return list(self.responder(event) or [])
            if self._scripted:
                return self._scripted.popleft()
            return []
        finally:
            self._processing = False
