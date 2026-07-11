# Migrating homeassistant-bjaalands to Sonos Conductor

A step-by-step, reversible migration from the YAML automation stack to the
integration. Nothing here needs to happen in one sitting — the conductor's
`enabled` switch makes rollback instant.

Entity ids below are what the integration creates (single device
**Sonos Conductor**):

| Entity | Role |
|---|---|
| `media_player.sonos_conductor` | Master proxy: play/pause/skip → group leader, volume = master, mute = global (HomeKit-friendly, device class *receiver*) |
| `number.sonos_conductor_master_volume` | Master volume 0–1 (replaces `input_number.master_sonos_volume`) |
| `switch.sonos_conductor_enabled` | The conductor acts only while this is on |
| `switch.sonos_conductor_mute` | Global mute (replaces `input_boolean.sonos_is_muted`) |
| `switch.sonos_conductor_tv_solo` | While the TV plays, silence other rooms |
| `switch.sonos_conductor_keep_grouped` | Auto-repair the Sonos group |
| `binary_sensor.sonos_conductor_zone_*` | Zone audibility (replaces `*_audio_zone` helpers), with phase/target/room attributes |
| `number.sonos_conductor_trim_*` | Per-speaker loudness trim |
| `sensor.sonos_conductor_state` | Diagnostics |

## 0. Install (no behavior change yet)

1. HACS → Integrations → ⋮ → Custom repositories →
   `https://github.com/leiklier/sonos-conductor`, category *Integration*.
2. Install **Sonos Conductor**, restart Home Assistant.
3. Settings → Devices & services → Add integration → Sonos Conductor.

## 1. Configure (wizard suggestions → confirm)

The flow discovers the three speakers. Intended configuration:

| Zone | Speaker | Room | Occupancy | TVs | Hold | Fallback | Trim | Dock sensor |
|---|---|---|---|---|---|---|---|---|
| Kjøkken | `media_player.kjokken_sonos_move` | `kjokken` | `binary_sensor.kjokken_occupancy` | — | **60 s** | no | **1.2** | `binary_sensor.kjokken_sonos_move_lader` (auto-discovered) |
| Spisebord | `media_player.spisebord_sonos` | `stue` | `binary_sensor.spisebord_occupancy` | — | 15 s | no | **1.1** | — |
| Sofakrok | `media_player.sofakrok_sonos` | `stue` | `binary_sensor.sofakrok_occupancy` | `media_player.sofakrok_tv`, `media_player.sofakrok_apple_tv` | 15 s | **yes** | 1.0 | — |

- **Room**: type `stue` for both sofakrok and spisebord (shared air → 1/√N
  compensation); kjøkken stays its own room.
- **Occupancy suggestions**: each zone will suggest both the template helper
  (`binary_sensor.<area>_occupancy`) *and* the raw Apollo radar sensor
  (`binary_sensor.<area>_<area>_apollo_msr_2_radar_zone_occupancy`) — they
  are OR-ed, so keeping both is harmless, but deselect the raw one to keep
  the template helper the single source of occupancy truth.
- Duck input: `binary_sensor.gang_inngangsdor`, duck volume 0.05, release
  fade 2 s.
- Tunables: the defaults replicate the legacy timings; nothing to change.

Then turn **off** `switch.sonos_conductor_enabled` for now — the integration
observes but doesn't act.

> Behavior note vs. the YAML system: the old `kjokken_audio_zone` only
> counted the kitchen as active while the Move was docked, and the old
> automations ducked *all playing* speakers on door-open. The conductor
> derives both from first principles (an undocked speaker is standalone and
> untouched; ducking caps *conductor-managed audible* speakers), which
> matches the same real-world outcomes.

## 2. Watch it think (optional but satisfying)

With the conductor disabled, compare `binary_sensor.sonos_conductor_zone_*`
against the legacy `binary_sensor.*_audio_zone` helpers for a day. The
diagnostics sensor shows the master/targets it *would* apply.

## 3. Cut over (minutes, reversible)

1. Disable these 9 automations (Settings → Automations):
   - Sonos - Fade Out When Zone Becomes Inactive
   - Sonos - Fade In When Zone Becomes Active
   - Sonos - Sync Master Volume
   - Sonos - Lower Volume on Door Open
   - Sonos - Restore Volume on Door Close
   - Sonos - Sync Apple TV Remote to Master Volume
   - Sonos - Execute Mute or Unmute
   - Sonos - Sync Apple TV Remote Mute
   - Sonos - Sync External Volume to Master
2. Turn **on** `switch.sonos_conductor_enabled`.

Keep (not volume-related): *Sonos - Configure Move When (Un)Docked* (status
light), *Radio - Play on First Arrival*, announcements, CO2 nudge.

**Rollback** = flip the switch off, re-enable the automations.

## 4. Repoint consumers

- **Philips Hue Tap Dial** (blueprint inputs in `automations.yaml`):
  - `button_1_rotate_clockwise` / `_counter_clockwise`: replace the
    `input_number.set_value` on `input_number.master_sonos_volume` with
    `number.set_value` on `number.sonos_conductor_master_volume` (same 0–1
    scale) — or simpler, `media_player.volume_up`/`volume_down` on
    `media_player.sonos_conductor` (fixed ±0.03 steps).
  - `button_1_hold`: `input_boolean.toggle` on `sonos_is_muted` →
    `switch.toggle` on `switch.sonos_conductor_mute`.
- **Radio - Play on First Arrival**: `input_number.set_value 0.15` →
  `number.set_value` on `number.sonos_conductor_master_volume`.

## 5. Retire the old plumbing (after a comfortable soak)

Deletable: `input_number.master_sonos_volume`,
`input_number.master_sonos_volume_before_mute`,
`input_boolean.sonos_is_muted`, the three `*_audio_zone` template helpers,
scripts `sonos_volume_fade` + `sonos_set_room_volume`, and the 9 disabled
automations. Keep `media_player.all_sonos_speakers` — the announce script
targets it.

## 6. New features to switch on

- **TV solo**: `switch.sonos_conductor_tv_solo` → walking to the kitchen
  during a movie no longer wakes the kitchen speaker; it fades back in when
  the movie ends.
- **Group repair**: `switch.sonos_conductor_keep_grouped` → spontaneous
  group dissolves heal after ~15 s; an undocked Move is deliberately left
  out and rejoins when re-docked.

## 7. HomeKit (Control Center volume)

`media_player.sonos_conductor` has device class *receiver*, which the HomeKit
bridge exposes as a Television accessory. Add it to one of the existing
per-area bridges (e.g. the Sofakrok bridge): Settings → Devices & services →
HomeKit → configure bridge → include `media_player.sonos_conductor`. In iOS,
add the **Apple TV Remote** to Control Center — selecting *Sonos Conductor*
there gives play/pause/skip plus hardware-volume-button control of the
master volume.
