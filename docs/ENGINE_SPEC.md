# ConductorEngine — Behavioral Specification

This is the normative contract for `core/engine.py`. Every numbered rule
should be covered by at least one test in `tests/core/`. Defaults referenced
here live in `core/model.py::Tunables`. The legacy YAML system this replaces
is documented in [LEGACY_BEHAVIOR.md](LEGACY_BEHAVIOR.md); where the two
conflict, this spec wins.

## 0. Definitions

- **audible(zone)** — `zone.phase ∈ {ACTIVE, RELEASING}` and the zone is not
  *solo-suppressed* (rule 6).
- **zone_level(zone)** — the zone's relative volume level: `1.0` if
  *audible(zone)*; `0.0` if STANDALONE (2.3), solo-suppressed (6.2) or
  `anyone_home is False` (1.8); else the **idle bed** fraction selected by
  `idle_attenuation` (rule 3.4): `idle_gentle_level` / `idle_balanced_level`
  / `0.0` for GENTLE / BALANCED / MAX.
- **room_scale(room)** — `volume_math.room_scale(zone_levels of the room's
  zones, any audible zone in room has tv_playing)` = `1/sqrt(Σ level²)`,
  never above 1.0 (a room summing quieter than one full zone is not
  boosted), forced to 1.0 while a TV plays. For N audible zones this is the
  classic `1/√N`; beds weigh in by their level, so total perceived room
  loudness is one full zone regardless of the bed.
- **desired(speaker)** —
  - `None` (do not touch) if speaker is STANDALONE or engine disabled;
  - `0.0` if `zone_level(zone) = 0`;
  - else `zone_level × min(speaker_target(master, trim, room_scale),
    duck_cap, night_cap)`
    where `duck_cap` = lowest `duck_volume` among active duck inputs (∞ if
    none) and `night_cap` = `night_volume_cap` while `night_mode` is on
    (∞ otherwise). The level multiplies the *capped* target: an idle bed is
    always its preset fraction of what the zone would play if active, so
    its relative attenuation survives night mode and ducking (a gentle bed
    at night plays half the cap, not up to the cap). This is the single
    point where both caps apply — fades, zone activations, rebalances,
    group joins, idle beds and startup all go through it.
- **reconcile(fade_context)** — for every speaker whose `desired ≠ commanded`
  (per `volumes_equal`), emit `RampVolume(speaker, desired, duration)` and set
  `commanded = desired`. The duration comes from the *cause*:

  | cause | duration |
  |---|---|
  | zone fade-in (IDLE→ACTIVE) | `fade_in` for that speaker; `rebalance_fade` for others rebalanced by the scale change |
  | zone fade-out (RELEASING/ACTIVE→IDLE) | `fade_out` / `rebalance_fade` for others |
  | master change | `master_fade` (all) |
  | duck engage / release | duck input's `engage_fade` / `release_fade` |
  | night mode engage / release | `rebalance_fade` |
  | idle-attenuation change | `rebalance_fade` |
  | external-volume sync | `0` for the reporting speaker (it is already there), `rebalance_fade` for others |
  | TV mode / tv-solo-mode change | `rebalance_fade` |
  | enable / startup convergence | `rebalance_fade` |

Reconciliation is *the* only way volumes are changed. Every event handler is
"update state, then reconcile".

## 1. Zone lifecycle

Zone activation is driven by **effective occupancy** (rule 1.4), widened by
the **follow mode** (rule 1.9). Where a rule below says "occupied / TV", read
it as `effective_occupied(zone)` — the follow mode decides how far a zone's
presence reaches.

1.1 `OccupancyChanged(occupied=True)`: IDLE→ACTIVE (emit `CancelTimer` if a
    release timer could be pending — cancel is idempotent), RELEASING→ACTIVE
    (cancel hold timer; **no volume effect** — the speaker never moved).
    Record `last_transition = now` only on phase changes that alter
    audibility (IDLE→ACTIVE yes, RELEASING→ACTIVE no).

