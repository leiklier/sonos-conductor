"""Controller actor tests: state->event translation, echo ledger, effects.

The engine is a scripted :class:`tests.fake_engine.FakeEngine` — the real
``ConductorEngine`` lands on a parallel branch and must not be instantiated
here. Speakers/sensors are plain fake states; media_player services are
re-mocked *after* setup (platform forwarding registers the real component
services over any earlier mocks).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.sonos_conductor.const import DOMAIN
from custom_components.sonos_conductor.core.effects import (
    CancelTimer,
    JoinGroup,
    RampVolume,
    SetSpeakerMute,
    StartTimer,
)
from custom_components.sonos_conductor.core.events import (
    DockChanged,
    DuckChanged,
    ExternalMute,
    ExternalVolume,
    GroupMembersReported,
    OccupancyChanged,
    PlaybackChanged,
    SetMaster,
    TimerFired,
    TvPlayingChanged,
)
from tests.fake_engine import FakeEngine

SOFA = "media_player.sofakrok_sonos"
MOVE = "media_player.kjokken_sonos_move"
SPISEBORD = "media_player.spisebord_sonos"
DOCK = "binary_sensor.kjokken_sonos_move_lader"
DOOR = "binary_sensor.inngangsdor"
OCC_SOFA_1 = "binary_sensor.sofakrok_occupancy"
OCC_SOFA_2 = "binary_sensor.sofakrok_radar"
OCC_KJOKKEN = "binary_sensor.kjokken_occupancy"
OCC_SPISEBORD = "binary_sensor.spisebord_occupancy"
TV = "media_player.sofakrok_tv"

OPTIONS: dict[str, Any] = {
    "speakers": [
        {"entity_id": SOFA, "name": "Sofakrok Sonos", "trim": 1.0, "dock_sensor": None},
        {"entity_id": MOVE, "name": "Kjøkken Move", "trim": 1.2, "dock_sensor": DOCK},
        {"entity_id": SPISEBORD, "name": "Spisebord Sonos", "trim": 1.1, "dock_sensor": None},
    ],
    "zones": [
        {
            "zone_id": "sofakrok",
            "name": "Sofakrok",
            "speaker": SOFA,
            "room": "stue",
            "occupancy": [OCC_SOFA_1, OCC_SOFA_2],
            "tvs": [TV],
            "hold_seconds": 15.0,
            "fallback": True,
        },
        {
            "zone_id": "kjokken",
            "name": "Kjøkken",
            "speaker": MOVE,
            "room": "kjokken",
            "occupancy": [OCC_KJOKKEN],
            "tvs": [],
            "hold_seconds": 60.0,
            "fallback": False,
        },
        {
            "zone_id": "spisebord",
            "name": "Spisebord",
            "speaker": SPISEBORD,
            "room": "stue",
            "occupancy": [OCC_SPISEBORD],
            "tvs": [],
            "hold_seconds": 15.0,
            "fallback": False,
        },
    ],
    "duck_inputs": [
        {
            "entity_id": DOOR,
            "name": "Inngangsdør",
            "duck_volume": 0.05,
            "engage_fade": 0.0,
            "release_fade": 2.0,
        }
    ],
    "primary_speaker": SOFA,
    "tunables": {"external_debounce": 1.5},
    "last_master": 0.2,
}


def set_speaker(
    hass: HomeAssistant,
    entity_id: str,
    *,
    state: str = "playing",
    volume: float = 0.2,
    muted: bool = False,
    members: list[str] | None = None,
    **extra: Any,
) -> None:
    hass.states.async_set(
        entity_id,
        state,
        {
            "volume_level": volume,
            "is_volume_muted": muted,
            "group_members": members if members is not None else [entity_id],
            **extra,
        },
    )


def seed_world(hass: HomeAssistant) -> None:
    for speaker in (SOFA, MOVE, SPISEBORD):
        set_speaker(hass, speaker)
    for occupancy in (OCC_SOFA_1, OCC_SOFA_2, OCC_KJOKKEN, OCC_SPISEBORD):
        hass.states.async_set(occupancy, "off")
    hass.states.async_set(DOCK, "on")
    hass.states.async_set(DOOR, "off")
    hass.states.async_set(TV, "off")


async def setup_conductor(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, Any] | None = None,
):
    """Set up the integration with FakeEngine and a seeded fake world."""
    import custom_components.sonos_conductor as integration

    monkeypatch.setattr(integration, "ConductorEngine", FakeEngine)
    seed_world(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Sonos Conductor", data={}, options=options or OPTIONS
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    controller = hass.data[DOMAIN][entry.entry_id]
    assert controller is not None
    return entry, controller, controller.engine


async def advance(hass: HomeAssistant, freezer, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# state -> event translation
# ---------------------------------------------------------------------------


async def test_occupancy_two_sensors_or_aggregate(hass: HomeAssistant, monkeypatch) -> None:
    _, _controller, fake = await setup_conductor(hass, monkeypatch)

    hass.states.async_set(OCC_SOFA_1, "on")
    await hass.async_block_till_done()
    assert fake.events_of(OccupancyChanged) == [OccupancyChanged("sofakrok", True)]

    # Second sensor turning on does not flip the aggregate: no new event.
    hass.states.async_set(OCC_SOFA_2, "on")
    await hass.async_block_till_done()
    assert len(fake.events_of(OccupancyChanged)) == 1

    # First sensor off: the other still holds the zone occupied.
    hass.states.async_set(OCC_SOFA_1, "off")
    await hass.async_block_till_done()
    assert len(fake.events_of(OccupancyChanged)) == 1

    hass.states.async_set(OCC_SOFA_2, "off")
    await hass.async_block_till_done()
    assert fake.events_of(OccupancyChanged) == [
        OccupancyChanged("sofakrok", True),
        OccupancyChanged("sofakrok", False),
    ]


async def test_occupancy_unavailable_counts_as_clear(hass: HomeAssistant, monkeypatch) -> None:
    _, _controller, fake = await setup_conductor(hass, monkeypatch)

    hass.states.async_set(OCC_KJOKKEN, "on")
    await hass.async_block_till_done()
    hass.states.async_set(OCC_KJOKKEN, "unavailable")
    await hass.async_block_till_done()
    assert fake.events_of(OccupancyChanged) == [
        OccupancyChanged("kjokken", True),
        OccupancyChanged("kjokken", False),
    ]


async def test_tv_aggregate(hass: HomeAssistant, monkeypatch) -> None:
    _, _controller, fake = await setup_conductor(hass, monkeypatch)

    hass.states.async_set(TV, "playing")
    await hass.async_block_till_done()
    assert fake.events_of(TvPlayingChanged) == [TvPlayingChanged("sofakrok", True)]

    # "on" also counts as playing: aggregate unchanged, no event.
    hass.states.async_set(TV, "on")
    await hass.async_block_till_done()
    assert len(fake.events_of(TvPlayingChanged)) == 1

    hass.states.async_set(TV, "off")
    await hass.async_block_till_done()
    assert fake.events_of(TvPlayingChanged)[-1] == TvPlayingChanged("sofakrok", False)


async def test_dock_sensor_unavailable_means_docked(hass: HomeAssistant, monkeypatch) -> None:
    _, _controller, fake = await setup_conductor(hass, monkeypatch)

    hass.states.async_set(DOCK, "off")
    await hass.async_block_till_done()
    assert fake.events_of(DockChanged) == [DockChanged(MOVE, False)]

    # Battery-charging sensor going unavailable must keep the speaker managed.
    hass.states.async_set(DOCK, "unavailable")
    await hass.async_block_till_done()
    assert fake.events_of(DockChanged) == [DockChanged(MOVE, False), DockChanged(MOVE, True)]

    # Recovering to "on" is not a flip (already docked).
    hass.states.async_set(DOCK, "on")
    await hass.async_block_till_done()
    assert len(fake.events_of(DockChanged)) == 2


async def test_duck_input(hass: HomeAssistant, monkeypatch) -> None:
    _, _controller, fake = await setup_conductor(hass, monkeypatch)

    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    assert fake.events_of(DuckChanged) == [DuckChanged(DOOR, True)]

    # Unavailable duck input counts as inactive.
    hass.states.async_set(DOOR, "unavailable")
    await hass.async_block_till_done()
    assert fake.events_of(DuckChanged) == [DuckChanged(DOOR, True), DuckChanged(DOOR, False)]


async def test_speaker_attribute_translation(hass: HomeAssistant, monkeypatch) -> None:
    _, _controller, fake = await setup_conductor(hass, monkeypatch)

    set_speaker(hass, SOFA, volume=0.25)
    await hass.async_block_till_done()
    assert fake.events_of(ExternalVolume) == [ExternalVolume(SOFA, 0.25)]

    set_speaker(hass, SOFA, volume=0.25, muted=True)
    await hass.async_block_till_done()
    assert fake.events_of(ExternalMute) == [ExternalMute(SOFA, True)]

    set_speaker(hass, SOFA, volume=0.25, muted=True, members=[SOFA, MOVE])
    await hass.async_block_till_done()
    assert fake.events_of(GroupMembersReported) == [GroupMembersReported(SOFA, (SOFA, MOVE))]

    set_speaker(hass, SOFA, state="paused", volume=0.25, muted=True, members=[SOFA, MOVE])
    await hass.async_block_till_done()
    assert fake.events_of(PlaybackChanged) == [PlaybackChanged(SOFA, False)]


# ---------------------------------------------------------------------------
# echo suppression
# ---------------------------------------------------------------------------


async def test_volume_echo_suppression(hass: HomeAssistant, monkeypatch) -> None:
    _, controller, fake = await setup_conductor(hass, monkeypatch)
    calls = async_mock_service(hass, "media_player", "volume_set")

    fake.script([RampVolume(SOFA, 0.3, 0)])
    controller.submit(SetMaster(0.3))
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data == {"entity_id": SOFA, "volume_level": 0.3}

    # The resulting state change is our own echo: swallowed.
    set_speaker(hass, SOFA, volume=0.3)
    await hass.async_block_till_done()
    assert fake.events_of(ExternalVolume) == []

    # A different value is a genuine external change.
    set_speaker(hass, SOFA, volume=0.35)
    await hass.async_block_till_done()
    assert fake.events_of(ExternalVolume) == [ExternalVolume(SOFA, 0.35)]


async def test_mute_echo_suppression(hass: HomeAssistant, monkeypatch) -> None:
    _, controller, fake = await setup_conductor(hass, monkeypatch)
    calls = async_mock_service(hass, "media_player", "volume_mute")

    fake.script([SetSpeakerMute(SOFA, True)])
    controller.submit(SetMaster(0.2))
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data == {"entity_id": SOFA, "is_volume_muted": True}

    set_speaker(hass, SOFA, muted=True)
    await hass.async_block_till_done()
    assert fake.events_of(ExternalMute) == []

    set_speaker(hass, SOFA, muted=False)
    await hass.async_block_till_done()
    assert fake.events_of(ExternalMute) == [ExternalMute(SOFA, False)]


async def test_reavailable_speaker_bypasses_ledger(hass: HomeAssistant, monkeypatch) -> None:
    _, _controller, fake = await setup_conductor(hass, monkeypatch)

    # Engine knows volume 0.2 (snapshot). Outage + return at the same value:
    # nothing to report.
    hass.states.async_set(SOFA, "unavailable")
    await hass.async_block_till_done()
    set_speaker(hass, SOFA, volume=0.2)
    await hass.async_block_till_done()
    assert fake.events_of(ExternalVolume) == []

    # Return at a different volume: reported as external.
    hass.states.async_set(SOFA, "unavailable")
    await hass.async_block_till_done()
    set_speaker(hass, SOFA, volume=0.5)
    await hass.async_block_till_done()
    assert fake.events_of(ExternalVolume) == [ExternalVolume(SOFA, 0.5)]


# ---------------------------------------------------------------------------
# ramps
# ---------------------------------------------------------------------------


async def test_ramp_steps_end_exactly_at_target(hass: HomeAssistant, monkeypatch, freezer) -> None:
    _, controller, fake = await setup_conductor(hass, monkeypatch)
    calls = async_mock_service(hass, "media_player", "volume_set")

    fake.script([RampVolume(SOFA, 0.4, 1.0)])  # from 0.2: 4 steps of 0.05
    controller.submit(SetMaster(0.4))
    await hass.async_block_till_done()
    assert calls == []  # first step is scheduled, not immediate

    for _ in range(4):
        await advance(hass, freezer, 0.26)
    values = [call.data["volume_level"] for call in calls]
    assert values == pytest.approx([0.25, 0.3, 0.35, 0.4])
    assert values[-1] == 0.4  # exact target, no float drift

    # No further steps after completion.
    await advance(hass, freezer, 1.0)
    assert len(calls) == 4


async def test_new_ramp_cancels_in_flight_ramp(hass: HomeAssistant, monkeypatch, freezer) -> None:
    _, controller, fake = await setup_conductor(hass, monkeypatch)
    calls = async_mock_service(hass, "media_player", "volume_set")

    fake.script([RampVolume(SOFA, 0.4, 1.0)])
    controller.submit(SetMaster(0.4))
    await hass.async_block_till_done()
    await advance(hass, freezer, 0.26)
    assert [call.data["volume_level"] for call in calls] == [0.25]

    fake.script([RampVolume(SOFA, 0.1, 0)])
    controller.submit(SetMaster(0.1))
    await hass.async_block_till_done()
    assert [call.data["volume_level"] for call in calls] == [0.25, 0.1]

    # The cancelled ramp must never interleave further writes.
    await advance(hass, freezer, 2.0)
    assert [call.data["volume_level"] for call in calls] == [0.25, 0.1]


async def test_ramp_skipped_for_unavailable_speaker(
    hass: HomeAssistant, monkeypatch, freezer
) -> None:
    _, controller, fake = await setup_conductor(hass, monkeypatch)
    calls = async_mock_service(hass, "media_player", "volume_set")

    hass.states.async_set(SOFA, "unavailable")
    await hass.async_block_till_done()

    fake.script([RampVolume(SOFA, 0.4, 1.0)])
    controller.submit(SetMaster(0.4))
    await hass.async_block_till_done()
    await advance(hass, freezer, 2.0)
    assert calls == []

    # The actor is still alive.
    controller.submit(SetMaster(0.25))
    await hass.async_block_till_done()
    assert fake.events_of(SetMaster)[-1] == SetMaster(0.25)


# ---------------------------------------------------------------------------
# timers
# ---------------------------------------------------------------------------

RELEASE_TIMER = "zone_release:sofakrok"


async def test_timer_fires_after_delay(hass: HomeAssistant, monkeypatch, freezer) -> None:
    _, controller, fake = await setup_conductor(hass, monkeypatch)

    fake.script([StartTimer(RELEASE_TIMER, 15.0)])
    controller.submit(OccupancyChanged("sofakrok", False))
    await hass.async_block_till_done()

    await advance(hass, freezer, 10.0)
    assert fake.events_of(TimerFired) == []
    await advance(hass, freezer, 5.1)
    assert fake.events_of(TimerFired) == [TimerFired(RELEASE_TIMER)]


async def test_cancel_timer_prevents_firing(hass: HomeAssistant, monkeypatch, freezer) -> None:
    _, controller, fake = await setup_conductor(hass, monkeypatch)

    fake.script([StartTimer(RELEASE_TIMER, 15.0)])
    controller.submit(OccupancyChanged("sofakrok", False))
    await hass.async_block_till_done()

    fake.script([CancelTimer(RELEASE_TIMER)])
    controller.submit(OccupancyChanged("sofakrok", True))
    await hass.async_block_till_done()

    await advance(hass, freezer, 30.0)
    assert fake.events_of(TimerFired) == []


async def test_restart_timer_resets_delay(hass: HomeAssistant, monkeypatch, freezer) -> None:
    _, controller, fake = await setup_conductor(hass, monkeypatch)

    fake.script([StartTimer(RELEASE_TIMER, 15.0)])
    controller.submit(OccupancyChanged("sofakrok", False))
    await hass.async_block_till_done()
    await advance(hass, freezer, 10.0)

    fake.script([StartTimer(RELEASE_TIMER, 15.0)])  # restart resets the delay
    controller.submit(OccupancyChanged("sofakrok", False))
    await hass.async_block_till_done()

    await advance(hass, freezer, 10.0)  # 20 s after first start, 10 s after restart
    assert fake.events_of(TimerFired) == []
    await advance(hass, freezer, 5.1)
    assert fake.events_of(TimerFired) == [TimerFired(RELEASE_TIMER)]


# ---------------------------------------------------------------------------
# other effects
# ---------------------------------------------------------------------------


async def test_join_group_effect(hass: HomeAssistant, monkeypatch) -> None:
    _, controller, fake = await setup_conductor(hass, monkeypatch)
    calls = async_mock_service(hass, "media_player", "join")

    fake.script([JoinGroup(SOFA, (MOVE, SPISEBORD))])
    controller.submit(SetMaster(0.2))
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data == {"entity_id": SOFA, "group_members": [MOVE, SPISEBORD]}


# ---------------------------------------------------------------------------
# actor robustness
# ---------------------------------------------------------------------------


async def test_queue_is_serialized_and_ordered(hass: HomeAssistant, monkeypatch) -> None:
    _, controller, fake = await setup_conductor(hass, monkeypatch)

    # FakeEngine.handle asserts non-reentrancy internally.
    for i in range(10):
        controller.submit(SetMaster(i / 10))
    await hass.async_block_till_done()
    assert [event.value for event in fake.events_of(SetMaster)] == [i / 10 for i in range(10)]


async def test_engine_exception_does_not_kill_the_actor(hass: HomeAssistant, monkeypatch) -> None:
    _, controller, fake = await setup_conductor(hass, monkeypatch)

    def responder(event):
        if isinstance(event, SetMaster) and event.value == 0.9:
            raise RuntimeError("boom")
        return []

    fake.responder = responder
    controller.submit(SetMaster(0.9))
    await hass.async_block_till_done()
    controller.submit(SetMaster(0.1))
    await hass.async_block_till_done()
    assert fake.events_of(SetMaster) == [SetMaster(0.9), SetMaster(0.1)]


# ---------------------------------------------------------------------------
# options listener + master persistence
# ---------------------------------------------------------------------------


async def test_last_master_only_change_does_not_reload(hass: HomeAssistant, monkeypatch) -> None:
    entry, controller, _fake = await setup_conductor(hass, monkeypatch)

    hass.config_entries.async_update_entry(entry, options={**entry.options, "last_master": 0.42})
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][entry.entry_id] is controller  # same actor: no reload


async def test_real_options_change_reloads(hass: HomeAssistant, monkeypatch) -> None:
    entry, controller, _fake = await setup_conductor(hass, monkeypatch)

    new_zones = [dict(zone, hold_seconds=30.0) for zone in OPTIONS["zones"]]
    hass.config_entries.async_update_entry(entry, options={**entry.options, "zones": new_zones})
    await hass.async_block_till_done()
    new_controller = hass.data[DOMAIN][entry.entry_id]
    assert new_controller is not None
    assert new_controller is not controller  # reloaded
    assert entry.state.value == "loaded"


async def test_master_persist_is_debounced(hass: HomeAssistant, monkeypatch, freezer) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)

    def responder(event):
        fake.state.master = 0.33
        return []

    fake.responder = responder
    controller.submit(SetMaster(0.33))
    await hass.async_block_till_done()
    assert entry.options["last_master"] == 0.2  # not yet persisted

    await advance(hass, freezer, 10.1)
    assert entry.options["last_master"] == 0.33
    assert hass.data[DOMAIN][entry.entry_id] is controller  # persist did not reload


async def test_master_persist_flushes_on_unload(hass: HomeAssistant, monkeypatch) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)

    def responder(event):
        fake.state.master = 0.44
        return []

    fake.responder = responder
    controller.submit(SetMaster(0.44))
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.options["last_master"] == 0.44
