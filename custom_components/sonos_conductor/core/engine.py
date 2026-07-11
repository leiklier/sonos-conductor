"""The conductor engine: a pure, synchronous event processor.

``handle(event, now)`` mutates :class:`~.model.EngineState` and returns the
effects the adapter must execute. It never performs I/O, never reads a
clock, and never sleeps — see docs/ENGINE_SPEC.md for the full behavioral
contract this class implements.
"""

from __future__ import annotations

from .effects import Effect
from .events import Event
from .model import ConductorConfig, EngineState, InitialSnapshot


class ConductorEngine:
    """Deterministic core of Sonos Conductor.

    Implementation lands in the ``feat/core-engine`` branch; this skeleton
    freezes the public API that the adapter layer builds against.
    """

    def __init__(self, config: ConductorConfig, snapshot: InitialSnapshot) -> None:
        self.config = config
        self.state: EngineState = EngineState()
        raise NotImplementedError("implemented in feat/core-engine")

    def start(self, now: float) -> list[Effect]:
        """Adopt the snapshot and return gentle startup effects.

        Never causes audible volume jumps: current volumes within
        ``startup_tolerance`` of their computed targets are adopted as-is.
        """
        raise NotImplementedError

    def handle(self, event: Event, now: float) -> list[Effect]:
        """Process one event and return the effects to execute."""
        raise NotImplementedError
