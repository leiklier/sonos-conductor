# Sonos Conductor

[![CI](https://github.com/leiklier/sonos-conductor/actions/workflows/ci.yml/badge.svg)](https://github.com/leiklier/sonos-conductor/actions/workflows/ci.yml)

Presence-aware multi-room Sonos volume orchestration for Home Assistant —
a testable state machine replacing YAML automation sprawl.

## What it does

- **Master volume** — one slider / dial / HomeKit control for the whole house.
- **Presence zones** — speakers fade in when a zone becomes occupied and fade
  out (after a hold time) when it empties.
- **Acoustic compensation** — zones that share a room are scaled by `1/√N` so
  total loudness stays constant as people move around.
- **Loudness trims** — per-speaker ratios compensate for hardware differences
  (a Move needs more gain than an Arc to feel equally loud).
- **Ducking** — any binary sensor (e.g. the entrance door) temporarily caps
  volume; release restores exactly what presence dictates.
- **External volume sync** — volume changed on the speaker itself, in the
  Sonos app, or with the Apple TV remote is solved back into the master and
  rebalanced across the fleet. Mute syncs both ways too.
- **Dock-aware standalone mode** — undock a portable speaker (Sonos Move) and
  the conductor hands it over to you untouched; dock it again and it rejoins
  the choreography. Detected automatically from its charging sensor.
- **TV solo** — while the TV plays, optionally keep the music out of the other
  rooms (no more kitchen soundtrack while fetching a glass of water).
- **Group repair** — if the Sonos group spontaneously dissolves, the conductor
  quietly reassembles it.
- **HomeKit-friendly** — exposes a master media player as a HomeKit
  Television accessory: volume and play/pause from Control Center, swipe
  left/right in the iOS Remote to skip tracks, power toggle = play/pause, and
  your Sonos favorites (radio stations, playlists) plus hardware inputs
  appear as selectable inputs in the Home app.

## Why an integration instead of automations

The equivalent YAML (a dozen automations, two fade scripts, five helpers)
races itself: parallel fades interleave on one speaker, self-inflicted volume
changes bounce back through "external change" triggers, stability is imitated
with `for:` timers. Sonos Conductor runs everything through a **pure,
deterministic state machine** fed by a **single-writer event queue** with an
**echo-suppression ledger** — races are impossible by construction, and every
behavior is unit-tested. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/ENGINE_SPEC.md](docs/ENGINE_SPEC.md). Migrating from a YAML setup?
[docs/MIGRATION.md](docs/MIGRATION.md) is a worked example.

## Installation

Via [HACS](https://hacs.xyz): add `https://github.com/leiklier/sonos-conductor`
as a custom repository (category: integration), install, restart, then add the
**Sonos Conductor** integration. The config flow discovers your Sonos
speakers, their areas, occupancy sensors, TVs, and dock sensors and suggests a
configuration — you only confirm and group zones into acoustic rooms.

## Development

```bash
uv sync            # Python 3.14 + homeassistant 2026.6.4 test harness
uv run pytest      # core unit tests + HA component tests
uv run ruff check .
```

Layout: `custom_components/sonos_conductor/core/` is pure Python (no HA
imports, CI-enforced); the surrounding files adapt it to Home Assistant.
