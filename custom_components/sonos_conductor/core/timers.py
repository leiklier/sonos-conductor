"""Structured timer identifiers shared by the engine and its tests."""

from __future__ import annotations

ZONE_RELEASE_PREFIX = "zone_release:"
EXTERNAL_DEBOUNCE_PREFIX = "external_debounce:"
GROUP_REPAIR = "group_repair"


def zone_release(zone_id: str) -> str:
    """Hold timer: fires when a RELEASING zone should fade out."""
    return f"{ZONE_RELEASE_PREFIX}{zone_id}"


def external_debounce(speaker_id: str) -> str:
    """Debounce timer for external volume reports of one speaker."""
    return f"{EXTERNAL_DEBOUNCE_PREFIX}{speaker_id}"
