# Sonos Conductor — Architecture

## Why this exists

The feature set (master volume, presence zones, acoustic compensation, ducking,
external-volume reverse sync, dock-aware standalone mode, group repair) outgrew
Home Assistant YAML automations: a dozen automations raced each other through
`input_number` helpers, `for:` stability hacks, and parallel fade scripts that
could interleave on the same speaker. This integration replaces them with a
**pure, deterministic core** driven by a **single-writer actor**, so every race
condition is eliminated *by construction* and every behavior is unit-testable.

## Layering

```
custom_components/sonos_conductor/
├── core/                 PURE PYTHON — no homeassistant imports, ever (CI-enforced)
│   ├── model.py          Config + state dataclasses (the single source of truth)
│   ├── events.py         Input events (frozen dataclasses, discriminated union)
│   ├── effects.py        Output effects (frozen dataclasses, discriminated union)
│   ├── volume_math.py    Pure functions: ratios, room scaling, master mapping
│   ├── plan.py           Plan: effect accumulation in spec 10.5 order
│   ├── zones.py          Zone FSM: lifecycle, docking, fallback (rules 1, 2)
│   ├── audio.py          Master, night mode, mute, reverse sync, ducking, trims (rules 3-5)
│   ├── grouping.py       Group repair (rule 7)
│   ├── reconcile.py      Reconciliation + derived state (section 0, 6.2)
│   └── engine.py         ConductorEngine: state, dispatch, handle(event) -> list[Effect]
│
├── controller.py         The actor: HA events -> queue -> engine -> effect executor
├── discovery.py          Registry scanning (speakers, dock sensors, areas, TVs)
├── config_flow.py        Config + options flow built on discovery suggestions
├── media_player.py       Master media player (HomeKit-friendly proxy)
├── number.py             Per-speaker trim
├── switch.py             enabled / keep_grouped / night_mode
├── select.py             tv_solo (off / same_room / tv_zone) + follow_mode (per_zone / per_room / all_speakers) + idle_attenuation (gentle / balanced / max)
├── binary_sensor.py      Per-zone activity (replaces template helpers)
└── sensor.py             Engine diagnostics
```

**The dependency rule:** `core/` never imports from Home Assistant or from the
outer layer. The outer layer translates between HA and the core's events and
effects. A CI check (`tests/test_purity.py`) imports every `core` module with
`homeassistant` blocked in `sys.modules` to enforce this.

## The core model

### Concepts

- **Speaker** — a Sonos `media_player` plus static config: `trim` (loudness
  ratio compensating for hardware differences, e.g. Move 1.2 / Era 1.1 / Arc
  1.0) and optional discovered *dock sensor* (battery-charging binary sensor on
  the same device → the speaker is *dockable*).
- **Zone** — one speaker + its activity inputs (occupancy sensors, optional TV
  media players) + `hold_seconds` (how long occupancy may be absent before the
  zone releases) + `fallback` flag (the zone that stays active when no zone is).
- **Room** — an acoustic group of zones that share air (e.g. `stue` =
  sofakrok + spisebord). When N zones of a room play simultaneously, each is
  scaled by `1/sqrt(N)` so perceived total loudness stays constant. Zones in
  different rooms don't affect each other.
- **Duck input** — any binary sensor (e.g. entrance door) that, while active,
  caps all playing speakers at a configured `duck_volume`. Multiple duck
  inputs stack; the lowest cap wins.
- **TV mode** — while a zone's TV plays: that room's scale is forced to 1.0
  (Apple TV remote gets a 1:1 mapping) and, per the *TV solo* mode, other
  zones are suppressed: `same_room` silences zones in other rooms (no more
  kitchen music while fetching water), `tv_zone` silences every zone except
  the TV zone itself, `off` suppresses nothing.
- **Night mode** — a global volume ceiling: while the `night_mode` switch is
  on, no speaker plays above `night_volume_cap` (tunable, default 0.15).
  The master volume is untouched, so switching night mode off restores the
  exact previous targets. Scheduling is deliberately external (an HA
  automation flips the switch); the engine only sees `SetNightMode`.

### Zone lifecycle FSM

