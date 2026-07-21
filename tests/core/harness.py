"""Test harness for the pure engine: legacy-shaped config + effect helpers.

The default fixture mirrors docs/LEGACY_BEHAVIOR.md: three zones
(kjokken / spisebord / sofakrok), two rooms (kjokken, stue), trims
1.2 / 1.1 / 1.0, the Move is dockable, sofakrok is the fallback zone and
has the TV, and one duck input (the entrance door, cap 0.05).
"""

from __future__ import annotations

import math

import pytest

from custom_components.sonos_conductor.core import timers
from custom_components.sonos_conductor.core.effects import (
    CancelTimer,
    Effect,
    JoinGroup,
    RampVolume,
    SetSpeakerMute,
    StartTimer,
)
from custom_components.sonos_conductor.core.engine import ConductorEngine
from custom_components.sonos_conductor.core.events import Event, OccupancyChanged, TimerFired
from custom_components.sonos_conductor.core.model import (
    ConductorConfig,
    DuckInputConfig,
    FollowMode,
    IdleAttenuation,
    InitialSnapshot,
    PresenceActivity,
    SpeakerConfig,
    Tunables,
    TvSoloMode,
    ZoneConfig,
)
from custom_components.sonos_conductor.core.volume_math import VOLUME_FLOOR

#: Where "silent" speakers rest (never a true zero — see VOLUME_FLOOR).
FLOOR = VOLUME_FLOOR

KJOKKEN = "media_player.kjokken_sonos_move"
SPISEBORD = "media_player.spisebord_sonos"
SOFAKROK = "media_player.sofakrok_sonos"
ALL_SPEAKERS = (KJOKKEN, SPISEBORD, SOFAKROK)
DOOR = "binary_sensor.entrance_door"

#: Room scale for two audible zones sharing the stue room.
STUE_2 = 1.0 / math.sqrt(2)

TRIM = {KJOKKEN: 1.2, SPISEBORD: 1.1, SOFAKROK: 1.0}
ZONE_SPEAKER = {"kjokken": KJOKKEN, "spisebord": SPISEBORD, "sofakrok": SOFAKROK}


def make_config(
    *,
    duck_inputs: tuple[DuckInputConfig, ...] | None = None,
    primary_speaker_id: str | None = None,
    **tunable_overrides: float,
) -> ConductorConfig:
    """The legacy 3-zone topology (see docs/LEGACY_BEHAVIOR.md)."""
    if duck_inputs is None:
        duck_inputs = (
            DuckInputConfig(
                DOOR, "Entrance door", duck_volume=0.05, engage_fade=0.0, release_fade=2.0
            ),
        )
    return ConductorConfig(
        speakers=(
            SpeakerConfig(KJOKKEN, "Kjokken Move", trim=1.2, dockable=True),
            SpeakerConfig(SPISEBORD, "Spisebord Era", trim=1.1),
            SpeakerConfig(SOFAKROK, "Sofakrok Arc", trim=1.0),
        ),
        zones=(
            ZoneConfig("kjokken", "Kjokken", KJOKKEN, room_id="kjokken", hold_seconds=60.0),
            ZoneConfig("spisebord", "Spisebord", SPISEBORD, room_id="stue", hold_seconds=15.0),
            ZoneConfig(
                "sofakrok",
                "Sofakrok",
                SOFAKROK,
                room_id="stue",
                hold_seconds=15.0,
                fallback=True,
                has_tv=True,
            ),
        ),
        duck_inputs=duck_inputs,
        primary_speaker_id=primary_speaker_id,
        tunables=Tunables(**tunable_overrides),
    )


def make_config_with_extra_speaker(speaker_id: str) -> ConductorConfig:
    """The legacy config plus one zone-less (unmanaged) speaker."""
    base = make_config()
    return ConductorConfig(
        speakers=(*base.speakers, SpeakerConfig(speaker_id, "Extra")),
        zones=base.zones,
        duck_inputs=base.duck_inputs,
        tunables=base.tunables,
    )


def make_snapshot(
    *,
    master: float | None = 0.3,
    occupancy: dict[str, bool] | None = None,
    tv_playing: dict[str, bool] | None = None,
    docked: dict[str, bool] | None = None,
    volumes: dict[str, float | None] | None = None,
    muted: dict[str, bool] | None = None,
    playing: dict[str, bool] | None = None,
    group_members: dict[str, tuple[str, ...]] | None = None,
    duck_active: dict[str, bool] | None = None,
    activity: dict[str, PresenceActivity | None] | None = None,
    anyone_home: bool | None = None,
    mute: bool = False,
    enabled: bool = True,
    tv_solo_mode: TvSoloMode = TvSoloMode.OFF,
    follow_mode: FollowMode = FollowMode.PER_ZONE,
    idle_attenuation: IdleAttenuation = IdleAttenuation.MAX,
    keep_grouped: bool = True,
    night_mode: bool = False,
) -> InitialSnapshot:
    """A quiet house: nobody home, everything docked, grouped, converged.

    The fallback zone (sofakrok) will be forced ACTIVE; its default volume
    matches its target so ``start()`` emits nothing.
    """
    base_volumes: dict[str, float | None] = {
        KJOKKEN: FLOOR,
        SPISEBORD: FLOOR,
        SOFAKROK: master if master is not None else FLOOR,
    }
    if volumes is not None:
        base_volumes.update(volumes)  # partial overrides merge over defaults
    volumes = base_volumes
    if group_members is None:
        group_members = dict.fromkeys(ALL_SPEAKERS, ALL_SPEAKERS)
    return InitialSnapshot(
        occupancy=occupancy or {},
        tv_playing=tv_playing or {},
        docked=docked or {},
        volumes=volumes,
        muted=muted or {},
        playing=playing or {},
        group_members=group_members,
        duck_active=duck_active or {},
        activity=activity or {},
        anyone_home=anyone_home,
        master=master,
        mute=mute,
        enabled=enabled,
        tv_solo_mode=tv_solo_mode,
        follow_mode=follow_mode,
        idle_attenuation=idle_attenuation,
        keep_grouped=keep_grouped,
        night_mode=night_mode,
    )


