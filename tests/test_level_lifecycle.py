"""Unit tests for LevelLifecycleManager and Section 58 regression test."""
from datetime import timedelta
from types import SimpleNamespace

from engines.candles import Candle
from engines.levels_lifecycle import (
    LevelLifecycleManager,
    LevelRole,
    LevelStatus,
)
from utils import ist_now


def test_consumed_level_cannot_retrigger():
    """Section 58 Acceptance Test: A level tested >= 3 times becomes CONSUMED
    and CANNOT trigger repeated fade/reversal setups."""
    now = ist_now()
    mgr = LevelLifecycleManager(tolerance_pts=5.0, max_tests_before_consumed=3)
    lvl = mgr.register_level("R1", 22500.0, "SWING-H", LevelRole.RESISTANCE, now)

    assert lvl.status == LevelStatus.ACTIVE
    assert lvl.can_trigger_reversal is True

    # Test 1: Touch within tolerance and close held below 22500
    c1 = Candle(now, 22480, 22502, 22475, 22490)
    mgr.update(SimpleNamespace(candles=[c1], timestamp=now, pdh=None, pdl=None, eq=None, orb=None, spot=22490), c1)
    assert lvl.status == LevelStatus.TESTED
    assert lvl.test_count == 1
    assert lvl.can_trigger_reversal is True

    # Test 2: Touch again and hold
    c2 = Candle(now + timedelta(minutes=5), 22490, 22504, 22485, 22492)
    mgr.update(SimpleNamespace(candles=[c2], timestamp=now, pdh=None, pdl=None, eq=None, orb=None, spot=22492), c2)
    assert lvl.status == LevelStatus.TESTED
    assert lvl.test_count == 2
    assert lvl.can_trigger_reversal is True

    # Test 3: Touch third time and hold -> Becomes CONSUMED!
    c3 = Candle(now + timedelta(minutes=10), 22492, 22501, 22480, 22488)
    mgr.update(SimpleNamespace(candles=[c3], timestamp=now, pdh=None, pdl=None, eq=None, orb=None, spot=22488), c3)
    assert lvl.status == LevelStatus.CONSUMED
    assert lvl.test_count == 3
    assert lvl.is_consumed is True
    assert lvl.can_trigger_reversal is False, "Consumed level must NOT be allowed to trigger reversals!"


def test_level_break_accept_and_flip():
    """Section 33 Acceptance Test: Resistance breaks, gets accepted, and flips to Support."""
    now = ist_now()
    mgr = LevelLifecycleManager(tolerance_pts=5.0)
    lvl = mgr.register_level("PDH", 22500.0, "PDH", LevelRole.RESISTANCE, now)

    # Candle breaks above resistance
    c1 = Candle(now, 22490, 22520, 22485, 22515)
    mgr.update(SimpleNamespace(candles=[c1], timestamp=now, pdh=22500, pdl=None, eq=None, orb=None, spot=22515), c1)
    assert lvl.status == LevelStatus.BROKEN

    # Second candle closes above resistance -> ACCEPTED!
    c2 = Candle(now + timedelta(minutes=5), 22515, 22540, 22510, 22535)
    mgr.update(SimpleNamespace(candles=[c2], timestamp=now, pdh=22500, pdl=None, eq=None, orb=None, spot=22535), c2)
    assert lvl.status == LevelStatus.ACCEPTED

    # Retest from above: low dips near 22500, but candle closes above 22500 -> FLIPPED to SUPPORT!
    c3 = Candle(now + timedelta(minutes=10), 22530, 22535, 22502, 22518)
    mgr.update(SimpleNamespace(candles=[c3], timestamp=now, pdh=22500, pdl=None, eq=None, orb=None, spot=22518), c3)
    assert lvl.status == LevelStatus.FLIPPED
    assert lvl.role == LevelRole.SUPPORT, "Former resistance must flip to support!"