```
                 occupancy on            hold expires
   ┌────────┐ ──────────────────▶ ┌────────┐             ┌─────────────┐
   │  IDLE  │                     │ ACTIVE │ ──────────▶ │ RELEASING   │──▶ IDLE
   └────────┘ ◀────────────────── └────────┘  occupancy  │ (hold timer)│
        ▲        fade-out done         ▲       off       └─────────────┘
        │                              └──── occupancy on ──────┘
        │
   ┌────────────┐   undocked (dock sensor off)
   │ STANDALONE │ ◀────────────────────────────── any state
   └────────────┘ ──── docked again ────▶ re-evaluated from inputs
```

- `ACTIVE` — participates in master volume fan-out and reverse sync.
- `RELEASING` — occupancy lost, hold timer pending; still at full target
  (re-entry cancels the timer with no audible change — no flicker).
- `STANDALONE` — undocked portable speaker: excluded from *everything*
  (master fan-out, reverse sync, ducking, group repair). The user owns it.
  Docking re-admits it.
- The `fallback` zone is ACTIVE whenever it would otherwise be IDLE *and* no
  other zone is ACTIVE/RELEASING (someone is home ⇒ at least one live zone).

Fades are **not** FSM states: the engine emits `RampVolume` effects; the
controller runs (and cancels) the actual ramps. The engine only tracks the
*commanded target* per speaker.

### Reconciliation, not deltas

After **every** event the engine recomputes the desired volume of **every**
speaker from scratch:

```
desired(speaker) = 0                                    if zone not audible
                 = master × trim × room_scale(room)     otherwise
capped by duck:  min(desired, duck_volume)              while any duck active
capped by night: min(desired, night_volume_cap)         while night mode on
muted:           mute flag handled via SetMute effects, volume preserved
```

It then emits effects only for speakers whose desired target differs from the
last commanded target (with transition-appropriate fade durations). Commands
are idempotent; a lost or duplicated event can never wedge the system in a
wrong state — the next event heals it.

### Reverse sync (external volume changes)

When a speaker's volume changes and it *wasn't us* (see echo suppression), the
engine debounces it (`external_debounce`, default 1.5 s, timer effect) and then
solves the master equation backwards:

```
new_master = clamp(reported_volume / (trim × room_scale), 0, 1)
```

applied only if the zone is ACTIVE, not muted, not ducked, no zone transition
happened within `transition_suppression` (default 10 s), and the implied master
change exceeds `sync_threshold` (default 0.02). Then normal reconciliation
fans the new master out to the other speakers. This single path covers all
three sources: Apple TV remote (Arc, room scale forced 1.0 while TV on),
speaker touch controls, and the Sonos app. Mute changes reported by the Arc
sync into the global mute the same way.

## Race-condition strategy (the whole point)

1. **Single-writer actor.** Every input — HA state events, entity commands,
   service calls, timer expiries — becomes an `Event` on one `asyncio.Queue`,
   consumed by one task, processed by a synchronous pure engine. Concurrent
   mutation is impossible by construction.
2. **Echo suppression ledger.** Every volume/mute write the controller issues
   is recorded (entity, value, TTL ≈ 3 s). Incoming state events matching a
   pending write (ε = 0.005) are consumed as acknowledgements, not fed to the
   engine. Our own fades can never be mistaken for user input — this replaces
   the old system's fragile `for:`-duration guesswork.
3. **One cancellable ramp per speaker.** A new target for a speaker atomically
   cancels its in-flight ramp. (The old `mode: parallel` fade script could
   interleave two ramps on the same speaker — the flagship race.)
4. **Debounce + suppression in the engine, not in YAML.** Stability windows
   are explicit engine state driven by injected monotonic time — deterministic
   and unit-tested, not wall-clock-dependent template hacks.
5. **Startup reconciliation.** On start/reload the controller snapshots all
   entity states, the engine adopts current volumes (inferring master from the
   median active speaker) and converges gently — no volume jumps on restart.
6. **Time is an input.** The engine never calls a clock; `now` (monotonic) is
   passed with every event, and delays are `StartTimer`/`CancelTimer` effects.
   Tests simulate hours of behavior in milliseconds.

## The controller (HA adapter)

```
HA state_changed ──┐
entity commands ───┼──▶ translate ──▶ Queue ──▶ engine.handle(event, now)
timer expiry ──────┘                                    │
                                              list[Effect]
                                                        ▼
                                          effect executor (async)
                                          ├─ RampVolume  → ramp task (cancel old)
                                          ├─ SetMute     → media_player.volume_mute + ledger
                                          ├─ StartTimer  → loop.call_later → TimerFired event
                                          ├─ JoinGroup   → media_player.join (+ cooldown)
                                          └─ UpdateX     → push state to conductor entities
```

