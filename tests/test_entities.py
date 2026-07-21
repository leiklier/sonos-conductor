"""Entity platform tests: engine-state mirroring and command routing."""

from __future__ import annotations

from copy import deepcopy
from math import sqrt

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_component import DATA_INSTANCES
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
    mock_restore_cache,
)

from custom_components.sonos_conductor.const import DOMAIN
from custom_components.sonos_conductor.core.events import (
    SetEnabled,
    SetFollowMode,
    SetIdleAttenuation,
    SetKeepGrouped,
    SetMaster,
    SetMute,
    SetNightMode,
    SetTrim,
    SetTvSoloMode,
)
from custom_components.sonos_conductor.core.model import (
    FollowMode,
    IdleAttenuation,
    TvSoloMode,
    ZonePhase,
)
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
    assert state.attributes["device_class"] == "tv"  # HomeKit Television category

    # Leader metadata is mirrored.
    set_speaker(hass, SOFA, media_title="Song", media_artist="Artist")
    await hass.async_block_till_done()
    state = hass.states.get(player)
    assert state.attributes["media_title"] == "Song"
    assert state.attributes["media_artist"] == "Artist"

    # Leader pauses -> proxy reports off (HomeKit power mirrors playback).
    set_speaker(hass, SOFA, state="paused", media_title="Song", media_artist="Artist")
    await hass.async_block_till_done()
    assert hass.states.get(player).state == "off"

    # Engine mute shows up after a publish (attributes only exist while on).
    set_speaker(hass, SOFA, media_title="Song", media_artist="Artist")
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


async def test_master_number_is_gone(hass: HomeAssistant, monkeypatch) -> None:
    """The standalone master-volume number was folded into the media player."""
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_master") is None


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
    keep_grouped = entity_id_for(hass, "switch", f"{entry.entry_id}_keep_grouped")

    assert hass.states.get(enabled).state == "on"
    assert hass.states.get(keep_grouped).state == "on"

    await hass.services.async_call("switch", "turn_off", {"entity_id": enabled}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": keep_grouped}, blocking=True)
    await hass.async_block_till_done()

    assert fake.events_of(SetEnabled) == [SetEnabled(False)]
    assert fake.events_of(SetKeepGrouped) == [SetKeepGrouped(False)]

    # Engine state drives is_on via the dispatcher signal.
    fake.state.enabled = False
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()
    assert hass.states.get(enabled).state == "off"


async def test_night_mode_switch_mirrors_state_and_submits(
    hass: HomeAssistant, monkeypatch
) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)
    night = entity_id_for(hass, "switch", f"{entry.entry_id}_night_mode")
    assert night == "switch.sonos_conductor_night_mode"

    assert hass.states.get(night).state == "off"  # engine default

    await hass.services.async_call("switch", "turn_on", {"entity_id": night}, blocking=True)
    await hass.async_block_till_done()
    assert fake.events_of(SetNightMode) == [SetNightMode(True)]

    # Engine state drives is_on via the dispatcher signal.
    fake.state.night_mode = True
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()
    assert hass.states.get(night).state == "on"

    await hass.services.async_call("switch", "turn_off", {"entity_id": night}, blocking=True)
    await hass.async_block_till_done()
    assert fake.events_of(SetNightMode)[-1] == SetNightMode(False)


async def test_night_mode_switch_restores_state(hass: HomeAssistant, monkeypatch) -> None:
    """A restored 'on' is pushed back into the engine as SetNightMode."""
    mock_restore_cache(hass, (State("switch.sonos_conductor_night_mode", "on"),))
    _entry, _controller, fake = await setup_conductor(hass, monkeypatch)
    assert fake.events_of(SetNightMode) == [SetNightMode(True)]


async def test_night_mode_switch_ignores_invalid_restore(hass: HomeAssistant, monkeypatch) -> None:
    """Unknown/invalid restored values leave the engine default (off)."""
    mock_restore_cache(hass, (State("switch.sonos_conductor_night_mode", "unavailable"),))
    entry, _controller, fake = await setup_conductor(hass, monkeypatch)
    assert fake.events_of(SetNightMode) == []
    night = entity_id_for(hass, "switch", f"{entry.entry_id}_night_mode")
    assert hass.states.get(night).state == "off"


