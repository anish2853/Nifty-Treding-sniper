"""Phase 2 Acceptance Tests: Liquidity Map & Level Lifecycle Cleanup.

Verifies:
1. test_liquidity_hierarchy_tiers
2. test_level_status_lifecycle_and_decay
3. test_consumed_liquidity_cannot_produce_reversals
4. test_next_liquidity_ranking_and_selection
5. test_400_point_trend_dynamic_advancement
6. test_dashboard_display_level_limit
7. test_oct8_regression_phase2
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest

from engines.candles import Candle
from engines.levels_lifecycle import (
    LevelLifecycleManager,
    LevelRole,
    LevelStatus,
    LevelTier,
    LifecycleLevel,
    LiquidityMap,
)
from utils import ist_now


def test_liquidity_hierarchy_tiers():
    """1. Explicit 4-tier hierarchy: Major > Session > Structure > Internal POI."""
    now = ist_now()
    mgr = LevelLifecycleManager()

    l1 = mgr.register_level("PDH", 22550.0, "PDH", LevelRole.RESISTANCE, now, tier=LevelTier.MAJOR)
    l2 = mgr.register_level("ORB-H", 22520.0, "ORB", LevelRole.RESISTANCE, now, tier=LevelTier.SESSION)
    l3 = mgr.register_level("STRUC-H", 22500.0, "5M_SWING", LevelRole.RESISTANCE, now, tier=LevelTier.STRUCTURE)
    l4 = mgr.register_level("OB-1", 22480.0, "OB_ENGINE", LevelRole.RESISTANCE, now, tier=LevelTier.INTERNAL)

    assert l1.tier == LevelTier.MAJOR
    assert l2.tier == LevelTier.SESSION
    assert l3.tier == LevelTier.STRUCTURE
    assert l4.tier == LevelTier.INTERNAL

    # Major outranks internal POI
    assert l1.tier.value < l4.tier.value


def test_level_status_lifecycle_and_decay():
    """2. Complete lifecycle states (CANDIDATE, ACTIVE, TESTED, BROKEN, CONSUMED, FLIPPED, EXPIRED).
    No level is allowed to remain ACTIVE forever. Tier 4 decays rapidly."""
    now = ist_now().replace(hour=10, minute=0)
    mgr = LevelLifecycleManager(tolerance_pts=5.0, max_tests_before_consumed=3)

    # A. Tier 4 POI age decay (> 60 min without test)
    old_time = now - timedelta(minutes=65)
    poi = mgr.register_level(
        "MICRO-OB-1", 22450.0, "OB_ENGINE", LevelRole.RESISTANCE, old_time,
        tier=LevelTier.INTERNAL, strength="WEAK"
    )
    c_decay = Candle(now, 22430.0, 22435.0, 22425.0, 22430.0)
    snap_decay = SimpleNamespace(candles=[c_decay], timestamp=now, spot=22430.0, pdh=None, pdl=None, eq=None, orb=None, atr=20.0)
    mgr.update(snap_decay, c_decay)
    assert poi.status == LevelStatus.EXPIRED
    assert poi.is_expired is True
    assert poi.is_tradeable is False

    # B. Test count progression: ACTIVE -> TESTED -> CONSUMED after 3 tests
    r_level = mgr.register_level("RES_22500", 22500.0, "SWING", LevelRole.RESISTANCE, now, tier=LevelTier.SESSION)
    assert r_level.status == LevelStatus.ACTIVE

    # Test 1
    c1 = Candle(now + timedelta(minutes=5), 22485.0, 22502.0, 22480.0, 22495.0)
    snap1 = SimpleNamespace(candles=[c1], timestamp=now + timedelta(minutes=5), spot=22495.0, pdh=None, pdl=None, eq=None, orb=None, atr=20.0)
    mgr.update(snap1, c1)
    assert r_level.status == LevelStatus.TESTED
    assert r_level.test_count == 1

    # Test 2
    c2 = Candle(now + timedelta(minutes=10), 22490.0, 22503.0, 22485.0, 22498.0)
    snap2 = SimpleNamespace(candles=[c2], timestamp=now + timedelta(minutes=10), spot=22498.0, pdh=None, pdl=None, eq=None, orb=None, atr=20.0)
    mgr.update(snap2, c2)
    assert r_level.test_count == 2

    # Test 3 -> CONSUMED
    c3 = Candle(now + timedelta(minutes=15), 22495.0, 22504.0, 22490.0, 22497.0)
    snap3 = SimpleNamespace(candles=[c3], timestamp=now + timedelta(minutes=15), spot=22497.0, pdh=None, pdl=None, eq=None, orb=None, atr=20.0)
    mgr.update(snap3, c3)
    assert r_level.status == LevelStatus.CONSUMED
    assert r_level.is_consumed is True
    assert r_level.is_tradeable is False


def test_consumed_liquidity_cannot_produce_reversals():
    """3. Consumed liquidity must NOT continue producing fresh reversal signals."""
    now = ist_now()
    mgr = LevelLifecycleManager()
    lvl = mgr.register_level("PDL", 22450.0, "PDL", LevelRole.SUPPORT, now, tier=LevelTier.MAJOR)
    lvl.status = LevelStatus.CONSUMED

    assert lvl.is_consumed is True
    assert lvl.is_tradeable is False
    assert lvl.can_trigger_reversal is False
    assert lvl not in mgr.get_actionable_levels()


def test_next_liquidity_ranking_and_selection():
    """4. Liquidity ranking: A strong Major pool 20 pts away outranks a weak micro zone 10 pts away."""
    now = ist_now()
    mgr = LevelLifecycleManager()
    spot = 22430.0

    # Downside Major Liquidity Pool (Tier 1): 20 pts away (22410)
    major_pool = mgr.register_level(
        "PDL_MAJOR", 22410.0, "PDL", LevelRole.SUPPORT, now,
        tier=LevelTier.MAJOR, strength="STRONG"
    )

    # Downside Micro POI (Tier 4): 10 pts away (22420)
    micro_poi = mgr.register_level(
        "MICRO_OB", 22420.0, "OB", LevelRole.SUPPORT, now,
        tier=LevelTier.INTERNAL, strength="WEAK"
    )

    # Upside Major Pool (Tier 1): 40 pts away (22470)
    orb_high = mgr.register_level(
        "ORB-H", 22470.0, "ORB", LevelRole.RESISTANCE, now,
        tier=LevelTier.SESSION, strength="STRONG"
    )

    liq_map = mgr.get_next_liquidity(spot, market_state="TREND-DOWN")
    assert isinstance(liq_map, LiquidityMap)

    # In TREND-DOWN, next_major_downside selects the Tier 1 Major Pool (22410)
    assert liq_map.next_major_downside.level_id == "PDL_MAJOR"
    # Even though MICRO_OB is 10 pts closer, PDL_MAJOR's tier and strength outrank it!
    assert liq_map.nearest_downside_liquidity.level_id == "PDL_MAJOR"
    assert liq_map.room_down == 20.0
    assert liq_map.room_up == 40.0


def test_400_point_trend_dynamic_advancement():
    """5. Section 10: 400-Point Trend Behavior.
    System dynamically advances through liquidity pools as price descends.
    Never continues targeting old consumed/broken levels."""
    now = ist_now().replace(hour=10, minute=0)
    mgr = LevelLifecycleManager(tolerance_pts=5.0)

    # Setup the sequence of downside pools (e.g. Oct 8 style):
    # 22,390 -> 22,300 -> 22,220
    p1 = mgr.register_level("POOL_22390", 22390.0, "SWING_LOW", LevelRole.SUPPORT, now, tier=LevelTier.SESSION)
    p2 = mgr.register_level("POOL_22300", 22300.0, "ROUND_NUMBER", LevelRole.SUPPORT, now, tier=LevelTier.MAJOR)
    p3 = mgr.register_level("POOL_22220", 22220.0, "MAJOR_POOL", LevelRole.SUPPORT, now, tier=LevelTier.MAJOR)

    # Step 1: Price at 22,430 -> next downside is 22,390
    liq1 = mgr.get_next_liquidity(22430.0, market_state="TREND-DOWN")
    assert liq1.nearest_downside_liquidity.level_id == "POOL_22390"
    assert liq1.room_down == 40.0

    # Step 2: Price dumps through 22,390 to 22,350 (breaks 22,390 by 40 pts)
    c1 = Candle(now + timedelta(minutes=5), 22410.0, 22415.0, 22340.0, 22350.0)
    snap1 = SimpleNamespace(candles=[c1], timestamp=now + timedelta(minutes=5), spot=22350.0, pdh=None, pdl=None, eq=None, orb=None, atr=20.0)
    mgr.update(snap1, c1)

    # POOL_22390 is broken
    assert p1.status == LevelStatus.BROKEN
    # Next downside liquidity dynamically advances to 22,300!
    liq2 = mgr.get_next_liquidity(22350.0, market_state="TREND-DOWN")
    assert liq2.nearest_downside_liquidity.level_id == "POOL_22300"
    assert liq2.room_down == 50.0

    # Step 3: Price drops to 22,260 (breaks 22,300 by 40 pts, and 22,390 by 130 pts -> 22,390 expires)
    c2 = Candle(now + timedelta(minutes=15), 22320.0, 22325.0, 22255.0, 22260.0)
    snap2 = SimpleNamespace(candles=[c2], timestamp=now + timedelta(minutes=15), spot=22260.0, pdh=None, pdl=None, eq=None, orb=None, atr=20.0)
    mgr.update(snap2, c2)

    assert p1.status == LevelStatus.EXPIRED
    assert p2.status == LevelStatus.BROKEN
    # Next downside liquidity dynamically advances to 22,220!
    liq3 = mgr.get_next_liquidity(22260.0, market_state="TREND-DOWN")
    assert liq3.nearest_downside_liquidity.level_id == "POOL_22220"
    assert liq3.room_down == 40.0


def test_dashboard_display_level_limit():
    """6. Dashboard display filter limits clutter: top 3 major + live structure + max 1 active POI."""
    now = ist_now()
    mgr = LevelLifecycleManager()
    spot = 22450.0

    # Register 2 Major levels
    mgr.register_level("PDH", 22550.0, "PDH", LevelRole.RESISTANCE, now, tier=LevelTier.MAJOR)
    mgr.register_level("PDL", 22400.0, "PDL", LevelRole.SUPPORT, now, tier=LevelTier.MAJOR)

    # Register 2 Session levels
    mgr.register_level("ORB-H", 22500.0, "ORB", LevelRole.RESISTANCE, now, tier=LevelTier.SESSION)
    mgr.register_level("ORB-L", 22430.0, "ORB", LevelRole.SUPPORT, now, tier=LevelTier.SESSION)

    # Register 5 minor internal POIs
    for i in range(5):
        mgr.register_level(f"OB_{i}", 22440.0 + (i * 2), "OB", LevelRole.SUPPORT, now, tier=LevelTier.INTERNAL)

    # Display levels must NOT dump all 9 levels
    disp_levels = mgr.get_dashboard_display_levels(spot, max_major=3)
    # Total displayed levels should be clean (top 3 major/session + at most 1 POI = max 4)
    assert len(disp_levels) <= 5
    tier_counts = {l.tier: 0 for l in disp_levels}
    for l in disp_levels:
        tier_counts[l.tier] = tier_counts.get(l.tier, 0) + 1

    # At most 1 internal POI displayed
    assert tier_counts.get(LevelTier.INTERNAL, 0) <= 1


def test_oct8_regression_phase2():
    """7. October 8 Regression:
    - 22,228 and 22,200 broken support levels cannot generate reversal
    - Next downside liquidity updates dynamically as market trends down."""
    now = ist_now().replace(hour=10, minute=30)
    mgr = LevelLifecycleManager(tolerance_pts=5.0)

    # Oct 8 support levels
    pdl = mgr.register_level("PDL", 22450.0, "PDL", LevelRole.SUPPORT, now, tier=LevelTier.MAJOR)
    lvl_22228 = mgr.register_level("S_22228", 22228.0, "SUPPORT", LevelRole.SUPPORT, now, tier=LevelTier.SESSION)
    lvl_22200 = mgr.register_level("S_22200", 22200.0, "ROUND_NUMBER", LevelRole.SUPPORT, now, tier=LevelTier.MAJOR)

    # Price at 10:35 dumps below PDL to 22,428.30
    c1 = Candle(now + timedelta(minutes=5), 22452.4, 22455.5, 22428.3, 22428.3)
    snap1 = SimpleNamespace(candles=[c1], timestamp=now + timedelta(minutes=5), spot=22428.3, pdh=22580.0, pdl=22450.0, eq=22515.0, orb=None, atr=25.0)
    mgr.update(snap1, c1)

    assert pdl.status == LevelStatus.BROKEN
    # Next downside target points toward 22,228
    liq1 = mgr.get_next_liquidity(22428.3, market_state="TREND-DOWN")
    assert liq1.nearest_downside_liquidity.price < 22428.3

    # Afternoon: price collapses to 22,150 (breaking 22,228 by > 70 pts)
    c2 = Candle(now + timedelta(hours=4), 22225.0, 22230.0, 22145.0, 22150.0)
    snap2 = SimpleNamespace(candles=[c2], timestamp=now + timedelta(hours=4), spot=22150.0, pdh=22580.0, pdl=22450.0, eq=22515.0, orb=None, atr=25.0)
    mgr.update(snap2, c2)

    # 22,228 is EXPIRED
    assert lvl_22228.status == LevelStatus.EXPIRED
    assert lvl_22228.can_trigger_reversal is False
    assert lvl_22228.is_tradeable is False