1.2 `OccupancyChanged(occupied=False)` while ACTIVE → RELEASING +
    `StartTimer(zone_release(zone), hold_seconds × hold_scale)`. Volume
    unchanged. `hold_scale` comes from the zone's episode peak (rule 1.7):

    | episode peak | hold_scale |
    |---|---|
    | SETTLED | `hold_settled_scale` |
    | PASSING | `hold_passing_scale` |
    | anything else (ACTIVE, EMPTY, no activity input) | 1.0 |

    Rationale: a walk-through must not leave music lingering; stepping out
    of a room you were settled in must not fade it while you fetch
    something.

1.3 `TimerFired(zone_release(zone))` while RELEASING → IDLE, reconcile
    (fade-out). Stale release timers (zone no longer RELEASING) are ignored.

1.4 TV activity counts as occupancy: a zone with `tv_playing=True` behaves
    as occupied regardless of its sensors (drives ACTIVE the same way, and
    holds off RELEASING). When both TV and occupancy go inactive, the normal
    RELEASING/hold flow applies.

1.5 **Fallback zone**: after any state update, if no zone is audible and the
    fallback zone is IDLE (docked, enabled), force it ACTIVE (a forced-active
    fallback zone returns to IDLE via the normal occupancy rules the moment
    another zone becomes audible — implement as: fallback zone is audible iff
    `occupied ∨ tv_playing ∨ no other zone audible`; keep its phase in sync).
    Forcing additionally requires `anyone_home is not False` (rule 1.8).

1.6 Phase changes never emit volume effects directly — only reconcile() does.

1.7 **Activity** (rich presence input, optional): `ActivityChanged(zone,
    activity ∈ {EMPTY, PASSING, ACTIVE, SETTLED} | None)` updates
    `ZoneState.activity` — state only, never a phase or volume change
    (`None` = the estimator is blind: no information, not "empty").
    The engine tracks an **episode peak** per zone: the most severe
    activity (severity SETTLED > ACTIVE > PASSING > EMPTY; `None` carries
    no information) observed since the zone last became audible:
    - IDLE→ACTIVE starts a new episode: peak := current activity.
    - RELEASING→ACTIVE is the same episode: peak := max(peak, activity).
    - While ACTIVE, each `ActivityChanged` raises the peak.
    The peak selects the hold-time scale in rule 1.2. Zones without an
    activity input keep `activity = None` and behave exactly as before.

1.8 **Home presence** (optional): `HomePresenceChanged(present)` stores
    `EngineState.anyone_home` (state update even while disabled), then
    reconciles. While `anyone_home is False`, rule 1.5 fallback forcing is
    suspended: a *forced* fallback zone returns to IDLE (fade-out) even
    though no other zone is audible — an empty home should be silent.
    Zones audible on their own merits (occupied / TV) are unaffected.
    When presence returns (True or None) and nothing is audible, forcing
    resumes (fade-in): music greets whoever comes home. `None` (no input
    configured, estimator blind) behaves as present — fail-safe. In
    ALL_SPEAKERS follow mode, `anyone_home` additionally feeds effective
    occupancy itself (rule 1.9).

1.9 **Follow mode** (`follow_mode ∈ {PER_ZONE, PER_ROOM, ALL_SPEAKERS}`,
    default `PER_ZONE`, published state, seeded from the snapshot like any
    flag per 9.1). It widens **effective occupancy** — the presence that
    makes a zone ACTIVE. Define `self_present(z) = z.occupied ∨ z.tv_playing`
    (rule 1.4). Then:

    | mode | `effective_occupied(zone)` |
    |---|---|
    | PER_ZONE | `self_present(zone)` (legacy behavior) |
    | PER_ROOM | any zone in `zone.room` has `self_present` |
    | ALL_SPEAKERS | `anyone_home is not False`, or any zone has `self_present` |

    A STANDALONE zone is never made audible (its speaker left the system),
    but its own `self_present` still counts as a contributor — occupancy is a
    property of the room, not the speaker. Because a change in one zone can
    now flip its neighbors' effective occupancy, `OccupancyChanged` /
    `TvPlayingChanged` re-run the IDLE/ACTIVE/RELEASING transitions (rules
    1.1–1.4) for every zone in the *affected set* (the changed zone in
    PER_ZONE; its room in PER_ROOM; all zones in ALL_SPEAKERS), and in
    ALL_SPEAKERS a `HomePresenceChanged` re-runs them for all zones too.

    ALL_SPEAKERS means sound in every zone *while someone is home*, gated
    by home-level presence. Rule 1.8's input doubles as presence anywhere —
    someone home but outside every zone (bedroom, bathroom) keeps the whole
    house playing — and its fail-safe convention carries over: `None` (no
    input configured, estimator blind) behaves as present, so a blind
    estimator keeps the house on rather than silencing it. Only a
    definitive `anyone_home is False` empties the house: every zone
    releases through its normal hold and the empty-home rule (1.8) keeps
    the fallback unforced. Consequence: without a home-presence input the
    mode keeps every zone audible indefinitely — the whole-house analogue
    of the fallback's "music never dies"; configure an anyone-home sensor
    to get empty-home silence. Follow mode is orthogonal to TV solo (rule
    6.2): solo suppression applies *on top of* whatever effective occupancy
    makes audible, so a playing TV can still silence rooms even in
    ALL_SPEAKERS.

    `SetFollowMode(m)`: if changed, re-derive every zone from current inputs
    and reconcile (`rebalance_fade`). Widening fades newly-woken zones in;
    narrowing lets zones that lose their trigger release through the normal
    hold/fade-out path. While disabled: recompute phases only (8.1).