async def test_night_mode_switch_restore_matching_state_is_silent(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Restoring the engine's current value submits nothing."""
    mock_restore_cache(hass, (State("switch.sonos_conductor_night_mode", "off"),))
    _entry, _controller, fake = await setup_conductor(hass, monkeypatch)
    assert fake.events_of(SetNightMode) == []


async def test_other_switches_do_not_restore(hass: HomeAssistant, monkeypatch) -> None:
    """Only night_mode restores; enabled/keep_grouped keep engine defaults."""
    mock_restore_cache(
        hass,
        (
            State("switch.sonos_conductor_enabled", "off"),
            State("switch.sonos_conductor_keep_grouped", "off"),
        ),
    )
    _entry, _controller, fake = await setup_conductor(hass, monkeypatch)
    assert fake.events_of(SetEnabled) == []
    assert fake.events_of(SetKeepGrouped) == []


async def test_tv_solo_switch_is_gone(hass: HomeAssistant, monkeypatch) -> None:
    """The tv_solo boolean switch was replaced by the mode select."""
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_tv_solo") is None


async def test_mute_switch_is_gone(hass: HomeAssistant, monkeypatch) -> None:
    """Mute moved onto the media player; no standalone switch duplicates it."""
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_mute") is None


# ---------------------------------------------------------------------------
# tv_solo select
# ---------------------------------------------------------------------------


async def test_tv_solo_select_options_state_and_dispatch(hass: HomeAssistant, monkeypatch) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)
    select = entity_id_for(hass, "select", f"{entry.entry_id}_tv_solo")
    assert select == "select.sonos_conductor_tv_solo"

    state = hass.states.get(select)
    assert state.state == "off"  # engine default
    assert state.attributes["options"] == ["off", "same_room", "tv_zone"]

    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": "tv_zone"}, blocking=True
    )
    await hass.async_block_till_done()
    assert fake.events_of(SetTvSoloMode) == [SetTvSoloMode(TvSoloMode.TV_ZONE)]

    # Engine state drives the rendered option via the dispatcher signal.
    fake.state.tv_solo_mode = TvSoloMode.SAME_ROOM
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()
    assert hass.states.get(select).state == "same_room"


async def test_tv_solo_select_restores_mode(hass: HomeAssistant, monkeypatch) -> None:
    """A restored option is pushed back into the engine as SetTvSoloMode."""
    mock_restore_cache(hass, (State("select.sonos_conductor_tv_solo", "tv_zone"),))
    _entry, _controller, fake = await setup_conductor(hass, monkeypatch)
    assert fake.events_of(SetTvSoloMode) == [SetTvSoloMode(TvSoloMode.TV_ZONE)]


async def test_tv_solo_select_ignores_invalid_restore(hass: HomeAssistant, monkeypatch) -> None:
    """Unknown/invalid restored values leave the engine at OFF."""
    mock_restore_cache(hass, (State("select.sonos_conductor_tv_solo", "unavailable"),))
    entry, _controller, fake = await setup_conductor(hass, monkeypatch)
    assert fake.events_of(SetTvSoloMode) == []
    select = entity_id_for(hass, "select", f"{entry.entry_id}_tv_solo")
    assert hass.states.get(select).state == "off"


# ---------------------------------------------------------------------------
# follow_mode select
# ---------------------------------------------------------------------------


async def test_follow_mode_select_options_state_and_dispatch(
    hass: HomeAssistant, monkeypatch
) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)
    select = entity_id_for(hass, "select", f"{entry.entry_id}_follow_mode")
    assert select == "select.sonos_conductor_follow_mode"

    state = hass.states.get(select)
    assert state.state == "per_zone"  # engine default
    assert state.attributes["options"] == ["per_zone", "per_room", "all_speakers"]

    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": "all_speakers"}, blocking=True
    )
    await hass.async_block_till_done()
    assert fake.events_of(SetFollowMode) == [SetFollowMode(FollowMode.ALL_SPEAKERS)]

    # Engine state drives the rendered option via the dispatcher signal.
    fake.state.follow_mode = FollowMode.PER_ROOM
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()
    assert hass.states.get(select).state == "per_room"


async def test_follow_mode_select_restores_mode(hass: HomeAssistant, monkeypatch) -> None:
    """A restored option is pushed back into the engine as SetFollowMode."""
    mock_restore_cache(hass, (State("select.sonos_conductor_follow_mode", "per_room"),))
    _entry, _controller, fake = await setup_conductor(hass, monkeypatch)
    assert fake.events_of(SetFollowMode) == [SetFollowMode(FollowMode.PER_ROOM)]


