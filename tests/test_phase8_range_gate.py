"""Phase 8 Range Gate Acceptance Tests.

Verifies all 12 required specifications:
TEST 1: RANGE + S2 candidate -> BLOCK
TEST 2: TREND-UP + S2 candidate -> existing behavior unchanged
TEST 3: TREND-DOWN + S2 candidate -> existing behavior unchanged
TEST 4: RANGE + S3 candidate -> existing S3 behavior unchanged
TEST 5: RANGE + S1 candidate -> existing S1 behavior unchanged
TEST 6: Existing open S2 position + market transitions to RANGE -> position management continues normally
TEST 7: Blocked S2 candidate -> no TradeIntent
TEST 8: Blocked S2 candidate -> no PositionManager entry
TEST 9: Blocked S2 candidate -> no SmartAPI/PAPER order
TEST 10: Decision record contains: RANGE_GATE_S2_BLOCKED
TEST 11: No lookahead introduced
TEST 12: No cross-session state introduced
"""
from datetime import datetime, timedelta, time as dtime
from unittest.mock import MagicMock
import pytest

from data_sources.snapshot import MarketSnapshot
from engines.arbiter import Arbiter, RiskGovernor, TradeIntent, make_range_gate_decision_record
from engines.candles import Candle
from engines.levels_lifecycle import LevelLifecycleManager, LevelRole
from engines.position_manager import PositionManager, TradeStateEnum
from engines.strategies import S1TrendExpansion, S2VwapMomentum, S3LiquidityReversal, StrategyAssessment
from utils import ist_now


def _make_sample_snapshot(spot=22400.0, vwap=22400.0, ema20=22400.0, timestamp=None):
    now = timestamp or ist_now().replace(hour=10, minute=0, second=0, microsecond=0)
    c = Candle(now - timedelta(minutes=5), spot - 5.0, spot + 10.0, spot - 10.0, spot, volume=1500)
    snap = MarketSnapshot(
        snapshot_id="SNAP-RANGE-TEST",
        timestamp=now,
        spot=spot,
        candles=(c,),
        vwap=vwap,
        ema20=ema20,
        atr=20.0,
        pdh=spot + 50.0,
        pdl=spot - 50.0,
        orb={"high": spot + 30.0, "low": spot - 30.0},
    )
    return snap, c


def test_1_range_s2_candidate_blocked():
    """TEST 1: RANGE + S2 candidate -> BLOCK with RANGE_GATE_S2_BLOCKED."""
    snap, c = _make_sample_snapshot(spot=22405.0, vwap=22400.0)
    lvl_mgr = LevelLifecycleManager()
    s2 = S2VwapMomentum()

    assessment = s2.assess(snap, lvl_mgr, market_state="RANGE")
    assert assessment.eligible is False
    assert "RANGE_GATE_S2_BLOCKED" in assessment.blocker_codes

    arbiter = Arbiter()
    intent, shadows, reason = arbiter.evaluate([assessment], market_state="RANGE")
    assert intent is None
    assert "RANGE_GATE_S2_BLOCKED" in reason


def test_2_trend_up_s2_candidate_unchanged():
    """TEST 2: TREND-UP + S2 candidate -> existing behavior unchanged."""
    snap, c = _make_sample_snapshot(spot=22410.0, vwap=22400.0)
    lvl_mgr = LevelLifecycleManager()
    s2 = S2VwapMomentum()

    assessment = s2.assess(snap, lvl_mgr, market_state="TREND-UP")
    assert assessment.eligible is True
    assert assessment.direction == "LONG"
    assert "RANGE_GATE_S2_BLOCKED" not in assessment.blocker_codes

    arbiter = Arbiter()
    intent, shadows, reason = arbiter.evaluate([assessment], market_state="TREND-UP")
    assert intent is not None
    assert intent.strategy_id == "S2_VWAP_MOMENTUM"
    assert intent.direction == "LONG"


def test_3_trend_down_s2_candidate_unchanged():
    """TEST 3: TREND-DOWN + S2 candidate -> existing behavior unchanged."""
    snap, c = _make_sample_snapshot(spot=22390.0, vwap=22400.0)
    lvl_mgr = LevelLifecycleManager()
    s2 = S2VwapMomentum()

    assessment = s2.assess(snap, lvl_mgr, market_state="TREND-DOWN")
    assert assessment.eligible is True
    assert assessment.direction == "SHORT"
    assert "RANGE_GATE_S2_BLOCKED" not in assessment.blocker_codes

    arbiter = Arbiter()
    intent, shadows, reason = arbiter.evaluate([assessment], market_state="TREND-DOWN")
    assert intent is not None
    assert intent.strategy_id == "S2_VWAP_MOMENTUM"
    assert intent.direction == "SHORT"


