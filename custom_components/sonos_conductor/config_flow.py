"""Config and options flow for Sonos Conductor.

The setup wizard walks speakers -> one zone per speaker -> duck inputs ->
tunables, prefilled from registry discovery (see discovery.py). Everything is
stored in ``entry.options`` (``entry.data`` stays empty) using the contract
documented in const.py. The options flow re-runs the zone/duck/tunable steps
seeded from the stored options and merges its result back, preserving any
keys it does not own (notably the runtime-written ``last_master``).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util import slugify

from . import discovery
from .const import (
    CONF_DUCK_INPUTS,
    CONF_HOME_PRESENCE,
    CONF_HOMEKIT_SOURCES,
    CONF_PRIMARY_SPEAKER,
    CONF_SPEAKERS,
    CONF_TUNABLES,
    CONF_ZONES,
    DOMAIN,
)
from .core.model import DuckInputConfig, SpeakerConfig, Tunables, ZoneConfig


def _defaults(cls: type) -> dict[str, Any]:
    """Field defaults of a core dataclass (the single source of truth)."""
    return {
        f.name: f.default for f in dataclasses.fields(cls) if f.default is not dataclasses.MISSING
    }


_ZONE_DEFAULTS = _defaults(ZoneConfig)
_SPEAKER_DEFAULTS = _defaults(SpeakerConfig)
_DUCK_DEFAULTS = _defaults(DuckInputConfig)

#: UI metadata (min, max, step, unit) per Tunables field. Defaults come from
#: the dataclass; unlisted (future) fields get _TUNABLE_FALLBACK_UI.
_TUNABLE_UI: dict[str, tuple[float, float, float, str | None]] = {
    "fade_in": (0, 60, 0.5, "s"),
    "fade_out": (0, 60, 0.5, "s"),
    "rebalance_fade": (0, 60, 0.5, "s"),
    "master_fade": (0, 60, 0.5, "s"),
    "sync_threshold": (0, 0.5, 0.01, None),
    "external_debounce": (0, 30, 0.1, "s"),
    "transition_suppression": (0, 120, 0.5, "s"),
    "group_repair_delay": (0, 300, 1, "s"),
    "startup_tolerance": (0, 0.5, 0.01, None),
    "night_volume_cap": (0.0, 1.0, 0.01, None),
    "hold_passing_scale": (0.0, 2.0, 0.05, None),
    "hold_settled_scale": (1.0, 10.0, 0.5, None),
}
_TUNABLE_FALLBACK_UI: tuple[float, float, float, str | None] = (0, 600, 0.01, None)


def _number(minimum: float, maximum: float, step: float, unit: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX)
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def _zone_schema(include_name: bool) -> vol.Schema:
    """Schema for one zone (setup and options; options keeps the name fixed)."""
    schema: dict[Any, Any] = {}
    if include_name:
        schema[vol.Required("name")] = TextSelector()
    schema.update(
        {
            vol.Required("room"): TextSelector(),
            vol.Optional("presence_entity"): EntitySelector(
                EntitySelectorConfig(
                    domain="binary_sensor",
                    integration=discovery.PRESENCE_PLATFORM,
                    device_class="occupancy",
                )
            ),
            vol.Optional("occupancy", default=[]): EntitySelector(
                EntitySelectorConfig(domain="binary_sensor", multiple=True)
            ),
            vol.Optional("tvs", default=[]): EntitySelector(
                EntitySelectorConfig(domain="media_player", multiple=True)
            ),
            vol.Required("hold_seconds", default=_ZONE_DEFAULTS["hold_seconds"]): _number(
                0, 600, 1, "s"
            ),
            vol.Required("fallback", default=_ZONE_DEFAULTS["fallback"]): BooleanSelector(),
            vol.Required("trim", default=_SPEAKER_DEFAULTS["trim"]): _number(0.5, 2.0, 0.05),
            vol.Optional("dock_sensor"): EntitySelector(
                EntitySelectorConfig(
                    domain="binary_sensor", device_class=discovery.DOCK_DEVICE_CLASS
                )
            ),
        }
    )
    return vol.Schema(schema)


def _ducks_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional("duck_entities", default=[]): EntitySelector(
                EntitySelectorConfig(
                    domain="binary_sensor",
                    device_class=list(discovery.DUCK_DEVICE_CLASSES),
                    multiple=True,
                )
            ),
            vol.Required("duck_volume", default=_DUCK_DEFAULTS["duck_volume"]): NumberSelector(
                NumberSelectorConfig(min=0, max=1, step=0.01, mode=NumberSelectorMode.SLIDER)
            ),
            vol.Required("release_fade", default=_DUCK_DEFAULTS["release_fade"]): _number(
                0, 30, 0.5, "s"
            ),
        }
    )


def _home_schema() -> vol.Schema:
    """Optional home-level presence input (Presence Conductor "Anyone home")."""
    return vol.Schema(
        {
            vol.Optional("home_presence_entity"): EntitySelector(
                EntitySelectorConfig(
                    domain="binary_sensor",
                    integration=discovery.PRESENCE_PLATFORM,
                    device_class="presence",
                )
            )
        }
    )


def _tunables_schema() -> vol.Schema:
    schema: dict[Any, Any] = {}
    for field in dataclasses.fields(Tunables):
        minimum, maximum, step, unit = _TUNABLE_UI.get(field.name, _TUNABLE_FALLBACK_UI)
        schema[vol.Required(field.name, default=field.default)] = _number(
            minimum, maximum, step, unit
        )
    return vol.Schema(schema)


def _tunables_from_input(user_input: dict[str, Any]) -> dict[str, float]:
    return {f.name: user_input[f.name] for f in dataclasses.fields(Tunables)}


def _build_duck_inputs(
    hass: Any, user_input: dict[str, Any], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Duck input dicts for the selected entities; keep names/engage fades of survivors."""
    stored = {duck["entity_id"]: duck for duck in existing}
    ducks: list[dict[str, Any]] = []
    for entity_id in user_input["duck_entities"]:
        old = stored.get(entity_id, {})
        ducks.append(
            {
                "entity_id": entity_id,
                "name": old.get("name") or discovery.friendly_name(hass, entity_id),
                "duck_volume": user_input["duck_volume"],
                "engage_fade": old.get("engage_fade", _DUCK_DEFAULTS["engage_fade"]),
                "release_fade": user_input["release_fade"],
            }
        )
    return ducks