Group repair: `group_members` changes are observed; if `keep_grouped` is on
and a non-standalone speaker leaves the expected group without a conductor
command (echo-suppressed via a post-command cooldown), a repair timer
(default 15 s) fires a `JoinGroup` effect. Undocked speakers are expected to
be absent; re-docking triggers repair too.

## Entities

| Entity | Purpose |
|---|---|
| `media_player.sonos_conductor` | Master proxy: play/pause/next/prev to group leader, **volume slider = master, mute button = global mute** (the master volume and mute live here — no separate `number`/`switch` duplicate them). `device_class: tv`-style TelevisionAccessory → expose via an existing HomeKit bridge for Control-Center volume control. |
| `switch.<name>_enabled` | Kill switch — instant rollback to old automations during migration. |
| `switch.<name>_night_mode` | Global volume ceiling (`night_volume_cap`): no speaker plays above the cap while on. Restored across restarts; flip it from an HA automation for scheduling. |
| `select.<name>_tv_solo` | TV-solo mode: off / same room / TV zone only. |
| `select.<name>_follow_mode` | Follow mode (rule 1.9): per zone / per room / all speakers. How far presence spreads audibility; orthogonal to TV solo. Restored across restarts. |
| `select.<name>_idle_attenuation` | Idle attenuation (rule 3.4): gentle / balanced / max. How much volume idle zones keep as a background bed; max = silent (default). Restored across restarts. |
| `switch.<name>_keep_grouped` | Runtime toggle for group repair. |
| `binary_sensor.<name>_zone_<zone>` | Zone audible? Attributes: FSM state, target volume, room scale. Replaces the `*_audio_zone` template helpers. |
| `sensor.<name>_state` | Diagnostics: engine state snapshot, last event, effect counts. |
| `number.<name>_trim_<speaker>` | Per-speaker loudness trim, adjustable at runtime. |

## Config flow & discovery

1. **Speakers** — all `media_player`s from the `sonos` platform (this
   automatically excludes Music Assistant mirrors). User deselects any.
2. **Dockable detection** — automatic, zero config: a `battery_charging`
   binary sensor on the speaker's device ⇒ dockable ⇒ undock → STANDALONE.
3. **Zones** — one per speaker, named from the speaker's device area.
   Occupancy sensors default to `motion`/`occupancy`/`presence` binary sensors
   in the same area (falling back to entity-id heuristics for area-less
   template helpers). TV players suggested from non-Sonos media players in the
   area. Hold time per zone (default 15 s).
4. **Rooms** — user groups zones that share air; default one room per zone.
   One zone may be marked `fallback`.
5. **Duck inputs** — any binary sensors + duck volume (default 0.05) +
   restore fade (default 2 s).
6. **Tunables** (options flow, sane defaults): fade durations (in 3 s /
   out 5 s / rebalance 2 s), `sync_threshold`, `external_debounce`,
   `transition_suppression`, group-repair delay, `night_volume_cap`.

All of this lives in the config entry's `options` so it is editable without
re-adding the integration.

## Testing

- `tests/core/` — pure pytest, no HA: scenario tests feed event sequences and
  assert effect sequences. Race regressions live here (fade-out during
  fade-in, duck during ramp, external change mid-fade, dock flapping…).
- `tests/` (component) — `pytest-homeassistant-custom-component`
  (pinned `0.13.340` ⇒ `homeassistant==2026.6.4`, matching production):
  config/options flow, controller against fake speaker states, entity
  behavior, echo suppression against real `state_changed` plumbing.
- CI: ruff (lint+format), pytest on 3.14, hassfest, HACS validation.

## Migration (from homeassistant-bjaalands)

See [MIGRATION.md](MIGRATION.md). Summary: install via HACS custom repo, add
the integration, verify zone behavior with `switch.<name>_enabled` off →
disable the 11 Sonos automations + flip the switch on. The Hue Tap Dial and
radio-on-arrival automations repoint `input_number.master_sonos_volume` →
`media_player.volume_set`/`volume_up`/`volume_down` on
`media_player.sonos_conductor` and `input_boolean.sonos_is_muted` →
`media_player.volume_mute` on the same entity. The `*_audio_zone` template
helpers and the two Sonos scripts become deletable.
