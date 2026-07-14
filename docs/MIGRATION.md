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

`media_player.sonos_conductor` has device class *tv*, which HomeKit exposes
as a Television-category accessory — the category the Home app renders with
the input picker and a working power toggle (*receiver*-category accessories
only get a power button). Expose it through a HomeKit entry in **accessory
mode** (Settings → Devices & services → HomeKit; HA requires accessory mode
for television media players).

> **Upgrading from ≤ v0.2.0:** Apple caches the accessory category at pairing
> time. If the conductor was already paired (as a receiver), remove that
> HomeKit entry in HA (which removes the accessory from the Home app) and add
> a fresh accessory-mode entry — inputs and power will then render correctly.

The power button maps to playback: on = play, off = pause; the entity
correspondingly reports `off` whenever the leader is not playing. In iOS, add
the **Apple TV Remote** to Control Center — selecting *Sonos Conductor*
there gives play/pause plus hardware-volume-button control of the master
volume, and swiping left/right on the touch surface skips tracks (the
integration translates the bridge's remote-key events).

**Radio stations & inputs**: the accessory also exposes the leader's sources
(Sonos favorites — radio stations, playlists — and the Arc's TV input) as
HomeKit inputs, so you can switch station from the Home app tile. A synthetic
**Other** input absorbs anything no favorite matches (Spotify Connect,
announcements); selecting it does nothing. Limit which sources appear under
*Configure → Media — sources exposed to HomeKit* (empty = all). Reload the
HomeKit entry after changing the selection so the Home app picks up the new
inputs.

## Upgrading to rich presence (Presence Conductor)

If [Presence Conductor](https://github.com/leiklier/presence-conductor) is
installed, point each zone at its room instead of raw radar/template
sensors: *Configure → Zones*, pick the room's occupancy sensor in
**Presence Conductor room** and clear the plain **Occupancy sensors** list
(discovery pre-fills exactly this for new installs). The room's activity
sensor is found automatically on the same device; no extra selection needed.

What changes in behavior:

- The zone still fades in the moment the room reports occupied (passing
  counts — the music follows you immediately).
- The hold time becomes activity-aware: a visit that never rose above
  *passing* releases after `hold_seconds × passing hold scale` (default
  0.3×), while a room someone was *settled* in holds for
  `hold_seconds × settled hold scale` (default 4×). Both scales live under
  *Configure → Tunables*.
- Under *Configure → Home presence*, select Presence Conductor's
  **Anyone home** sensor to silence the fallback zone while the home is
  empty (replaces radio-on-arrival style automations: the fallback fades
  back in when someone comes home). If the sensor goes unavailable the
  conductor fails safe and behaves as if someone is home.

Zone template helpers like `*_audio_zone` that merely OR occupancy sources
can be deleted once their zone points at a Presence Conductor room.
