"""Diagnostics download: options, published engine state, adapter internals."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import ExtendedJSONEncoder
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sonos_conductor.const import DOMAIN
from custom_components.sonos_conductor.core.effects import RampVolume
from custom_components.sonos_conductor.core.events import SetMaster
from custom_components.sonos_conductor.diagnostics import async_get_config_entry_diagnostics
from tests.test_controller import MOVE, OPTIONS, SOFA, setup_conductor


async def test_diagnostics_snapshot(hass: HomeAssistant, monkeypatch) -> None:
    entry, controller, fake = await setup_conductor(hass, monkeypatch)
    fake.state.suppressed = frozenset({"kjokken"})
    fake.script([RampVolume(SOFA, 0.4, 1.0)])
    controller.submit(SetMaster(0.4))
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["options"]["speakers"] == OPTIONS["speakers"]
    state = diag["engine_state"]
    assert state["master"] == 0.2
    assert state["tv_solo_mode"] == "off"
    assert state["suppressed"] == ["kjokken"]
    assert state["trims"][MOVE] == 1.2
    assert state["zones"]["sofakrok"]["phase"] == "idle"
    assert state["speakers"][SOFA]["volume"] == 0.2
    ctl = diag["controller"]
    assert ctl["started"] is True
    assert ctl["ramps_in_flight"] == [SOFA]
    assert ctl["speaker_views"][SOFA]["volume"] == 0.2
    assert ctl["speaker_views"][SOFA]["available"] is True
    # Everything HA's diagnostics endpoint will serialize must round-trip.
    json.dumps(diag, cls=ExtendedJSONEncoder)


async def test_diagnostics_without_controller(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Sonos Conductor", data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag == {"options": {}, "controller": None}
