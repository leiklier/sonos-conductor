"""Pure domain core for Sonos Conductor.

Nothing in this package may import from ``homeassistant``. The outer
integration layer translates Home Assistant state changes into
:mod:`.events`, feeds them to :class:`.engine.ConductorEngine`, and executes
the returned :mod:`.effects`. This boundary is enforced by
``tests/core/test_purity.py``.
"""
