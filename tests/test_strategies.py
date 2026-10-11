"""Unit tests for Three Strategy Architecture and Section 61 regression test."""
from datetime import timedelta
from types import SimpleNamespace

from data_sources.snapshot import MarketSnapshot
from engines.candles import Candle
from engines.levels_lifecycle import LevelLifecycleManager
from engines.strategies import S1TrendExpansion, S2VwapMomentum, S3LiquidityReversal
from utils import ist_now


def test_all_strategies_evaluate_same_snapshot():
    """Section 61 Acceptance Test: All strategies evaluate the exact same
    snapshot instance without independent state mutations."""
    now = ist_now()
    c = Candle(now, 22400, 22450, 22390, 22440, volume=1000)
    snap = MarketSnapshot(
        snapshot_id="SNAP-CONCURRENT-001",
        timestamp=now,
        spot=22440.0,
        candles=(c,),
        vwap=22420.0,
        ema20=22410.0,
        atr=25.0,
        pdh=22500.0,
        pdl=22300.0,
        eq=22400.0,
        orb={"high": 22430.0, "low": 22380.0},
    )

    lvl_mgr = LevelLifecycleManager()
    lvl_mgr.update(snap)

    s1 = S1TrendExpansion()
    s2 = S2VwapMomentum()
    s3 = S3LiquidityReversal()

    a1 = s1.assess(snap, lvl_mgr, market_state="TREND-UP")
    a2 = s2.assess(snap, lvl_mgr, market_state="TREND-UP")
    a3 = s3.assess(snap, lvl_mgr, market_state="TREND-UP")

    assert a1.snapshot_id == snap.snapshot_id
    assert a2.snapshot_id == snap.snapshot_id
    assert a3.snapshot_id == snap.snapshot_id
    assert a1.strategy_id == "S1_TREND_EXPANSION"
    assert a2.strategy_id == "S2_VWAP_MOMENTUM"
    assert a3.strategy_id == "S3_LIQUIDITY_REVERSAL"


def test_trend_override_vetoes_s3_reversal():
    """Section 16 Acceptance Test: In an active trend expansion, a counter-trend
    S3 reversal setup is VETOED, even if a pool was swept."""
    now = ist_now()
    # High penetrates PDH 22500 and closes inside at 22495 (classic sweep signature)
    c = Candle(now, 22480, 22520, 22470, 22495)
    snap = MarketSnapshot(
        snapshot_id="SNAP-TREND-002",
        timestamp=now,
        spot=22495.0,
        candles=(c,),
        vwap=22450.0,
        ema20=22440.0,
        atr=25.0,
        pdh=22500.0,
        pdl=22300.0,
    )
    lvl_mgr = LevelLifecycleManager()

    s3 = S3LiquidityReversal()

    # Case A: Market is in EXPANSION or S1 trend strength is HIGH -> S3 VETOED!
    a_veto = s3.assess(snap, lvl_mgr, market_state="EXPANSION", s1_trend_strength="HIGH")
    assert a_veto.eligible is False
    assert any("COUNTER_TREND_EXPANSION_VETO" in b for b in a_veto.blocker_codes)

    # Case B: Market is in RANGE state -> S3 is eligible to fade!
    a_allowed = s3.assess(snap, lvl_mgr, market_state="RANGE", s1_trend_strength="LOW")
    assert a_allowed.eligible is True
    assert a_allowed.direction == "SHORT"
    assert a_allowed.setup_score >= 80
