# Legacy system (homeassistant-bjaalands) — behavior inventory

What the YAML automations this integration replaces actually did, extracted
2026-07-11 from `automations.yaml`, `scripts.yaml`, and the UI template
helpers of the live instance (HA 2026.6.4). Serves as the source of truth
for defaults and for the migration guide.

## Topology

| Zone | Speaker | Model | Trim | Room | Notes |
|---|---|---|---|---|---|
| kjokken | `media_player.kjokken_sonos_move` | Move | 1.2 | kjokken | dockable: `binary_sensor.kjokken_sonos_move_lader` |
| spisebord | `media_player.spisebord_sonos` | Era 100 | 1.1 | stue | |
| sofakrok | `media_player.sofakrok_sonos` | Arc | 1.0 | stue | TV: `media_player.sofakrok_tv` (webOS; `unavailable` when off), `media_player.sofakrok_apple_tv`; fallback zone |

Helpers: `input_number.master_sonos_volume`, `input_number.master_sonos_volume_before_mute`,
`input_boolean.sonos_is_muted`, group `media_player.all_sonos_speakers`.
Occupancy: template sensors over Apollo MSR-2 radar zones per area
(`binary_sensor.{kjokken,spisebord,sofakrok,kontor}_occupancy`).

## Zone activity (template helpers)

- `kjokken_audio_zone` = kjokken occupancy AND Move docked
- `spisebord_audio_zone` = spisebord occupancy
- `sofakrok_audio_zone` = (sofakrok occupancy OR TV not in
  off/standby/unavailable/unknown) OR (both other zones off) ← fallback

## Volume model (`script.sonos_set_room_volume`)

- `target = master × ratio × zone_scale`; ratios above; `zone_scale =
  1/sqrt(active linked zones)` where sofakrok+spisebord are linked and
  kjokken is independent; forced 1.0 while TV on.
- Reverse sync: `master = volume / (ratio × zone_scale)`, applied when
  `volume > 0.01` and `|volume - expected| > 0.02`.

## Automations → behavior

| Automation | Behavior | Timing |
|---|---|---|
| Fade Out When Zone Becomes Inactive | zone off → fade speaker to 0; rebalance others | hold: kjokken 60 s, others 15 s; fade 5 s; rebalance 3 s. Kjokken skipped when undocked. |
| Fade In When Zone Becomes Active | zone on → fade to target (skip if within 0.03); rebalance others only if it was genuinely silent (< 0.02) | fade 3 s; rebalance 2 s |
| Sync Master Volume | master slider → set all active zones | immediate; `mode: queued` |
| Lower Volume on Door Open | all *playing* speakers → 0.05 | immediate |
| Restore Volume on Door Close | active zones back to target | 2 s fade |
| Sync Apple TV Remote to Master | Arc volume change (TV on, stable 1 s, not muted, door closed) → reverse sync | |
| Sync External Volume to Master | any speaker volume stable 2 s → reverse sync; conditions: not muted, door closed, no zone flapped within 10 s, zone active, Move docked | |
| Execute Mute or Unmute | mute: save master → `volume_mute` all; unmute: unmute → restore master | |
| Sync Apple TV Remote Mute | Arc `is_volume_muted` ≠ helper → sync helper | |
| Configure Move When (Un)Docked | status light off/on | stays in YAML (not volume-related) |

## Known pain points being fixed

- `mode: parallel` fade scripts can interleave two ramps on one speaker.
- Own fades re-trigger the external-volume sync (mitigated by `for:` windows
  and a 10 s zone-flap suppression template — fragile).
- Fade-out and fade-in can race when zones flicker (partially mitigated by
  `volume_before < 0.02` guard).
- Master slider writes echo back through `input_number` triggers.
- No group repair; no TV-solo; mute save/restore needed an extra helper.

## Consumers that must be repointed on migration

- Hue Tap Dial blueprint: rotates `input_number.master_sonos_volume`, holds
  toggle `input_boolean.sonos_is_muted` → repoint to conductor entities.
- `Radio - Play on First Arrival`: sets `input_number.master_sonos_volume`
  to 0.15 → repoint.
- `script.announce` targets `media_player.all_sonos_speakers` — unaffected.
- CO2 nudge, welcome announcements, appliance announcements — unaffected.
