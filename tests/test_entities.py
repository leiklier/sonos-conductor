"""Entity platform tests: engine-state mirroring and command routing."""

from __future__ import annotations

from math import sqrt

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_component import DATA_INSTANCES
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.sonos_conductor.const import DOMAIN
from custom_components.sonos_conductor.core.events import (
    SetEnabled,
    SetKeepGrouped,
    SetMaster,
    SetMute,
    SetTrim,
    SetTvSolo,
)
from custom_components.sonos_conductor.core.model import ZonePhase
from tests.test_controller import MOVE, OPTIONS, SOFA, set_speaker, setup_conductor


def entity_id_for(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, f"no {platform} entity with unique_id {unique_id}"
    return entity_id


# ---------------------------------------------------------------------------
# media player
# ---------------------------------------------------------------------------


async def test_media_player_mirrors_leader_and_master(hass: HomeAssistant, monkeypatch) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)
    player = entity_id_for(hass, "media_player", f"{entry.entry_id}_master")

    state = hass.states.get(player)
    assert state.state == "playing"  # leader (sofakrok) is playing
    assert state.attributes["volume_level"] == 0.2  # engine master
    assert state.attributes["is_volume_muted"] is False
    assert state.attributes["device_class"] == "receiver"

    # Leader metadata is mirrored.
    set_speaker(hass, SOFA, media_title="Song", media_artist="Artist")
    await hass.async_block_till_done()
    state = hass.states.get(player)
    assert state.attributes["media_title"] == "Song"
    assert state.attributes["media_artist"] == "Artist"

    # Leader pauses -> proxy pauses.
    set_speaker(hass, SOFA, state="paused", media_title="Song", media_artist="Artist")
    await hass.async_block_till_done()
    assert hass.states.get(player).state == "paused"

    # Engine mute shows up after a publish.
    fake.state.muted = True
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()
    assert hass.states.get(player).attributes["is_volume_muted"] is True


async def test_media_player_volume_and_mute_route_to_engine(
    hass: HomeAssistant, monkeypatch
) -> None:
    entry, _controller, fake = await setup_conductor(hass, monkeypatch)
    player = entity_id_for(hass, "media_player", f"{entry.entry_id}_master")

    await hass.services.async_call(
        "media_player", "volume_set", {"entity_id": player, "volume_level": 0.5}, blocking=True
    )
    await hass.async_block_till_done()
    assert fake.events_of(SetMaster)[-1] == SetMaster(0.5, source="media_player")

    await hass.services.async_call(
        "media_player", "volume_up", {"entity_id": player}, blocking=True
    )
    await hass.async_block_till_done()
    step = fake.events_of(SetMaster)[-1]
    assert step.value == pytest.approx(0.23)  # master 0.2 + 0.03
    assert step.source == "media_player"

    await hass.services.async_call(
        "media_player",
        "volume_mute",
        {"entity_id": player, "is_volume_muted": True},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert fake.events_of(SetMute)[-1] == SetMute(True, source="media_player")


async def test_media_player_transport_forwards_to_leader(hass: HomeAssistant, monkeypatch) -> None:
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    player = entity_id_for(hass, "media_player", f"{entry.entry_id}_master")

    # Re-mock the transport services (platform forwarding registered the real
    # media_player services over any earlier mocks) and drive the entity
    # object directly — going through the service registry would now only
    # reach the mock, never the entity.
    play_calls = async_mock_service(hass, "media_player", "media_play")
    next_calls = async_mock_service(hass, "media_player", "media_next_track")
    entity = hass.data[DATA_INSTANCES]["media_player"].get_entity(player)
    assert entity is not None

    await entity.async_media_play()
    await entity.async_media_next_track()
    await hass.async_block_till_done()

    assert len(play_calls) == 1
    assert play_calls[0].data == {"entity_id": SOFA}
    assert len(next_calls) == 1
    assert next_calls[0].data == {"entity_id": SOFA}


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------


async def test_master_number(hass: HomeAssistant, monkeypatch) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)
    number = entity_id_for(hass, "number", f"{entry.entry_id}_master")

    assert hass.states.get(number).state == "0.2"

    await hass.services.async_call(
        "number", "set_value", {"entity_id": number, "value": 0.35}, blocking=True
    )
    await hass.async_block_till_done()
    assert fake.events_of(SetMaster)[-1] == SetMaster(0.35, source="number")

    # Engine-side change propagates via the dispatcher signal.
    fake.state.master = 0.35
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()
    assert hass.states.get(number).state == "0.35"


