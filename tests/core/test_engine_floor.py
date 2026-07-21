"""The volume floor (spec section 0): silence is 0.01, never a true zero.

Sonos turns its status LED green while a speaker sits at volume 0, so
every silent target parks at ``VOLUME_FLOOR`` instead.
"""

from __future__ import annotations

from custom_components.sonos_conductor.core import reconcile
from custom_components.sonos_conductor.core.events import ExternalVolume, SetMaster
from custom_components.sonos_conductor.core.model import IdleAttenuation

from .harness import (
    FLOOR,
    KJOKKEN,
    SOFAKROK,
    SPISEBORD,
    Harness,
    expect_ramp,
    make_snapshot,
)


class TestVolumeFloor:
    def test_desired_is_never_zero_for_managed_grouped_speakers(self) -> None:
        h = Harness()  # quiet house: kjokken/spisebord silent, fallback forced
        for speaker in (KJOKKEN, SPISEBORD):
            assert reconcile.desired(h.engine, speaker) == FLOOR

    def test_fade_out_lands_on_the_floor(self) -> None:
        h = Harness()
        h.occupy("kjokken", at=0.0)
        effects = h.release("kjokken", at=10.0)
        ramp = expect_ramp(effects, KJOKKEN, FLOOR, duration=5.0)
        assert ramp.target > 0.0

    def test_bed_below_the_floor_is_floored(self) -> None:
        # Gentle bed at a whisper master: 0.5 x (0.01 x 1.2) = 0.006 < floor.
        h = Harness(snapshot=make_snapshot(idle_attenuation=IdleAttenuation.GENTLE))
        h.fire(SetMaster(0.01), at=0.0)
        assert reconcile.desired(h.engine, KJOKKEN) == FLOOR

    def test_floored_speaker_report_never_reverse_syncs(self) -> None:
        # A speaker parked at the floor reports 0.01 back; the hard-zero
        # guard (= the floor) must discard it, whatever the master implies.
        h = Harness()
        h.occupy("kjokken", at=0.0)
        assert h.fire(ExternalVolume(SOFAKROK, FLOOR), at=20.0) == []