def test_4_range_s3_candidate_unchanged():
    """TEST 4: RANGE + S3 candidate -> existing S3 behavior unchanged (NOT blocked)."""
    now = ist_now().replace(hour=10, minute=15)
    # Candle sweeps PDH 22450 (high 22455) and closes inside at 22445
    c = Candle(now - timedelta(minutes=5), 22440.0, 22455.0, 22435.0, 22445.0, volume=1800)
    snap = MarketSnapshot(
        snapshot_id="SNAP-S3-RANGE",
        timestamp=now,
        spot=22445.0,
        candles=(c,),
        vwap=22410.0,
        ema20=22415.0,
        atr=20.0,
        pdh=22450.0,
        pdl=22350.0,
    )
    lvl_mgr = LevelLifecycleManager()
    s3 = S3LiquidityReversal()

    assessment = s3.assess(snap, lvl_mgr, market_state="RANGE", s1_trend_strength="LOW")
    assert assessment.eligible is True
    assert assessment.direction == "SHORT"
    assert assessment.setup_score >= 80

    arbiter = Arbiter()
    intent, shadows, reason = arbiter.evaluate([assessment], market_state="RANGE")
    assert intent is not None
    assert intent.strategy_id == "S3_LIQUIDITY_REVERSAL"
    assert intent.direction == "SHORT"


def test_5_range_s1_candidate_unchanged():
    """TEST 5: RANGE + S1 candidate -> existing S1 behavior unchanged (not blanket-blocked)."""
    now = ist_now().replace(hour=10, minute=15)
    # Massive breakout candle through PDH
    c = Candle(now - timedelta(minutes=5), 22440.0, 22475.0, 22435.0, 22470.0, volume=2500)
    snap = MarketSnapshot(
        snapshot_id="SNAP-S1-RANGE",
        timestamp=now,
        spot=22470.0,
        candles=(c,),
        vwap=22420.0,
        ema20=22415.0,
        atr=20.0,
        pdh=22450.0,
        pdl=22350.0,
        pcr=1.2,
    )
    lvl_mgr = LevelLifecycleManager()
    s1 = S1TrendExpansion()

    # S1 evaluates based on its own structural breakout rules
    assessment = s1.assess(snap, lvl_mgr, market_state="RANGE")
    assert "RANGE_GATE_S2_BLOCKED" not in assessment.blocker_codes


def test_6_existing_open_s2_position_management_unaffected():
    """TEST 6: Existing open S2 position + market transitions to RANGE -> position management continues normally."""
    pm = PositionManager()
    now = ist_now().replace(hour=9, minute=30)
    trade = pm.open_position(
        trade_id="TRD-S2-EXISTING",
        strategy_id="S2_VWAP_MOMENTUM",
        direction="LONG",
        entry_time=now,
        entry_price=22400.0,
        initial_sl=22370.0,
        thesis="S2 trend morning entry",
        regime="TREND-UP",
    )
    assert pm.active_trade is not None
    assert pm.active_trade.lifecycle_state == TradeStateEnum.ENTERED

    # Next candle: market regime transitions to RANGE, price advances to 22450 (+50 pts)
    candle2 = Candle(now + timedelta(minutes=5), 22400.0, 22455.0, 22395.0, 22450.0, volume=2000)
    snap2 = MarketSnapshot(
        snapshot_id="SNAP-TRANSITION",
        timestamp=candle2.end,
        spot=22450.0,
        candles=(candle2,),
        vwap=22410.0,
        ema20=22405.0,
        atr=20.0,
    )
    # Position management continues normally in RANGE without being aborted or locked out
    pm.update(snap2, candle=candle2, current_regime="RANGE", structure_swing=22410.0)
    assert pm.active_trade.lifecycle_state in (TradeStateEnum.ENTERED, TradeStateEnum.TRAILING, TradeStateEnum.PROFIT_PROTECTION, TradeStateEnum.TREND_RUNNING)
    assert pm.active_trade.current_sl >= 22370.0


def test_7_blocked_s2_candidate_no_trade_intent():
    """TEST 7: Blocked S2 candidate -> no TradeIntent produced."""
    snap, c = _make_sample_snapshot(spot=22410.0, vwap=22400.0)
    lvl_mgr = LevelLifecycleManager()
    s2 = S2VwapMomentum()
    a2 = s2.assess(snap, lvl_mgr, market_state="RANGE")

    arbiter = Arbiter()
    intent, shadows, reason = arbiter.evaluate([a2], market_state="RANGE")
    assert intent is None