async def test_trim_numbers(hass: HomeAssistant, monkeypatch) -> None:
    entry, _controller, fake = await setup_conductor(hass, monkeypatch)
    trim = entity_id_for(hass, "number", f"{entry.entry_id}_trim_{MOVE}")

    assert hass.states.get(trim).state == "1.2"  # seeded from configured trim

    await hass.services.async_call(
        "number", "set_value", {"entity_id": trim, "value": 1.5}, blocking=True
    )
    await hass.async_block_till_done()
    assert fake.events_of(SetTrim)[-1] == SetTrim(MOVE, 1.5)
    assert hass.states.get(trim).state == "1.5"  # optimistic


# ---------------------------------------------------------------------------
# switches
# ---------------------------------------------------------------------------


async def test_switches_mirror_state_and_submit_events(hass: HomeAssistant, monkeypatch) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)

    enabled = entity_id_for(hass, "switch", f"{entry.entry_id}_enabled")
    mute = entity_id_for(hass, "switch", f"{entry.entry_id}_mute")
    tv_solo = entity_id_for(hass, "switch", f"{entry.entry_id}_tv_solo")
    keep_grouped = entity_id_for(hass, "switch", f"{entry.entry_id}_keep_grouped")

    assert hass.states.get(enabled).state == "on"
    assert hass.states.get(mute).state == "off"
    assert hass.states.get(tv_solo).state == "off"
    assert hass.states.get(keep_grouped).state == "on"

    await hass.services.async_call("switch", "turn_off", {"entity_id": enabled}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": mute}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": tv_solo}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": keep_grouped}, blocking=True)
    await hass.async_block_till_done()

    assert fake.events_of(SetEnabled) == [SetEnabled(False)]
    assert fake.events_of(SetMute) == [SetMute(True, source="switch")]
    assert fake.events_of(SetTvSolo) == [SetTvSolo(True)]
    assert fake.events_of(SetKeepGrouped) == [SetKeepGrouped(False)]

    # Engine state drives is_on via the dispatcher signal.
    fake.state.muted = True
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()
    assert hass.states.get(mute).state == "on"


# ---------------------------------------------------------------------------
# zone binary sensors
# ---------------------------------------------------------------------------


async def test_zone_binary_sensor(hass: HomeAssistant, monkeypatch) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)
    zone = entity_id_for(hass, "binary_sensor", f"{entry.entry_id}_zone_sofakrok")

    state = hass.states.get(zone)
    assert state.state == "off"
    assert state.attributes["phase"] == "idle"
    assert state.attributes["room"] == "stue"

    fake.state.zones["sofakrok"].phase = ZonePhase.ACTIVE
    fake.state.zones["sofakrok"].occupied = True
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()

    state = hass.states.get(zone)
    assert state.state == "on"
    assert state.attributes["phase"] == "active"
    assert state.attributes["occupied"] is True
    assert state.attributes["tv_playing"] is False
    assert state.attributes["room_scale"] == 1.0
    assert state.attributes["target_volume"] == pytest.approx(0.2)  # master * trim 1.0 * scale 1.0

    # A second audible zone in the same room halves the acoustic share.
    fake.state.zones["spisebord"].phase = ZonePhase.ACTIVE
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()

    state = hass.states.get(zone)
    assert state.attributes["room_scale"] == pytest.approx(1 / sqrt(2))
    assert state.attributes["target_volume"] == pytest.approx(0.2 / sqrt(2))

    # RELEASING still counts as audible.
    fake.state.zones["spisebord"].phase = ZonePhase.IDLE
    fake.state.zones["sofakrok"].phase = ZonePhase.RELEASING
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()
    assert hass.states.get(zone).state == "on"


async def test_zone_sensor_tv_solo_suppression(hass: HomeAssistant, monkeypatch) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)
    kjokken = entity_id_for(hass, "binary_sensor", f"{entry.entry_id}_zone_kjokken")

    # TV playing in stue with tv_solo: kjokken is suppressed -> target 0.
    fake.state.tv_solo = True
    fake.state.zones["sofakrok"].phase = ZonePhase.ACTIVE
    fake.state.zones["sofakrok"].tv_playing = True
    fake.state.zones["kjokken"].phase = ZonePhase.ACTIVE
    fake.state.zones["kjokken"].occupied = True
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()

    state = hass.states.get(kjokken)
    assert state.state == "on"  # phase-mirroring: FSM says active
    assert state.attributes["target_volume"] == 0.0  # but solo-suppressed


# ---------------------------------------------------------------------------
# diagnostics sensor
# ---------------------------------------------------------------------------


