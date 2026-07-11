"""End-to-end scenario tests: real ConductorEngine through the real controller.

The engine and the adapter each have exhaustive isolated suites; these tests
prove the *seam* — a config entry shaped like the real installation, fake
Sonos speaker states, and real service-call observation through the whole
stack. Entity ids mirror the production home (see docs/LEGACY_BEHAVIOR.md).

Fades are configured to 0 so volume writes are single ``volume_set`` calls;
ramp mechanics have their own tests in test_controller.py. Timer-driven
behavior (hold expiry, external debounce, group repair) is driven with
``async_fire_time_changed``.

Note on the fallback zone: sofakrok is forced audible whenever no other zone
is (someone is home => the stue always has sound). It retires the moment
another zone becomes audible, so a 1/sqrt(2) stue split only happens when
sofakrok and spisebord are *both* genuinely occupied.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.sonos_conductor.const import DOMAIN

ARC = "media_player.sofakrok_sonos"
ERA = "media_player.spisebord_sonos"
MOVE = "media_player.kjokken_sonos_move"
ALL_SPEAKERS = (ARC, ERA, MOVE)

OCC_SOFA = "binary_sensor.sofakrok_occupancy"
OCC_SPIS = "binary_sensor.spisebord_occupancy"
OCC_KJOK = "binary_sensor.kjokken_occupancy"
TV = "media_player.sofakrok_tv"
DOCK = "binary_sensor.kjokken_sonos_move_lader"
DOOR = "binary_sensor.gang_inngangsdor"

SQRT2 = 2**0.5

OPTIONS = {
    "speakers": [
        {"entity_id": ARC, "name": "Sofakrok Sonos", "trim": 1.0, "dock_sensor": None},
        {"entity_id": ERA, "name": "Spisebord Sonos", "trim": 1.1, "dock_sensor": None},
        {"entity_id": MOVE, "name": "Kjøkken Sonos Move", "trim": 1.2, "dock_sensor": DOCK},
    ],
    "zones": [
        {
            "zone_id": "sofakrok",
            "name": "Sofakrok",
            "speaker": ARC,
            "room": "stue",
            "occupancy": [OCC_SOFA],
            "tvs": [TV],
            "hold_seconds": 15.0,
            "fallback": True,
        },
        {
            "zone_id": "spisebord",
            "name": "Spisebord",
            "speaker": ERA,
            "room": "stue",
            "occupancy": [OCC_SPIS],
            "tvs": [],
            "hold_seconds": 15.0,
            "fallback": False,
        },
        {
            "zone_id": "kjokken",
            "name": "Kjøkken",
            "speaker": MOVE,
            "room": "kjokken",
            "occupancy": [OCC_KJOK],
            "tvs": [],
            "hold_seconds": 60.0,
            "fallback": False,
        },
    ],
    "duck_inputs": [
        {
            "entity_id": DOOR,
            "name": "Inngangsdør",
            "duck_volume": 0.05,
            "engage_fade": 0.0,
            "release_fade": 0.0,
        }
    ],
    "primary_speaker": ARC,
    # All fades zero: every reconciliation is a single immediate volume_set.
    "tunables": {
        "fade_in": 0.0,
        "fade_out": 0.0,
        "rebalance_fade": 0.0,
        "master_fade": 0.0,
        "sync_threshold": 0.02,
        "external_debounce": 1.5,
        "transition_suppression": 10.0,
        "group_repair_delay": 15.0,
        "startup_tolerance": 0.03,
    },
    "last_master": 0.2,
}

GROUPED = {"group_members": list(ALL_SPEAKERS)}


def _set_speaker(
    hass: HomeAssistant, entity_id: str, volume: float, state: str = "playing", **extra
) -> None:
    hass.states.async_set(
        entity_id,
        state,
        {"volume_level": volume, "is_volume_muted": False, **GROUPED, **extra},
    )


async def _setup(hass: HomeAssistant, *, occupied=(), tv="off", docked=True, door="off"):
    """Stand up the whole home, then the integration, then echo startup writes.

    Returns ``(entry, calls, n)`` where ``n`` is the number of volume writes
    issued during startup convergence — assert against ``calls["volume"][n:]``.
    """
    for sensor, zone_occupied in (
        (OCC_SOFA, "sofakrok" in occupied),
        (OCC_SPIS, "spisebord" in occupied),
        (OCC_KJOK, "kjokken" in occupied),
    ):
        hass.states.async_set(sensor, "on" if zone_occupied else "off")
    hass.states.async_set(TV, tv)
    hass.states.async_set(DOCK, "on" if docked else "off")
    hass.states.async_set(DOOR, door)
    # Arc starts at the master (0.2); the others start silent. Startup
    # reconciliation converges whatever does not match the seeded phases.
    _set_speaker(hass, ARC, 0.2)
    _set_speaker(hass, ERA, 0.0)
    _set_speaker(hass, MOVE, 0.0)

    # Load the real media_player services first, then replace them with mocks
    # (mocking before setup would be undone when the component registers the
    # real services during platform forwarding).
    assert await async_setup_component(hass, "media_player", {})
    calls = {
        "volume": async_mock_service(hass, "media_player", "volume_set"),
        "mute": async_mock_service(hass, "media_player", "volume_mute"),
        "join": async_mock_service(hass, "media_player", "join"),
    }
    entry = MockConfigEntry(domain=DOMAIN, title="Sonos Conductor", data={}, options=OPTIONS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    _echo(hass, calls)
    await hass.async_block_till_done()
    return entry, calls, len(calls["volume"])


def _volume_writes(calls, start: int = 0) -> list[tuple[str, float]]:
    return [
        (c.data["entity_id"], round(c.data["volume_level"], 4)) for c in calls["volume"][start:]
    ]


def _echo(hass: HomeAssistant, calls, start: int = 0) -> None:
    """Reflect issued volume_set calls back as speaker state (like Sonos would)."""
    for call in calls["volume"][start:]:
        _set_speaker(hass, call.data["entity_id"], call.data["volume_level"])


async def _advance(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def _master(hass: HomeAssistant) -> float:
    return float(hass.states.get("number.sonos_conductor_master_volume").state)


async def test_startup_adopts_reality_quietly(hass: HomeAssistant) -> None:
    """Volumes already match the seeded phases -> zero writes at startup."""
    _entry, _calls, n = await _setup(hass)
    assert n == 0

    assert _master(hass) == pytest.approx(0.2)
    zone = hass.states.get("binary_sensor.sonos_conductor_zone_sofakrok")
    assert zone is not None and zone.state == "on"  # fallback keeps stue alive


async def test_startup_converges_divergent_volumes(hass: HomeAssistant) -> None:
    """Kitchen occupied at startup but silent -> startup write brings it up."""
    _entry, calls, _n = await _setup(hass, occupied=("kjokken",))
    writes = dict(_volume_writes(calls))
    assert writes[MOVE] == pytest.approx(0.24)  # 0.2 * 1.2
    assert writes[ARC] == pytest.approx(0.0)  # fallback retired: kitchen is live


async def test_second_stue_zone_splits_loudness(hass: HomeAssistant) -> None:
    """Both stue zones genuinely occupied -> each at master*trim/sqrt(2)."""
    _entry, calls, n = await _setup(hass, occupied=("sofakrok",))
    hass.states.async_set(OCC_SPIS, "on")
    await hass.async_block_till_done()

    writes = dict(_volume_writes(calls, n))
    assert writes[ERA] == pytest.approx(0.2 * 1.1 / SQRT2, abs=1e-4)
    assert writes[ARC] == pytest.approx(0.2 * 1.0 / SQRT2, abs=1e-4)
    assert MOVE not in writes  # different room: untouched
    assert hass.states.get("binary_sensor.sonos_conductor_zone_spisebord").state == "on"


async def test_fallback_retires_when_other_zone_activates(hass: HomeAssistant) -> None:
    """Unoccupied fallback hands over to a genuinely occupied zone 1:1."""
    _entry, calls, n = await _setup(hass)  # nobody home-ish: fallback active
    hass.states.async_set(OCC_SPIS, "on")
    await hass.async_block_till_done()

    writes = dict(_volume_writes(calls, n))
    assert writes[ARC] == pytest.approx(0.0)  # fallback retires
    assert writes[ERA] == pytest.approx(0.22)  # sole stue zone: full scale


async def test_hold_timer_releases_zone_after_vacancy(hass: HomeAssistant) -> None:
    _entry, calls, n = await _setup(hass, occupied=("kjokken",))
    hass.states.async_set(OCC_KJOK, "off")
    await hass.async_block_till_done()
    assert _volume_writes(calls, n) == []  # RELEASING: still audible, no write

    await _advance(hass, 61)  # hold_seconds=60
    writes = dict(_volume_writes(calls, n))
    assert writes[MOVE] == pytest.approx(0.0)
    assert writes[ARC] == pytest.approx(0.2)  # fallback takes back over
    assert hass.states.get("binary_sensor.sonos_conductor_zone_kjokken").state == "off"


async def test_occupancy_flicker_within_hold_is_silent(hass: HomeAssistant) -> None:
    _entry, calls, n = await _setup(hass, occupied=("kjokken",))
    hass.states.async_set(OCC_KJOK, "off")
    await hass.async_block_till_done()
    await _advance(hass, 5)
    hass.states.async_set(OCC_KJOK, "on")
    await hass.async_block_till_done()
    await _advance(hass, 120)  # stale timer must not fire either
    assert _volume_writes(calls, n) == []


async def test_master_number_fans_out_to_audible_zones(hass: HomeAssistant) -> None:
    _entry, calls, n = await _setup(hass, occupied=("kjokken",))
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.sonos_conductor_master_volume", "value": 0.4},
        blocking=True,
    )
    await hass.async_block_till_done()

    writes = dict(_volume_writes(calls, n))
    assert writes == {MOVE: pytest.approx(0.48)}  # 0.4*1.2; idle zones untouched


async def test_door_ducks_and_restores(hass: HomeAssistant) -> None:
    _entry, calls, n = await _setup(hass, occupied=("kjokken",))
    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    assert dict(_volume_writes(calls, n)) == {MOVE: pytest.approx(0.05)}
    _echo(hass, calls, n)
    n = len(calls["volume"])

    hass.states.async_set(DOOR, "off")
    await hass.async_block_till_done()
    assert dict(_volume_writes(calls, n)) == {MOVE: pytest.approx(0.24)}


async def test_external_volume_syncs_master_and_rebalances(hass: HomeAssistant) -> None:
    """User drags the Arc slider: master follows, the kitchen rebalances."""
    _entry, calls, n = await _setup(hass, occupied=("sofakrok", "kjokken"))
    _set_speaker(hass, ARC, 0.5)  # external: not from a conductor write
    await hass.async_block_till_done()
    assert _volume_writes(calls, n) == []  # debouncing, nothing yet

    await _advance(hass, 2)  # external_debounce = 1.5
    writes = dict(_volume_writes(calls, n))
    assert ARC not in writes  # the reporter stays where the user put it
    assert writes[MOVE] == pytest.approx(0.6)  # 0.5 * 1.2
    assert _master(hass) == pytest.approx(0.5)


async def test_own_writes_do_not_bounce_back(hass: HomeAssistant) -> None:
    """Echoed conductor writes must not re-enter as external volume events."""
    _entry, calls, n = await _setup(hass, occupied=("kjokken",))
    # _setup already echoed the startup writes (MOVE -> 0.24, ARC -> 0).
    await _advance(hass, 3)  # any wrongly-accepted debounce would fire now
    assert _master(hass) == pytest.approx(0.2)  # unchanged
    assert _volume_writes(calls, n) == []


async def test_undock_hands_speaker_over_redock_reclaims(hass: HomeAssistant) -> None:
    _entry, calls, n = await _setup(hass, occupied=("kjokken",))
    hass.states.async_set(DOCK, "off")  # undock the Move
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.sonos_conductor_zone_kjokken").state == "off"
    # The Move is handed over untouched; the fallback wakes the stue instead.
    assert dict(_volume_writes(calls, n)) == {ARC: pytest.approx(0.2)}
    _echo(hass, calls, n)
    n = len(calls["volume"])

    # User cranks the standalone Move on the balcony: no sync, no writes.
    _set_speaker(hass, MOVE, 0.9)
    await hass.async_block_till_done()
    await _advance(hass, 3)
    assert _volume_writes(calls, n) == []
    assert _master(hass) == pytest.approx(0.2)

    hass.states.async_set(DOCK, "on")  # redock into the occupied kitchen
    await hass.async_block_till_done()
    writes = dict(_volume_writes(calls, n))
    assert writes[MOVE] == pytest.approx(0.24)  # reclaimed at conductor target
    assert writes[ARC] == pytest.approx(0.0)  # fallback retires again


async def test_mute_switch_fans_out(hass: HomeAssistant) -> None:
    _entry, calls, _n = await _setup(hass)
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.sonos_conductor_mute"},
        blocking=True,
    )
    await hass.async_block_till_done()
    muted = {(c.data["entity_id"], c.data["is_volume_muted"]) for c in calls["mute"]}
    assert muted == {(s, True) for s in ALL_SPEAKERS}

    player = hass.states.get("media_player.sonos_conductor")
    assert player.attributes["is_volume_muted"] is True


async def test_tv_solo_silences_other_room(hass: HomeAssistant) -> None:
    _entry, calls, n = await _setup(hass, occupied=("kjokken",))
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.sonos_conductor_tv_solo"},
        blocking=True,
    )
    await hass.async_block_till_done()

    hass.states.async_set(TV, "playing")  # movie night
    await hass.async_block_till_done()
    writes = dict(_volume_writes(calls, n))
    assert writes[MOVE] == pytest.approx(0.0)  # kitchen suppressed despite occupancy
    assert writes[ARC] == pytest.approx(0.2)  # TV room at full scale
    _echo(hass, calls, n)
    n = len(calls["volume"])

    hass.states.async_set(TV, "off")  # movie over
    await hass.async_block_till_done()
    writes_after = dict(_volume_writes(calls, n))
    assert writes_after[MOVE] == pytest.approx(0.24)  # kitchen comes back


async def test_group_dissolve_repaired_once(hass: HomeAssistant) -> None:
    _entry, calls, _n = await _setup(hass)
    # The group spontaneously dissolves: every speaker reports itself alone.
    for speaker in ALL_SPEAKERS:
        _set_speaker(hass, speaker, 0.2 if speaker == ARC else 0.0, group_members=[speaker])
    await hass.async_block_till_done()
    assert len(calls["join"]) == 0  # repair is delayed, not knee-jerk

    await _advance(hass, 16)  # group_repair_delay = 15
    assert len(calls["join"]) == 1
    join = calls["join"][0]
    assert join.data["entity_id"] == ARC
    assert set(join.data["group_members"]) == {ERA, MOVE}

    # Sonos regroups; the reports come back. No second join afterwards.
    for speaker in ALL_SPEAKERS:
        _set_speaker(hass, speaker, 0.2 if speaker == ARC else 0.0)
    await hass.async_block_till_done()
    await _advance(hass, 60)
    assert len(calls["join"]) == 1


async def test_undocked_speaker_not_dragged_into_group(hass: HomeAssistant) -> None:
    _entry, calls, _n = await _setup(hass, docked=False)
    for speaker in ALL_SPEAKERS:
        _set_speaker(hass, speaker, 0.2 if speaker == ARC else 0.0, group_members=[speaker])
    await hass.async_block_till_done()
    await _advance(hass, 16)
    assert len(calls["join"]) == 1
    assert set(calls["join"][0].data["group_members"]) == {ERA}  # Move left alone


async def test_media_player_proxies_transport_to_leader(hass: HomeAssistant) -> None:
    _entry, _calls, _n = await _setup(hass)
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    await hass.services.async_call(
        "media_player",
        "media_play_pause",
        {"entity_id": "media_player.sonos_conductor"},
        blocking=True,
    )
    await hass.async_block_till_done()
    # The conductor mirrors the leader's "playing" state, so HA's default
    # play_pause implementation resolves to media_pause before forwarding.
    forwarded = [
        e
        for e in events
        if e.data["service"] == "media_pause" and e.data["service_data"].get("entity_id") == ARC
    ]
    assert len(forwarded) == 1


async def test_disable_switch_stops_all_action(hass: HomeAssistant) -> None:
    _entry, calls, n = await _setup(hass)
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.sonos_conductor_enabled"},
        blocking=True,
    )
    await hass.async_block_till_done()

    hass.states.async_set(OCC_SPIS, "on")  # would normally fade spisebord in
    hass.states.async_set(DOOR, "on")  # would normally duck
    await hass.async_block_till_done()
    await _advance(hass, 30)
    assert _volume_writes(calls, n) == []
    assert len(calls["mute"]) == 0
