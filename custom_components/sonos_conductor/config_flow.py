"""Config flow for Sonos Conductor.

Placeholder single-step flow; the discovery-driven multi-step flow lands in
feat/ha-adapter.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class SonosConductorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create a single instance with empty config (placeholder)."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Sonos Conductor", data={})
        return self.async_show_form(step_id="user")
