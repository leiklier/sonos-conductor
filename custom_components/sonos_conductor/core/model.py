"""Configuration and state models for the conductor core.

All identifiers (``speaker_id``, ``zone_id``, ``input_id``) are opaque
strings to the core. The Home Assistant adapter uses entity ids, but the
core never inspects them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class ZonePhase(StrEnum):
    """Lifecycle phase of a zone. See docs/ARCHITECTURE.md for the FSM."""

    IDLE = "idle"
    ACTIVE = "active"
    RELEASING = "releasing"  # occupancy lost, hold timer running, still audible
    STANDALONE = "standalone"  # speaker undocked: excluded from the conductor


class TvSoloMode(StrEnum):
    """How aggressively a playing TV silences the rest of the house (rule 6.2)."""

    OFF = "off"  # never suppress anything
    SAME_ROOM = "same_room"  # suppress zones in rooms without a playing TV
    TV_ZONE = "tv_zone"  # suppress every zone except the TV zone(s) themselves


@dataclass(frozen=True, slots=True)
class SpeakerConfig:
    """A managed Sonos speaker."""

    speaker_id: str
    name: str
    #: Loudness trim compensating for hardware differences. The speaker's
    #: target volume is ``master * trim * room_scale`` (clamped to 1.0).
    trim: float = 1.0
    #: True when a dock/charging sensor was discovered for this speaker.
    #: Dockable speakers enter ``STANDALONE`` while undocked.
    dockable: bool = False


@dataclass(frozen=True, slots=True)
class ZoneConfig:
    """An audio zone: one speaker plus the inputs that make it audible."""

    zone_id: str
    name: str
    speaker_id: str
    #: Acoustic room this zone belongs to. Zones sharing a room are scaled
    #: by 1/sqrt(number of audible zones in the room).
    room_id: str
    hold_seconds: float = 15.0
    #: A fallback zone is forced audible while no other zone is audible.
    fallback: bool = False
    #: Zone has TV players attached (adapter aggregates them into
    #: TvPlayingChanged events). While the TV plays, the zone counts as
    #: occupied and its room's scale is forced to 1.0.
    has_tv: bool = False


@dataclass(frozen=True, slots=True)
class DuckInputConfig:
    """A binary input that temporarily caps all audible speakers."""

    input_id: str
    name: str
    #: Absolute volume cap while the input is active. Lowest active cap wins.
    duck_volume: float = 0.05
    engage_fade: float = 0.0
    release_fade: float = 2.0


@dataclass(frozen=True, slots=True)
class Tunables:
    """Behavioral tuning knobs, all durations in seconds."""

    fade_in: float = 3.0
    fade_out: float = 5.0
    #: Fade used when rebalancing already-audible speakers (scale/master/sync).
    rebalance_fade: float = 2.0
    #: Fade for master-volume fan-out (0 = immediate, tracks a slider tightly).
    master_fade: float = 0.0
    #: Minimum implied master change for an external volume report to count.
    sync_threshold: float = 0.02
    #: Debounce window for external volume reports (user dragging a slider).
    external_debounce: float = 1.5
    #: Ignore external volume reports within this window after any zone
    #: transition, duck change, or TV-mode change (the fleet is in motion).
    transition_suppression: float = 10.0
    #: How long a group topology must deviate before we repair it.
    group_repair_delay: float = 15.0
    #: Volume divergence below which startup adopts current volumes as-is.
    startup_tolerance: float = 0.03


@dataclass(frozen=True, slots=True)
class ConductorConfig:
    """Full static configuration handed to the engine."""

    speakers: tuple[SpeakerConfig, ...]
    zones: tuple[ZoneConfig, ...]
    duck_inputs: tuple[DuckInputConfig, ...] = ()
    #: Preferred group leader (e.g. the home-theater speaker). Defaults to
    #: the fallback zone's speaker, else the first speaker.
    primary_speaker_id: str | None = None
    tunables: Tunables = field(default_factory=Tunables)

    def speaker(self, speaker_id: str) -> SpeakerConfig:
        return next(s for s in self.speakers if s.speaker_id == speaker_id)

    def zone(self, zone_id: str) -> ZoneConfig:
        return next(z for z in self.zones if z.zone_id == zone_id)

    def zone_for_speaker(self, speaker_id: str) -> ZoneConfig | None:
        return next((z for z in self.zones if z.speaker_id == speaker_id), None)

    def zones_in_room(self, room_id: str) -> tuple[ZoneConfig, ...]:
        return tuple(z for z in self.zones if z.room_id == room_id)

    def leader_id(self) -> str:
        if self.primary_speaker_id:
            return self.primary_speaker_id
        for zone in self.zones:
            if zone.fallback:
                return zone.speaker_id
        return self.speakers[0].speaker_id


@dataclass(slots=True)
class ZoneState:
    """Mutable runtime state of a zone (owned by the engine)."""

    phase: ZonePhase = ZonePhase.IDLE
    occupied: bool = False
    tv_playing: bool = False
    #: Monotonic timestamp of the last phase change (drives suppression).
    last_transition: float = float("-inf")


@dataclass(slots=True)
class SpeakerState:
    """Mutable runtime state of a speaker (owned by the engine)."""

    #: Last volume reported by the device (None until first report).
    volume: float | None = None
    #: Last volume the engine commanded (echo/idempotency reference).
    commanded: float | None = None
    muted: bool = False
    playing: bool = False
    docked: bool = True
    #: Group members as last reported for this speaker (empty = unknown).
    group_members: tuple[str, ...] = ()
    #: Pending debounced external volume report, if any.
    pending_external: float | None = None


@dataclass(slots=True)
class EngineState:
    """Aggregate engine state. The adapter reads this to publish entities."""

    master: float = 0.15
    muted: bool = False
    enabled: bool = True
    tv_solo_mode: TvSoloMode = TvSoloMode.OFF
    keep_grouped: bool = True
    #: Zone ids currently solo-suppressed (rule 6.2). Engine-maintained,
    #: published so the adapter never re-derives suppression itself.
    suppressed: frozenset[str] = frozenset()
    zones: dict[str, ZoneState] = field(default_factory=dict)
    speakers: dict[str, SpeakerState] = field(default_factory=dict)
    duck_active: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InitialSnapshot:
    """Point-in-time world state used to seed the engine at startup.

    The adapter gathers this from current entity states so the engine can
    adopt reality instead of blasting volume changes on every restart.
    """

    occupancy: Mapping[str, bool]  # zone_id -> occupied
    tv_playing: Mapping[str, bool]  # zone_id -> tv playing
    docked: Mapping[str, bool]  # speaker_id -> docked (True if not dockable)
    volumes: Mapping[str, float | None]  # speaker_id -> current volume
    muted: Mapping[str, bool]  # speaker_id -> device mute
    playing: Mapping[str, bool]  # speaker_id -> is playing
    group_members: Mapping[str, tuple[str, ...]]  # speaker_id -> members
    duck_active: Mapping[str, bool]  # input_id -> active
    #: Restored master volume (e.g. from a RestoreEntity); None = infer from
    #: the median of ``volume / (trim * room_scale)`` across audible zones.
    master: float | None = None
    mute: bool = False
    enabled: bool = True
    tv_solo_mode: TvSoloMode = TvSoloMode.OFF
    keep_grouped: bool = True