async def test_follow_mode_select_ignores_invalid_restore(hass: HomeAssistant, monkeypatch) -> None:
    """Unknown/invalid restored values leave the engine at PER_ZONE."""
    mock_restore_cache(hass, (State("select.sonos_conductor_follow_mode", "unavailable"),))
    entry, _controller, fake = await setup_conductor(hass, monkeypatch)
    assert fake.events_of(SetFollowMode) == []
    select = entity_id_for(hass, "select", f"{entry.entry_id}_follow_mode")
    assert hass.states.get(select).state == "per_zone"


async def test_follow_mode_hides_per_room_without_shared_rooms(
    hass: HomeAssistant, monkeypatch
) -> None:
    """With every zone in its own room, per_room would equal per_zone —
    the redundant option is not offered (and a restored per_room is dropped)."""
    options = deepcopy(OPTIONS)
    for zone in options["zones"]:
        zone["room"] = zone["zone_id"]  # each zone its own acoustic room
    mock_restore_cache(hass, (State("select.sonos_conductor_follow_mode", "per_room"),))
    entry, _controller, fake = await setup_conductor(hass, monkeypatch, options=options)

    select = entity_id_for(hass, "select", f"{entry.entry_id}_follow_mode")
    state = hass.states.get(select)
    assert state.attributes["options"] == ["per_zone", "all_speakers"]
    assert state.state == "per_zone"
    assert fake.events_of(SetFollowMode) == []  # per_room restore: invalid now


# ---------------------------------------------------------------------------
# idle_attenuation select
# ---------------------------------------------------------------------------


async def test_idle_attenuation_select_options_state_and_dispatch(
    hass: HomeAssistant, monkeypatch
) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)
    select = entity_id_for(hass, "select", f"{entry.entry_id}_idle_attenuation")
    assert select == "select.sonos_conductor_idle_attenuation"

    state = hass.states.get(select)
    assert state.state == "max"  # engine default: idle zones fully silent
    assert state.attributes["options"] == ["gentle", "balanced", "max"]

    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": "gentle"}, blocking=True
    )
    await hass.async_block_till_done()
    assert fake.events_of(SetIdleAttenuation) == [SetIdleAttenuation(IdleAttenuation.GENTLE)]

    # Engine state drives the rendered option via the dispatcher signal.
    fake.state.idle_attenuation = IdleAttenuation.BALANCED
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()
    assert hass.states.get(select).state == "balanced"


async def test_idle_attenuation_select_restores_mode(hass: HomeAssistant, monkeypatch) -> None:
    """A restored option is pushed back into the engine as SetIdleAttenuation."""
    mock_restore_cache(hass, (State("select.sonos_conductor_idle_attenuation", "gentle"),))
    _entry, _controller, fake = await setup_conductor(hass, monkeypatch)
    assert fake.events_of(SetIdleAttenuation) == [SetIdleAttenuation(IdleAttenuation.GENTLE)]