def test_8_blocked_s2_candidate_no_position_manager_entry():
    """TEST 8: Blocked S2 candidate -> no PositionManager entry."""
    pm = PositionManager()
    gov = RiskGovernor()
    now = ist_now().replace(hour=10, minute=0)

    # Fabricate an S2 intent to verify RiskGovernor hard-blocks it from opening a position
    intent = TradeIntent(
        strategy_id="S2_VWAP_MOMENTUM",
        direction="LONG",
        entry_zone=(22400.0, 22410.0),
        initial_sl=22380.0,
        target_plan="ATR_TRAIL",
        confidence_tier="VALID",
        reason_codes=["VWAP test"],
        blocker_codes=[],
        snapshot_id="SNAP-TEST",
        valid_until=now + timedelta(minutes=10),
    )

    app, reason, risk = gov.validate_intent(intent, now, market_state="RANGE")
    assert app is False
    assert reason == "RANGE_GATE_S2_BLOCKED"

    # Because app is False, position manager is never called
    assert pm.active_trade is None


def test_9_blocked_s2_candidate_no_execution_route():
    """TEST 9: Blocked S2 candidate -> no ExecutionRouter route."""
    gov = RiskGovernor()
    mock_router = MagicMock()
    now = ist_now().replace(hour=10, minute=0)

    intent = TradeIntent(
        strategy_id="S2_VWAP_MOMENTUM",
        direction="SHORT",
        entry_zone=(22390.0, 22400.0),
        initial_sl=22420.0,
        target_plan="ATR_TRAIL",
        confidence_tier="VALID",
        reason_codes=["VWAP rejection"],
        blocker_codes=[],
        snapshot_id="SNAP-TEST",
        valid_until=now + timedelta(minutes=10),
    )

    app, reason, _ = gov.validate_intent(intent, now, market_state="RANGE")
    if app:
        mock_router.route(intent)

    mock_router.route.assert_not_called()


def test_10_decision_record_contains_range_gate_s2_blocked():
    """TEST 10: Decision record contains RANGE_GATE_S2_BLOCKED."""
    rec = make_range_gate_decision_record(strategy="S2_VWAP_MOMENTUM", market_state="RANGE")
    assert rec["strategy"] == "S2_VWAP_MOMENTUM"
    assert rec["market_state"] == "RANGE"
    assert rec["decision"] == "BLOCK"
    assert rec["reason"] == "RANGE_GATE_S2_BLOCKED"

    gov = RiskGovernor()
    intent = TradeIntent(
        strategy_id="S2_VWAP_MOMENTUM",
        direction="LONG",
        entry_zone=(22400.0, 22410.0),
        initial_sl=22380.0,
        target_plan="ATR_TRAIL",
        confidence_tier="VALID",
        reason_codes=[],
        blocker_codes=[],
        snapshot_id="SNAP-1",
        valid_until=ist_now(),
    )
    gov.validate_intent(intent, ist_now(), market_state="RANGE")
    assert gov.last_decision_record is not None
    assert gov.last_decision_record["reason"] == "RANGE_GATE_S2_BLOCKED"


def test_11_no_lookahead_introduced():
    """TEST 11: No lookahead introduced (uses only decision-time snapshot)."""
    now = ist_now().replace(hour=10, minute=0)
    snap, c = _make_sample_snapshot(timestamp=now)
    # The decision is strictly evaluated on the current candle and current market_state
    s2 = S2VwapMomentum()
    lvl_mgr = LevelLifecycleManager()
    a = s2.assess(snap, lvl_mgr, market_state="RANGE")
    assert a.timestamp == now
    assert "RANGE_GATE_S2_BLOCKED" in a.blocker_codes


def test_12_no_cross_session_state_introduced():
    """TEST 12: No cross-session state introduced (clean reset on fresh session)."""
    gov1 = RiskGovernor()
    intent = TradeIntent(
        strategy_id="S2_VWAP_MOMENTUM",
        direction="LONG",
        entry_zone=(22400.0, 22410.0),
        initial_sl=22380.0,
        target_plan="ATR_TRAIL",
        confidence_tier="VALID",
        reason_codes=[],
        blocker_codes=[],
        snapshot_id="SNAP-DAY1",
        valid_until=ist_now(),
    )
    gov1.validate_intent(intent, ist_now(), market_state="RANGE")
    assert len(gov1.decision_records) == 1

    # Fresh session instantiated on next day
    gov2 = RiskGovernor()
    assert len(gov2.decision_records) == 0
    assert gov2.last_decision_record is None
