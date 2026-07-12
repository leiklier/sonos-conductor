"""Group repair (spec rule 7): keep the speakers in one Sonos group.

Observes reported group topology, schedules the repair timer when expected
members are missing (7.2), and emits a single ``JoinGroup`` when it fires
(7.3). STANDALONE speakers are expected to be absent (7.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import timers
from .events import GroupMembersReported, SetKeepGrouped

if TYPE_CHECKING:
    from .engine import ConductorEngine
    from .plan import Plan


def on_group_members(engine: ConductorEngine, event: GroupMembersReported, plan: Plan) -> None:
    speaker_state = engine.state.speakers.get(event.speaker_id)
    if speaker_state is None:  # 10.4
        return
    speaker_state.group_members = tuple(event.members)
    evaluate_group_repair(engine, plan)  # 7.2 (self-gates)


def on_set_keep_grouped(engine: ConductorEngine, event: SetKeepGrouped, plan: Plan) -> None:
    engine.state.keep_grouped = event.enabled
    if not engine.state.enabled:
        return
    if event.enabled:
        evaluate_group_repair(engine, plan)  # 7.2
    else:
        plan.cancel_timer(timers.GROUP_REPAIR)  # 7.4


def evaluate_group_repair(engine: ConductorEngine, plan: Plan) -> None:
    """Rule 7.2: schedule or cancel the repair timer from observed topology."""
    if not engine.state.enabled or not engine.state.keep_grouped:
        return
    if group_missing(engine):
        plan.start_timer(timers.GROUP_REPAIR, engine.config.tunables.group_repair_delay)
    else:
        plan.cancel_timer(timers.GROUP_REPAIR)


def on_repair_fired(engine: ConductorEngine, plan: Plan) -> None:
    if not engine.state.enabled or not engine.state.keep_grouped:
        return
    missing = group_missing(engine)
    if missing:
        plan.join(engine.config.leader_id(), missing)  # 7.3, once


def group_missing(engine: ConductorEngine) -> tuple[str, ...] | None:
    """Expected members missing from the leader's observed group.

    Returns ``None`` when repair is not applicable: the leader is
    STANDALONE (skip entirely) or the topology is unknown (no report
    mentions the leader). Only membership matters, not who leads (7.1).
    """
    leader = engine.config.leader_id()
    if leader not in engine.state.speakers or engine._is_standalone_speaker(leader):
        return None
    observed: frozenset[str] | None = None
    leader_members = engine.state.speakers[leader].group_members
    if leader_members:
        observed = frozenset(leader_members) | {leader}
    else:
        for speaker in engine.config.speakers:
            members = engine.state.speakers[speaker.speaker_id].group_members
            if leader in members:
                observed = frozenset(members) | {speaker.speaker_id}
                break
    if observed is None:
        return None
    return tuple(
        speaker.speaker_id
        for speaker in engine.config.speakers
        if speaker.speaker_id != leader
        and not engine._is_standalone_speaker(speaker.speaker_id)
        and speaker.speaker_id not in observed
    )