def _primary_speaker(zones: list[dict[str, Any]], speakers: list[dict[str, Any]]) -> str | None:
    """The fallback zone's speaker if any, else the first speaker."""
    for zone in zones:
        if zone.get("fallback"):
            return zone["speaker"]
    if speakers:
        return speakers[0]["entity_id"]
    return None


class SonosConductorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Multi-step setup wizard driven by registry discovery."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: list[discovery.DiscoveredSpeaker] = []
        self._selected: list[discovery.DiscoveredSpeaker] = []
        self._zone_index = 0
        self._zones: list[dict[str, Any]] = []
        self._speakers: list[dict[str, Any]] = []
        self._ducks: list[dict[str, Any]] = []
        self._home_presence: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SonosConductorOptionsFlow:
        """Options flow for editing zones, ducks and tunables."""
        return SonosConductorOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Intro + speaker selection (single instance)."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if not self._discovered:
            self._discovered = discovery.discover_speakers(self.hass)
        if not self._discovered:
            return self.async_abort(reason="no_speakers_found")

        errors: dict[str, str] = {}
        if user_input is not None:
            selected = set(user_input["speakers"])
            if not selected:
                errors["speakers"] = "no_speakers_selected"
            else:
                self._selected = [s for s in self._discovered if s.entity_id in selected]
                return await self.async_step_zone()

        options = [
            SelectOptionDict(
                value=speaker.entity_id,
                label=f"{speaker.name} ({speaker.area_name})"
                if speaker.area_name
                else speaker.name,
            )
            for speaker in self._discovered
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    "speakers", default=[s.entity_id for s in self._discovered]
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=options, multiple=True, mode=SelectSelectorMode.LIST
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_zone(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """One zone per selected speaker (looping step)."""
        speaker = self._selected[self._zone_index]
        errors: dict[str, str] = {}

        if user_input is not None:
            zone_id = slugify(user_input["name"])
            if not zone_id:
                errors["name"] = "invalid_zone_name"
            elif any(zone["zone_id"] == zone_id for zone in self._zones):
                errors["name"] = "duplicate_zone_name"
            if user_input["fallback"] and any(zone["fallback"] for zone in self._zones):
                errors["fallback"] = "multiple_fallback_zones"
            if not errors:
                self._zones.append(
                    {
                        "zone_id": zone_id,
                        "name": user_input["name"],
                        "speaker": speaker.entity_id,
                        "room": slugify(user_input["room"]),
                        "presence_entity": user_input.get("presence_entity"),
                        "occupancy": user_input["occupancy"],
                        "tvs": user_input["tvs"],
                        "hold_seconds": user_input["hold_seconds"],
                        "fallback": user_input["fallback"],
                    }
                )
                self._speakers.append(
                    {
                        "entity_id": speaker.entity_id,
                        "name": speaker.name,
                        "trim": user_input["trim"],
                        "dock_sensor": user_input.get("dock_sensor"),
                    }
                )
                self._zone_index += 1
                if self._zone_index < len(self._selected):
                    return await self.async_step_zone()
                return await self.async_step_ducks()

        if user_input is not None:
            # Redisplay after a validation error: keep what the user typed.
            suggested = user_input
        else:
            # A Presence Conductor room is the preferred presence source:
            # when one matches the area, suggest it and leave the plain
            # occupancy sensors empty (they remain selectable as extras).
            presence = discovery.suggest_presence(self.hass, speaker.area_id, speaker.area_name)
            suggested = {
                "name": speaker.area_name or speaker.name,
                "room": speaker.area_name or speaker.name,
                "presence_entity": presence,
                "occupancy": []
                if presence
                else discovery.suggest_occupancy(self.hass, speaker.area_id, speaker.area_name),
                "tvs": discovery.suggest_tvs(self.hass, speaker.area_id),
                "dock_sensor": speaker.dock_sensor,
            }
        schema = self.add_suggested_values_to_schema(
            _zone_schema(include_name=True),
            {key: value for key, value in suggested.items() if value is not None},
        )
        return self.async_show_form(
            step_id="zone",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "speaker": speaker.name,
                "zone_number": str(self._zone_index + 1),
                "zone_count": str(len(self._selected)),
            },
        )

    async def async_step_ducks(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select duck inputs and their shared cap/fade."""
        if user_input is not None:
            self._ducks = _build_duck_inputs(self.hass, user_input, [])
            return await self.async_step_home()
        return self.async_show_form(step_id="ducks", data_schema=_ducks_schema())

    async def async_step_home(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Optional home-level presence gate for the fallback zone."""
        if user_input is not None:
            self._home_presence = user_input.get("home_presence_entity")
            return await self.async_step_tunables()
        schema = self.add_suggested_values_to_schema(
            _home_schema(),
            {"home_presence_entity": discovery.suggest_home_presence(self.hass)},
        )
        return self.async_show_form(step_id="home", data_schema=schema)

    async def async_step_tunables(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Behavioral tuning knobs, then create the entry."""
        if user_input is not None:
            options = {
                CONF_SPEAKERS: self._speakers,
                CONF_ZONES: self._zones,
                CONF_DUCK_INPUTS: self._ducks,
                CONF_PRIMARY_SPEAKER: _primary_speaker(self._zones, self._speakers),
                CONF_HOME_PRESENCE: self._home_presence,
                CONF_TUNABLES: _tunables_from_input(user_input),
            }
            return self.async_create_entry(title="Sonos Conductor", data={}, options=options)
        return self.async_show_form(step_id="tunables", data_schema=_tunables_schema())


class SonosConductorOptionsFlow(OptionsFlow):
    """Edit zones, duck inputs or tunables; merge back preserving other keys."""

    def __init__(self) -> None:
        self._zone_index = 0
        self._zones: list[dict[str, Any]] = []
        self._speakers: list[dict[str, Any]] = []

    def _save(self, updates: dict[str, Any]) -> ConfigFlowResult:
        """Merge our sections into the options, preserving everything else.

        Notably ``last_master`` (written at runtime by the controller) and
        any future keys survive untouched.
        """
        new_options = {**self.config_entry.options, **updates}
        return self.async_create_entry(title="", data=new_options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Section menu."""
        return self.async_show_menu(
            step_id="init", menu_options=["zones", "ducks", "home", "tunables", "media"]
        )

    async def async_step_home(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit the home-level presence gate."""
        if user_input is not None:
            return self._save({CONF_HOME_PRESENCE: user_input.get("home_presence_entity")})
        stored = self.config_entry.options.get(CONF_HOME_PRESENCE)
        schema = self.add_suggested_values_to_schema(
            _home_schema(),
            # Stored value wins; discovery only fills the gap for entries
            # created before the option existed.
            {
                "home_presence_entity": stored
                if CONF_HOME_PRESENCE in self.config_entry.options
                else discovery.suggest_home_presence(self.hass)
            },
        )
        return self.async_show_form(step_id="home", data_schema=schema)

    async def async_step_media(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose which leader sources the master player exposes (e.g. to HomeKit)."""
        if user_input is not None:
            return self._save({CONF_HOMEKIT_SOURCES: user_input.get("homekit_sources", [])})

        stored: list[str] = list(self.config_entry.options.get(CONF_HOMEKIT_SOURCES, []))
        # Offer the leader's current sources; keep stored entries selectable
        # even if the leader is momentarily unavailable.
        leader = self.config_entry.options.get(CONF_PRIMARY_SPEAKER)
        available: list[str] = []
        if leader and (state := self.hass.states.get(leader)):
            available = list(state.attributes.get("source_list") or [])
        options = sorted(set(available) | set(stored))
        schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Optional("homekit_sources", default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            custom_value=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            {"homekit_sources": stored},
        )
        return self.async_show_form(step_id="media", data_schema=schema)

    async def async_step_zones(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Re-run the zone step for every stored zone, seeded from options."""
        stored_zones: list[dict[str, Any]] = list(self.config_entry.options.get(CONF_ZONES, []))
        if not stored_zones:
            return self.async_abort(reason="no_zones_configured")
        stored_speakers: list[dict[str, Any]] = list(
            self.config_entry.options.get(CONF_SPEAKERS, [])
        )
        speakers_by_id = {speaker["entity_id"]: speaker for speaker in stored_speakers}

        zone = dict(stored_zones[self._zone_index])
        speaker = dict(
            speakers_by_id.get(
                zone["speaker"],
                {
                    "entity_id": zone["speaker"],
                    "name": zone["speaker"],
                    "trim": _SPEAKER_DEFAULTS["trim"],
                    "dock_sensor": None,
                },
            )
        )

        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input["fallback"] and any(z["fallback"] for z in self._zones):
                errors["fallback"] = "multiple_fallback_zones"
            if not errors:
                zone.update(
                    {
                        "room": slugify(user_input["room"]),
                        "presence_entity": user_input.get("presence_entity"),
                        "occupancy": user_input["occupancy"],
                        "tvs": user_input["tvs"],
                        "hold_seconds": user_input["hold_seconds"],
                        "fallback": user_input["fallback"],
                    }
                )
                speaker["trim"] = user_input["trim"]
                speaker["dock_sensor"] = user_input.get("dock_sensor")
                self._zones.append(zone)
                self._speakers.append(speaker)
                self._zone_index += 1
                if self._zone_index < len(stored_zones):
                    return await self.async_step_zones()

                edited = {speaker["entity_id"]: speaker for speaker in self._speakers}
                new_speakers = [
                    edited.pop(speaker["entity_id"], dict(speaker)) for speaker in stored_speakers
                ]
                new_speakers.extend(edited.values())
                return self._save(
                    {
                        CONF_ZONES: self._zones,
                        CONF_SPEAKERS: new_speakers,
                        CONF_PRIMARY_SPEAKER: _primary_speaker(self._zones, new_speakers),
                    }
                )

        if user_input is not None:
            suggested = user_input
        else:
            # Stored values win; discovery only fills gaps (e.g. a speaker
            # that gained a dock sensor, or keys missing from older options).
            discovered = {s.entity_id: s for s in discovery.discover_speakers(self.hass)}
            disc = discovered.get(zone["speaker"])
            suggested = {
                "room": zone.get("room") or (disc.area_name if disc else zone["name"]),
                "presence_entity": zone.get(
                    "presence_entity",
                    discovery.suggest_presence(self.hass, disc.area_id, disc.area_name)
                    if disc
                    else None,
                ),
                "occupancy": zone.get(
                    "occupancy",
                    discovery.suggest_occupancy(self.hass, disc.area_id, disc.area_name)
                    if disc
                    else [],
                ),
                "tvs": zone.get(
                    "tvs", discovery.suggest_tvs(self.hass, disc.area_id) if disc else []
                ),
                "hold_seconds": zone.get("hold_seconds", _ZONE_DEFAULTS["hold_seconds"]),
                "fallback": zone.get("fallback", _ZONE_DEFAULTS["fallback"]),
                "trim": speaker.get("trim", _SPEAKER_DEFAULTS["trim"]),
                "dock_sensor": speaker.get("dock_sensor") or (disc.dock_sensor if disc else None),
            }
        schema = self.add_suggested_values_to_schema(
            _zone_schema(include_name=False),
            {key: value for key, value in suggested.items() if value is not None},
        )
        return self.async_show_form(
            step_id="zones",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "zone": zone["name"],
                "zone_number": str(self._zone_index + 1),
                "zone_count": str(len(stored_zones)),
            },
        )

    async def async_step_ducks(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Re-run the duck step seeded from the stored duck inputs."""
        stored: list[dict[str, Any]] = list(self.config_entry.options.get(CONF_DUCK_INPUTS, []))
        if user_input is not None:
            return self._save({CONF_DUCK_INPUTS: _build_duck_inputs(self.hass, user_input, stored)})

        first = stored[0] if stored else {}
        schema = self.add_suggested_values_to_schema(
            _ducks_schema(),
            {
                "duck_entities": [duck["entity_id"] for duck in stored],
                "duck_volume": first.get("duck_volume", _DUCK_DEFAULTS["duck_volume"]),
                "release_fade": first.get("release_fade", _DUCK_DEFAULTS["release_fade"]),
            },
        )
        return self.async_show_form(step_id="ducks", data_schema=schema)

    async def async_step_tunables(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-run the tunables step seeded from the stored tunables."""
        if user_input is not None:
            return self._save({CONF_TUNABLES: _tunables_from_input(user_input)})
        stored = self.config_entry.options.get(CONF_TUNABLES, {})
        schema = self.add_suggested_values_to_schema(_tunables_schema(), stored)
        return self.async_show_form(step_id="tunables", data_schema=schema)