## 2. Dock / standalone

2.1 `DockChanged(docked=False)` → phase STANDALONE, cancel its release timer.
    **Emit no volume effects for this speaker** — the user takes it over at
    its current volume (legacy behavior). Other speakers reconcile (the
    zone's departure may change room scale).

2.2 `DockChanged(docked=True)` → leave STANDALONE; recompute phase from
    current `occupied/tv_playing` as if the inputs just changed (fade-in when
    it lands ACTIVE; landing ACTIVE starts a new activity episode per rule
    1.7 — peak := current activity, never a stale pre-undock peak). If
    `keep_grouped`, schedule group repair (rule 7).

2.3 STANDALONE speakers are invisible to: master fan-out, reverse sync, duck
    caps, mute fan-out, group repair expectations, and room-scale counting.

## 3. Master volume & night mode

3.1 `SetMaster(v)`: clamp to [0,1], store, reconcile with `master_fade`.
    While muted: store only — reconcile happens on unmute.

3.2 Master changes (forward or reverse) do not touch `last_transition`.

3.3 `SetNightMode(active)`: store `night_mode` (published state, seeded from
    the snapshot per 9.1). If it changed: stamp the mode-change timestamp
    (rule 4.1) and reconcile with `rebalance_fade` — engaging caps every
    audible speaker at `night_volume_cap` (section 0), disengaging restores
    the exact pre-night targets. `master` itself is never modified by night
    mode. While disabled: store only (8.1); the 8.2 enable reconcile applies
    the cap.

3.4 **Idle attenuation** (`idle_attenuation ∈ {GENTLE, BALANCED, MAX}`,
    default `MAX`, published state, seeded from the snapshot per 9.1):
    how much of its would-be level a non-audible zone keeps as a background
    **bed** instead of hard silence (`zone_level`, section 0). `MAX` is
    full attenuation — idle zones are silent, the pre-3.4 behavior. The bed
    rides `desired()`'s single funnel, so everything composes unchanged:
    the bed is the preset fraction of the fully capped active target (so
    duck and night compression preserve its relative attenuation — see
    section 0), TV-solo suppression (6.2) and a
    definitively empty home (1.8) keep such zones hard-silent, STANDALONE
    speakers are never touched (2.3), and beds weigh into `room_scale` by
    their level so the bed never raises a room's total loudness. Bed zones
    remain *not audible*: they do not reverse-sync (4.1), do not retire
    fallback forcing (1.5), and their FSMs run normally — a zone fade-out
    simply lands on the bed instead of zero.
    `SetIdleAttenuation(m)`: if changed, stamp the mode-change timestamp
    (rule 4.1) and reconcile (`rebalance_fade`). While disabled: store only
    (8.1); the 8.2 enable reconcile applies the bed.

## 4. External volume reports (reverse sync)

4.1 `ExternalVolume(speaker, v)`: update `speakers[s].volume = v` always.
    Then *consider* sync; the report is **discarded** (state updated, no
    sync) if any of: engine disabled; speaker STANDALONE (its volume is its
    own business); zone not audible; global mute on; any duck input active;
    night mode on (capped volumes imply nothing about the master — but see
    rule 4.5); `now - max(any zone's last_transition, duck/tv/night-mode
    change) < transition_suppression`; or `v ≤ 0.01` (hard-zero guard).

4.2 Accepted reports debounce, they do not apply immediately: store
    `pending_external = v`, `StartTimer(external_debounce(speaker),
    external_debounce)` (restarting the timer on each new report).

4.3 `TimerFired(external_debounce(speaker))`: take `pending_external` (ignore
    if cleared), re-check rule 4.1 conditions at *fire* time, compute
    `implied_master(v, trim, current room_scale)`. If
    `|implied - master| > sync_threshold`: set master, set the reporting
    speaker's `commanded = v` (it is already at its target — no ramp for it),
    reconcile others with `rebalance_fade`.

4.4 Any reconciliation that writes to a speaker clears its
    `pending_external` and cancels its debounce timer (our write supersedes
    the stale external report).

4.5 Night pull-back: while night mode is on, a report with
    `v > night_volume_cap` from a non-STANDALONE speaker of a zone with
    `zone_level > 0` (audible, or playing an idle bed per rule 3.4)
    is not debounced (4.1 already bars it from sync). Instead the engine
    adopts it as the speaker's commanded volume and reconciles
    (`rebalance_fade`), ramping the reporting speaker back down to its
    desired volume (at most the cap). `master` is never touched. The
    adapter's echo ledger swallows the corrective ramp's own state reports,
    so the correction cannot re-trigger itself. Reports at or below the cap
    follow 4.1 unchanged (discarded while night mode is on).

## 5. Mute

5.1 `SetMute(True)`: emit `SetSpeakerMute(True)` for every non-STANDALONE
    speaker; master and volumes untouched (mute is orthogonal to volume, so
    unmute needs no saved value).

5.2 `SetMute(False)`: `SetSpeakerMute(False)` fan-out, then reconcile
    (`rebalance_fade`) in case master changed while muted.

5.3 `ExternalMute(speaker, m)` from a non-STANDALONE speaker ≠ global mute →
    treat as `SetMute(m)` (source=speaker). The adapter's echo ledger
    guarantees this was a human.

5.4 While muted, rules 4.x discard external volume reports.

## 6. TV mode & solo

6.1 `TvPlayingChanged` updates the zone; TV playing forces that room's scale
    to 1.0 (via room_scale) and counts as occupancy (rule 1.4).

6.2 `tv_solo_mode ∈ {OFF, SAME_ROOM, TV_ZONE}` selects the set of
    **solo-suppressed** zones. A solo-suppressed zone is not audible
    (desired = 0) regardless of phase. While no zone has `tv_playing`, the
    set is empty in every mode. While at least one zone has `tv_playing`:

    | mode | solo-suppressed zones |
    |---|---|
    | OFF | none |
    | SAME_ROOM | every zone whose room contains no zone with `tv_playing` |
    | TV_ZONE | every zone that does not itself have `tv_playing` |

    Suppressed zones' FSMs keep running normally so that when the TV stops
    (or the mode changes) a single reconcile restores them. Suppression
    counts as a transition for rule 4.1 (set a mode-change timestamp when
    the suppression set changes — including changes caused by switching
    between modes while a TV plays).

6.3 `SetTvSoloMode(m)`: store + reconcile (`rebalance_fade`).

## 7. Group repair

7.1 Expected topology: all non-STANDALONE speakers in one group led by
    `config.leader_id()`. (Leadership itself is not enforced — only
    membership: every expected speaker in the same group as the leader.)

7.2 On `GroupMembersReported` / `DockChanged` / `SetKeepGrouped(True)`:
    if `keep_grouped` and the observed topology deviates from expected →
    `StartTimer(GROUP_REPAIR, group_repair_delay)` (restart if pending).
    If it matches → `CancelTimer(GROUP_REPAIR)`.

7.3 `TimerFired(GROUP_REPAIR)`: re-evaluate; if still deviating, emit
    `JoinGroup(leader, missing_members)` once. The adapter's post-command
    cooldown prevents the resulting `GroupMembersReported` echoes from
    re-triggering repair loops; if the join fails, the next report restarts
    the cycle (natural retry with backoff = repair delay).

7.4 `keep_grouped=False` cancels any pending repair.

## 8. Enable / disable

8.1 `SetEnabled(False)`: cancel **all** timers, emit nothing else. Events
    keep updating state while disabled (the world model stays fresh), but
    reconcile is inert and rules 4–7 emit nothing.

8.2 `SetEnabled(True)`: recompute all zone phases from current inputs, then
    reconcile everything with `rebalance_fade`; re-arm group repair check.

## 9. Startup (`start(now)`)

9.1 Seed all state from `InitialSnapshot`. Dockable speakers undocked in the
    snapshot start STANDALONE. Zone phases derive from occupancy/tv inputs
    (no hold timers pending — an unoccupied zone starts IDLE, not RELEASING).
    Activity and `anyone_home` seed like any flag; a zone seeded audible
    starts its episode at the seeded activity (rule 1.7), and fallback
    forcing respects rule 1.8 at seed time. `follow_mode` seeds like any flag;
    effective occupancy (1.9) is resolved across all zones, so a zone can seed
    ACTIVE because a room-mate (PER_ROOM) or any zone (ALL_SPEAKERS) is
    occupied.

9.2 Master: use `snapshot.master` if given; else the **median** of
    `implied_master(volume, trim, room_scale)` over audible zones with a
    known volume; else keep the model default.

9.3 Adopt: for each speaker, if `|volume - desired| ≤ startup_tolerance`,
    set `commanded = volume` (no effect). Otherwise reconcile that speaker
    with `rebalance_fade`. Never emit anything for STANDALONE speakers.

9.4 Evaluate group repair (rule 7.2) once.

## 10. Miscellany

10.1 `SetTrim(speaker, t)`: update config-shadow trim (engine keeps a
     mutable trim map seeded from config), reconcile (`rebalance_fade`).

10.2 Unknown/stale `TimerFired` ids are ignored silently.

10.3 `PlaybackChanged` only updates state (diagnostics; no effects).

10.4 The engine must be robust to events referencing unknown ids: ignore
     with no exception (the adapter logs).

10.5 Effect ordering within one `handle()` call: `CancelTimer`s first, then
     mute/volume effects, then `StartTimer`s, then `JoinGroup`. Determinism:
     speaker effects ordered by config declaration order.

## Race regression scenarios (must-have tests)

- R1 Fade-out fires (hold expiry) while a master-change ramp is mid-flight →
  single final `RampVolume(0)`; commanded state consistent.
- R2 Occupancy flickers off→on within hold → zero volume effects.
- R3 Door opens during a fade-in → duck cap applies immediately; door closes
  → exact pre-duck targets restored.
- R4 External volume report arrives during suppression window after a zone
  transition → no master change.
- R5 Debounce: 5 external reports in 1 s → exactly one master update (final
  value), one rebalance.
- R6 Undock mid-fade → no further effects for that speaker; redock while
  occupied → fade-in to correct target with current room scale.
- R7 Two zones in one room activate near-simultaneously → both end at
  `master × trim / √2`; deactivating one rebalances the other up.
- R8 Group dissolves spontaneously → exactly one `JoinGroup` after the
  repair delay; dissolve caused by our own `JoinGroup` echo does not loop.
- R9 Mute on → master slider moved → unmute → volumes match the new master.
- R10 TV starts (tv_solo_mode SAME_ROOM) → kitchen suppressed; walking into
  the kitchen while the TV plays keeps the kitchen silent; TV stops →
  kitchen fades in (it is occupied).
- R11 Night mode on → knob turned above the cap → exactly one corrective
  ramp back to the cap (no debounce timer), master untouched; a repeat
  report of the cap value itself is discarded — no ping-pong.
- R12 Follow mode ALL_SPEAKERS + tv_solo TV_ZONE: someone enters the kitchen
  while the TV plays → every zone's FSM is ACTIVE, yet only the TV zone is
  audible (solo suppression overrides the follow mode). PER_ROOM: occupancy
  in one room wakes its room-mates but never the other room.
- R13 Idle attenuation GENTLE: the occupied zone plays full, its room-mate
  plays the bed, and the pair's total room power equals exactly one full
  zone; a TV solo still hard-silences bed zones; at night the bed plays
  half the *capped* active level (0.075 for a 0.15 cap, at any master) and
  a report above it from a bed speaker gets the 4.5 pull-back.
