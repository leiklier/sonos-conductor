"""Sonos Conductor integration setup."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .controller import (
    CONF_LAST_MASTER,
    SonosConductorController,
    build_conductor_config,
    build_initial_snapshot,
)
from .core.engine import ConductorEngine

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

#: hass.data key holding, per entry_id, the options that warrant a reload.
DATA_RELOAD_BASELINE = f"{DOMAIN}_reload_baseline"


def _reload_relevant(options: Any) -> dict[str, Any]:
    """Options minus the keys the controller itself writes at runtime."""
    return {k: v for k, v in dict(options).items() if k != CONF_LAST_MASTER}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Sonos Conductor from a config entry."""
    config = build_conductor_config(entry.options)
    controller: SonosConductorController | None = None
    if config is not None:
        snapshot = build_initial_snapshot(hass, entry.options)
        controller = SonosConductorController(
            hass, entry, config, snapshot, engine_factory=ConductorEngine
        )
    else:
        _LOGGER.debug("No speakers configured; loading %s without a controller", entry.entry_id)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller
    hass.data.setdefault(DATA_RELOAD_BASELINE, {})[entry.entry_id] = _reload_relevant(entry.options)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if controller is not None:
        await controller.async_start()
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on options changes — except controller-written last_master.

    The controller persists the master volume into ``options["last_master"]``;
    reloading on that write would cause a reload loop.
    """
    baseline = hass.data.get(DATA_RELOAD_BASELINE, {}).get(entry.entry_id)
    if baseline is not None and _reload_relevant(entry.options) == baseline:
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    controller: SonosConductorController | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if controller is not None:
        await controller.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        hass.data.get(DATA_RELOAD_BASELINE, {}).pop(entry.entry_id, None)
    return unload_ok
