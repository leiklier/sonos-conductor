"""Constants for the Sonos Conductor integration."""

from __future__ import annotations

DOMAIN = "sonos_conductor"

# Config entry data/options keys. The config flow (feat/ha-adapter) is the
# single writer of these structures; the controller is the single reader.
CONF_SPEAKERS = "speakers"  # list[dict]: entity_id, name, trim, dock_sensor
CONF_ZONES = "zones"  # list[dict]: zone_id, name, speaker, room, occupancy[], tvs[], hold, fallback
CONF_DUCK_INPUTS = (
    "duck_inputs"  # list[dict]: entity_id, name, duck_volume, engage_fade, release_fade
)
CONF_PRIMARY_SPEAKER = "primary_speaker"
CONF_TUNABLES = "tunables"  # dict mirroring core.model.Tunables fields