async def test_diagnostics_sensor(hass: HomeAssistant, monkeypatch) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)
    sensor = entity_id_for(hass, "sensor", f"{entry.entry_id}_state")

    state = hass.states.get(sensor)
    assert state.state == "enabled"
    assert state.attributes["master"] == 0.2
    assert state.attributes["muted"] is False
    assert state.attributes["keep_grouped"] is True
    assert state.attributes["speakers"][SOFA] == {
        "commanded": None,
        "volume": 0.2,
        "docked": True,
    }
    assert state.attributes["active_duck_inputs"] == []

    fake.state.enabled = False
    fake.state.duck_active["binary_sensor.inngangsdor"] = True
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()

    state = hass.states.get(sensor)
    assert state.state == "disabled"
    assert state.attributes["active_duck_inputs"] == ["binary_sensor.inngangsdor"]


# ---------------------------------------------------------------------------
# unconfigured entry
# ---------------------------------------------------------------------------


async def test_unconfigured_entry_creates_no_entities(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Sonos Conductor", data={}, options={})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.value == "loaded"
    assert hass.data[DOMAIN][entry.entry_id] is None

    registry = er.async_get(hass)
    assert er.async_entries_for_config_entry(registry, entry.entry_id) == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.value == "not_loaded"


async def test_media_player_source_list_mirrors_leader(hass: HomeAssistant, monkeypatch) -> None:
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    player = entity_id_for(hass, "media_player", f"{entry.entry_id}_master")

    set_speaker(hass, SOFA, source_list=["TV", "NRK P1", "Discover Weekly"])
    await hass.async_block_till_done()
    state = hass.states.get(player)
    assert state.attributes["source_list"] == ["TV", "NRK P1", "Discover Weekly"]

    # Radio favorites only appear inside media_channel; source falls back to
    # the first listed source contained in the channel string.
    set_speaker(
        hass,
        SOFA,
        source_list=["TV", "NRK P1", "Discover Weekly"],
        media_channel="NRK P1 Rogaland",
    )
    await hass.async_block_till_done()
    assert hass.states.get(player).attributes["source"] == "NRK P1"

    # A recognized input reported via the leader's own source attribute wins.
    set_speaker(hass, SOFA, source_list=["TV", "NRK P1"], source="TV")
    await hass.async_block_till_done()
    assert hass.states.get(player).attributes["source"] == "TV"


async def test_media_player_source_allowlist_filters(hass: HomeAssistant, monkeypatch) -> None:
    options = {**OPTIONS, "homekit_sources": ["NRK P1", "NRK P3"]}
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch, options=options)
    player = entity_id_for(hass, "media_player", f"{entry.entry_id}_master")

    set_speaker(hass, SOFA, source_list=["TV", "NRK P1", "NRK P3", "Discover Weekly"])
    await hass.async_block_till_done()
    assert hass.states.get(player).attributes["source_list"] == ["NRK P1", "NRK P3"]


async def test_media_player_select_source_forwards_to_leader(
    hass: HomeAssistant, monkeypatch
) -> None:
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    player = entity_id_for(hass, "media_player", f"{entry.entry_id}_master")

    select_calls = async_mock_service(hass, "media_player", "select_source")
    entity = hass.data[DATA_INSTANCES]["media_player"].get_entity(player)
    await entity.async_select_source("NRK P1")
    await hass.async_block_till_done()
    assert len(select_calls) == 1
    assert select_calls[0].data == {"entity_id": SOFA, "source": "NRK P1"}


async def test_media_player_homekit_remote_keys_skip_tracks(
    hass: HomeAssistant, monkeypatch
) -> None:
    """arrow_right/left (and skip keys) from the HomeKit remote skip tracks."""
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    player = entity_id_for(hass, "media_player", f"{entry.entry_id}_master")

    next_calls = async_mock_service(hass, "media_player", "media_next_track")
    prev_calls = async_mock_service(hass, "media_player", "media_previous_track")

    for key, next_expected, prev_expected in (
        ("arrow_right", 1, 0),
        ("next_track", 2, 0),
        ("fast_forward", 3, 0),
        ("arrow_left", 3, 1),
        ("previous_track", 3, 2),
        ("rewind", 3, 3),
        ("select", 3, 3),  # unrelated keys are ignored
        ("arrow_up", 3, 3),
    ):
        hass.bus.async_fire("homekit_tv_remote_key_pressed", {"key_name": key, "entity_id": player})
        await hass.async_block_till_done()
        assert len(next_calls) == next_expected, key
        assert len(prev_calls) == prev_expected, key
    assert all(c.data["entity_id"] == SOFA for c in [*next_calls, *prev_calls])

    # Events aimed at other entities are ignored.
    hass.bus.async_fire(
        "homekit_tv_remote_key_pressed",
        {"key_name": "arrow_right", "entity_id": "media_player.other_tv"},
    )
    await hass.async_block_till_done()
    assert len(next_calls) == 3