class Harness:
    """Drives the engine with explicit time and tracks timer effects."""

    def __init__(
        self,
        config: ConductorConfig | None = None,
        snapshot: InitialSnapshot | None = None,
        *,
        auto_start: bool = True,
        now: float = 0.0,
    ) -> None:
        self.config = config if config is not None else make_config()
        self.snapshot = snapshot if snapshot is not None else make_snapshot()
        self.engine = ConductorEngine(self.config, self.snapshot)
        self.now = now
        #: timer_id -> absolute deadline, mirroring the adapter's timers.
        self.deadlines: dict[str, float] = {}
        self.start_effects: list[Effect] = []
        if auto_start:
            self.start_effects = self.engine.start(now)
            self._track(self.start_effects)

    @property
    def state(self):
        return self.engine.state

    def fire(self, event: Event, at: float | None = None) -> list[Effect]:
        if at is not None:
            assert at >= self.now, "monotonic time must not go backwards"
            self.now = at
        effects = self.engine.handle(event, self.now)
        self._track(effects)
        return effects

    def fire_timer(self, timer_id: str, at: float | None = None) -> list[Effect]:
        """Fire a timer as the adapter would (at its deadline by default)."""
        if at is None:
            at = max(self.now, self.deadlines.get(timer_id, self.now))
        self.deadlines.pop(timer_id, None)
        return self.fire(TimerFired(timer_id), at=at)

    # -- convenience event wrappers ------------------------------------

    def occupy(self, zone_id: str, at: float | None = None) -> list[Effect]:
        return self.fire(OccupancyChanged(zone_id, True), at=at)

    def vacate(self, zone_id: str, at: float | None = None) -> list[Effect]:
        return self.fire(OccupancyChanged(zone_id, False), at=at)

    def release(self, zone_id: str, at: float | None = None) -> list[Effect]:
        """Vacate a zone and expire its hold timer; returns the expiry effects."""
        self.vacate(zone_id, at=at)
        return self.fire_timer(timers.zone_release(zone_id))

    def _track(self, effects: list[Effect]) -> None:
        for effect in effects:
            if isinstance(effect, StartTimer):
                self.deadlines[effect.timer_id] = self.now + effect.delay
            elif isinstance(effect, CancelTimer):
                self.deadlines.pop(effect.timer_id, None)


# ---------------------------------------------------------------------
# Effect assertion helpers
# ---------------------------------------------------------------------


def ramps(effects: list[Effect]) -> list[RampVolume]:
    return [e for e in effects if isinstance(e, RampVolume)]


def timer_starts(effects: list[Effect]) -> list[StartTimer]:
    return [e for e in effects if isinstance(e, StartTimer)]


def timer_cancels(effects: list[Effect]) -> list[CancelTimer]:
    return [e for e in effects if isinstance(e, CancelTimer)]


def mute_effects(effects: list[Effect]) -> list[SetSpeakerMute]:
    return [e for e in effects if isinstance(e, SetSpeakerMute)]


def join_effects(effects: list[Effect]) -> list[JoinGroup]:
    return [e for e in effects if isinstance(e, JoinGroup)]


def ramp_for(effects: list[Effect], speaker_id: str) -> RampVolume | None:
    matches = [r for r in ramps(effects) if r.speaker_id == speaker_id]
    assert len(matches) <= 1, f"multiple ramps for {speaker_id}: {matches}"
    return matches[0] if matches else None


def expect_ramp(
    effects: list[Effect],
    speaker_id: str,
    target: float,
    duration: float | None = None,
) -> RampVolume:
    ramp = ramp_for(effects, speaker_id)
    assert ramp is not None, f"expected a ramp for {speaker_id}, got {effects}"
    assert ramp.target == pytest.approx(target, abs=1e-9), f"wrong target in {ramp}"
    if duration is not None:
        assert ramp.duration == pytest.approx(duration, abs=1e-9), f"wrong duration in {ramp}"
    return ramp


def expect_no_ramp(effects: list[Effect], speaker_id: str) -> None:
    assert ramp_for(effects, speaker_id) is None, f"unexpected ramp for {speaker_id}"


def expect_no_volume_effects(effects: list[Effect]) -> None:
    assert not ramps(effects), f"unexpected volume effects: {ramps(effects)}"