async def test_idle_attenuation_select_ignores_invalid_restore(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Unknown/invalid restored values leave the engine at MAX."""
    mock_restore_cache(hass, (State("select.sonos_conductor_idle_attenuation", "unavailable"),))
    entry, _controller, fake = await setup_conductor(hass, monkeypatch)
    assert fake.events_of(SetIdleAttenuation) == []
    select = entity_id_for(hass, "select", f"{entry.entry_id}_idle_attenuation")
    assert hass.states.get(select).state == "max"


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
    spisebord = entity_id_for(hass, "binary_sensor", f"{entry.entry_id}_zone_spisebord")

    # The sensor renders the engine's published suppression set (rule 6.2)
    # instead of re-deriving it. same_room: the engine suppresses kjokken,
    # spisebord (same room as the TV) keeps a target.
    fake.state.tv_solo_mode = TvSoloMode.SAME_ROOM
    fake.state.suppressed = frozenset({"kjokken"})
    fake.state.zones["sofakrok"].phase = ZonePhase.ACTIVE
    fake.state.zones["sofakrok"].tv_playing = True
    fake.state.zones["kjokken"].phase = ZonePhase.ACTIVE
    fake.state.zones["kjokken"].occupied = True
    fake.state.zones["spisebord"].phase = ZonePhase.ACTIVE
    fake.state.zones["spisebord"].occupied = True
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()

    state = hass.states.get(kjokken)
    assert state.state == "on"  # phase-mirroring: FSM says active
    assert state.attributes["target_volume"] == 0.0  # but solo-suppressed
    assert hass.states.get(spisebord).attributes["target_volume"] > 0.0

    # tv_zone: the engine also suppresses the same-room zone; only the TV
    # zone plays.
    fake.state.tv_solo_mode = TvSoloMode.TV_ZONE
    fake.state.suppressed = frozenset({"kjokken", "spisebord"})
    async_dispatcher_send(hass, controller.signal)
    await hass.async_block_till_done()

    assert hass.states.get(spisebord).attributes["target_volume"] == 0.0
    assert hass.states.get(kjokken).attributes["target_volume"] == 0.0


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
    assert state.attributes["tv_solo_mode"] == "off"
    assert state.attributes["follow_mode"] == "per_zone"
    assert state.attributes["keep_grouped"] is True
    assert state.attributes["night_mode"] is False
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
    assert state.attributes["source_list"] == ["Other", "TV", "NRK P1", "Discover Weekly"]

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
    assert hass.states.get(player).attributes["source_list"] == ["Other", "NRK P1", "NRK P3"]


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
        ("back", 3, 3),  # unmapped keys are ignored
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


async def test_media_player_homekit_select_toggles_play_pause(
    hass: HomeAssistant, monkeypatch
) -> None:
    """The remote's center tap (OK) toggles play/pause on the leader."""
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    player = entity_id_for(hass, "media_player", f"{entry.entry_id}_master")

    calls = async_mock_service(hass, "media_player", "media_play_pause")
    hass.bus.async_fire(
        "homekit_tv_remote_key_pressed", {"key_name": "select", "entity_id": player}
    )
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data == {"entity_id": SOFA}


async def test_media_player_homekit_vertical_arrows_step_favorites(
    hass: HomeAssistant, monkeypatch
) -> None:
    """arrow_up/arrow_down step through the leader's sources (favorites)."""
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    player = entity_id_for(hass, "media_player", f"{entry.entry_id}_master")
    favorites = ["TV", "NRK P1", "Discover Weekly"]

    select_calls = async_mock_service(hass, "media_player", "select_source")

    def press(key: str) -> None:
        hass.bus.async_fire("homekit_tv_remote_key_pressed", {"key_name": key, "entity_id": player})

    # Nothing recognized playing (source = Other): down enters at the first
    # favorite, up at the last — the synthetic Other is never selected.
    set_speaker(hass, SOFA, source_list=favorites)
    await hass.async_block_till_done()
    press("arrow_down")
    await hass.async_block_till_done()
    assert select_calls[-1].data == {"entity_id": SOFA, "source": "TV"}
    press("arrow_up")
    await hass.async_block_till_done()
    assert select_calls[-1].data == {"entity_id": SOFA, "source": "Discover Weekly"}

    # From a recognized source, down/up step forward/backward...
    set_speaker(hass, SOFA, source_list=favorites, source="NRK P1")
    await hass.async_block_till_done()
    press("arrow_down")
    await hass.async_block_till_done()
    assert select_calls[-1].data == {"entity_id": SOFA, "source": "Discover Weekly"}
    press("arrow_up")
    await hass.async_block_till_done()
    assert select_calls[-1].data == {"entity_id": SOFA, "source": "TV"}

    # ...and wrap around at either end.
    set_speaker(hass, SOFA, source_list=favorites, source="Discover Weekly")
    await hass.async_block_till_done()
    press("arrow_down")
    await hass.async_block_till_done()
    assert select_calls[-1].data == {"entity_id": SOFA, "source": "TV"}
    set_speaker(hass, SOFA, source_list=favorites, source="TV")
    await hass.async_block_till_done()
    press("arrow_up")
    await hass.async_block_till_done()
    assert select_calls[-1].data == {"entity_id": SOFA, "source": "Discover Weekly"}

    # No sources at all: stepping is a no-op.
    count = len(select_calls)
    set_speaker(hass, SOFA, source_list=[])
    await hass.async_block_till_done()
    press("arrow_down")
    await hass.async_block_till_done()
    assert len(select_calls) == count


async def test_remote_key_event_entity_records_presses(hass: HomeAssistant, monkeypatch) -> None:
    """Every forwarded remote key lands on the event entity for automations."""
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    player = entity_id_for(hass, "media_player", f"{entry.entry_id}_master")
    event_entity = entity_id_for(hass, "event", f"{entry.entry_id}_remote_key")

    assert hass.states.get(event_entity).state == "unknown"  # no press yet

    # The info button has no built-in mapping — its whole point is this hook.
    hass.bus.async_fire(
        "homekit_tv_remote_key_pressed", {"key_name": "information", "entity_id": player}
    )
    await hass.async_block_till_done()
    assert hass.states.get(event_entity).attributes["event_type"] == "information"

    # Keys with built-in behavior are recorded too.
    async_mock_service(hass, "media_player", "media_play_pause")
    hass.bus.async_fire(
        "homekit_tv_remote_key_pressed", {"key_name": "select", "entity_id": player}
    )
    await hass.async_block_till_done()
    assert hass.states.get(event_entity).attributes["event_type"] == "select"

    # Presses aimed at another accessory never register.
    hass.bus.async_fire(
        "homekit_tv_remote_key_pressed",
        {"key_name": "information", "entity_id": "media_player.other_tv"},
    )
    await hass.async_block_till_done()
    assert hass.states.get(event_entity).attributes["event_type"] == "select"


async def test_media_player_power_maps_to_playback(hass: HomeAssistant, monkeypatch) -> None:
    """HomeKit power: turn_on plays the leader, turn_off pauses it."""
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    player = entity_id_for(hass, "media_player", f"{entry.entry_id}_master")

    play_calls = async_mock_service(hass, "media_player", "media_play")
    pause_calls = async_mock_service(hass, "media_player", "media_pause")
    entity = hass.data[DATA_INSTANCES]["media_player"].get_entity(player)

    await entity.async_turn_on()
    await entity.async_turn_off()
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in play_calls] == [SOFA]
    assert [c.data["entity_id"] for c in pause_calls] == [SOFA]


