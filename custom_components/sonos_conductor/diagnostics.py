"""Diagnostics download: entry options, published engine state, adapter internals."""

from __future__ import annotations

from dataclasses import asdict
from enum import Enum
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .controller import SonosConductorController


def _jsonable(value: Any) -> Any:
    """Make dataclass output JSON-friendly (enums, sets, tuples)."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    controller: SonosConductorController | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    diagnostics: dict[str, Any] = {"options": _jsonable(dict(entry.options))}
    if controller is None:
        diagnostics["controller"] = None
        return diagnostics
    diagnostics["engine_state"] = _jsonable(asdict(controller.engine.state))
    diagnostics["controller"] = _jsonable(controller.diagnostics())
    return diagnostics