async def test_media_player_other_source_absorbs_unmatched(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Unmatched playback shows as 'Other'; selecting 'Other' is a no-op."""
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    player = entity_id_for(hass, "media_player", f"{entry.entry_id}_master")

    # Spotify Connect: not in source_list -> synthetic Other.
    set_speaker(hass, SOFA, source_list=["TV", "NRK P1"], source="Spotify Connect")
    await hass.async_block_till_done()
    state = hass.states.get(player)
    assert state.attributes["source_list"] == ["Other", "TV", "NRK P1"]
    assert state.attributes["source"] == "Other"

    select_calls = async_mock_service(hass, "media_player", "select_source")
    entity = hass.data[DATA_INSTANCES]["media_player"].get_entity(player)
    await entity.async_select_source("Other")
    await hass.async_block_till_done()
    assert select_calls == []  # synthetic input: nothing forwarded


# ---------------------------------------------------------------------------
# speaker volume sensors
# ---------------------------------------------------------------------------


async def test_speaker_volume_sensors_mirror_device_volume(
    hass: HomeAssistant, monkeypatch
) -> None:
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    sofa = entity_id_for(hass, "sensor", f"{entry.entry_id}_volume_{SOFA}")
    move = entity_id_for(hass, "sensor", f"{entry.entry_id}_volume_{MOVE}")

    state = hass.states.get(sofa)
    assert state.state == "20"  # seeded at volume_level 0.2
    assert state.attributes["unit_of_measurement"] == "%"
    assert hass.states.get(move).state == "20"

    set_speaker(hass, SOFA, volume=0.35)  # a conductor write or user change lands
    await hass.async_block_till_done()
    assert hass.states.get(sofa).state == "35"
    assert hass.states.get(move).state == "20"  # other speakers untouched


async def test_speaker_volume_sensor_follows_availability(hass: HomeAssistant, monkeypatch) -> None:
    entry, _controller, _fake = await setup_conductor(hass, monkeypatch)
    sofa = entity_id_for(hass, "sensor", f"{entry.entry_id}_volume_{SOFA}")

    hass.states.async_set(SOFA, "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(sofa).state == "unavailable"

    set_speaker(hass, SOFA, volume=0.15)  # the speaker comes back
    await hass.async_block_till_done()
    assert hass.states.get(sofa).state == "15"
