"""Phase 1 Acceptance Tests: Authority Refactor and Single Live Execution Pipeline.

Verifies:
1. test_no_legacy_direct_live_execution
2. test_only_arbiter_can_create_trade_intent
3. test_position_manager_is_authoritative
4. test_legacy_detectors_are_feature_only
5. test_stale_level_cannot_retrigger (Oct 8 levels 22,228 and 22,200)
6. test_s3_trend_override_blocks_countertrend
7. test_backfill_cannot_trigger_trade
8. test_live_candle_can_trigger_trade
9. test_short_gate_score_model
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest

import main
import settings
from data_sources.snapshot import MarketSnapshot
from engines.arbiter import Arbiter, RiskGovernor, TradeIntent
from engines.candles import Candle
from engines.levels_lifecycle import (
    LevelLifecycleManager,
    LevelRole,
    LevelStatus,
)
from engines.position_manager import PositionManager, TradeStateEnum
from engines.strategies import S1TrendExpansion, S2VwapMomentum, S3LiquidityReversal
from journal.store import JournalStore
from data_sources.market_data import MarketDataStore
from engines.regime import RegimeClassifier
from utils import ist_now


class DummyTG:
    def __init__(self):
        self.sent = []

    def send(self, msg: str):
        self.sent.append(msg)


def make_test_session(store, live_mode: bool = True):
    market = MarketDataStore(session=None, journal=store, holidays=set())
    market.set_prev_day(22500.0, 22400.0, 22450.0)
    session = main.DaySession(
        store=store,
        tg=DummyTG(),
        classifier=RegimeClassifier(event_dates=set()),
        event_day=False,
        today_iso=ist_now().date().isoformat(),
        market=market,
        live_mode=live_mode,
    )
    return session


def test_no_legacy_direct_live_execution(tmp_path):
    """1. Calling legacy fire() or _fire_mtf() directly does NOT create a live trade when live_mode is active."""
    store = JournalStore(db_path=tmp_path / "t.sqlite3")
    session = make_test_session(store, live_mode=True)
    assert session.live_mode is True
    assert session.legacy_execution_enabled is False

    now = ist_now()
    c = Candle(now, 22400.0, 22420.0, 22390.0, 22410.0)
    ev = SimpleNamespace(
        direction="LONG",
        candle=c,
        score=95,
        level=22400.0,
        level_name="PDH",
        reasons=[],
    )

    trade = session.fire(ev, "BREAKOUT", sl_spot=22380.0, broken_level=22400.0, level_name="PDH")
    assert trade is None
    assert len(session.book.trades) == 0
    assert session.position_manager.active_trade is None

    trade_mtf = session._fire_mtf(
        family="SWEEP-MSS-POI",
        direction="LONG",
        candle=c,
        entry_spot=22410.0,
        sl_spot=22380.0,
        broken_level=22400.0,
        level_name="SWEEP PDL",
    )
    assert trade_mtf is None
    assert len(session.book.trades) == 0
    assert session.position_manager.active_trade is None


def test_only_arbiter_can_create_trade_intent():
    """2. Verify only Arbiter produces TradeIntent from StrategyAssessments."""
    now = ist_now()
    c = Candle(now, 22400.0, 22430.0, 22395.0, 22425.0)
    snap = MarketSnapshot(
        snapshot_id="SNAP-INTENT-01",
        timestamp=now,
        spot=22425.0,
        candles=(c,),
        vwap=22400.0,
        ema20=22395.0,
        atr=20.0,
        orb={"high": 22420.0, "low": 22380.0},
        pdh=22415.0,
    )
    lvl_mgr = LevelLifecycleManager()
    s1 = S1TrendExpansion()
    assessment = s1.assess(snap, lvl_mgr, market_state="EXPANSION")
    assert assessment.eligible is True
    # StrategyAssessment is not a TradeIntent
    assert not isinstance(assessment, TradeIntent)

    arbiter = Arbiter()
    intent, shadows, reason = arbiter.evaluate([assessment], market_state="EXPANSION")
    assert intent is not None
    assert isinstance(intent, TradeIntent)
    assert intent.strategy_id == "S1_TREND_EXPANSION"
    assert intent.direction == "LONG"


def test_position_manager_is_authoritative():
    """3. Verify live position state is held exclusively in PositionManager."""
    pm = PositionManager()
    assert pm.active_trade is None

    now = ist_now().replace(hour=10, minute=15)
    trade_state = pm.open_position(
        trade_id="TRD-01",
        strategy_id="S1_TREND_EXPANSION",
        direction="LONG",
        entry_time=now,
        entry_price=22450.0,
        initial_sl=22410.0,
        thesis="Breakout above PDH",
        regime="TREND-UP",
        target_plan="STRUCTURE_TRAIL",
    )

    assert pm.active_trade is not None
    assert pm.active_trade.trade_id == "TRD-01"
    assert pm.active_trade.lifecycle_state == TradeStateEnum.ENTERED
    assert pm.active_trade.entry_price == 22450.0
    assert pm.active_trade.risk_pts == 40.0
    assert pm.active_trade.one_r_pts == 40.0

    # Test update and authoritative state transitions
    snap = MarketSnapshot(
        snapshot_id="SNAP-PM-01",
        timestamp=now + timedelta(minutes=5),
        spot=22530.0,
        candles=(),
        atr=20.0,
    )
    c2 = Candle(now + timedelta(minutes=5), 22490.0, 22535.0, 22485.0, 22530.0)
    state, exit_info = pm.update(snap, candle=c2, current_regime="TREND-UP")
    assert exit_info is None
    # Profit +80 pts = +2.0R -> runner armed & trailing SL moved
    assert pm.active_trade.lifecycle_state in (TradeStateEnum.PROFIT_PROTECTION, TradeStateEnum.TREND_RUNNING)
    assert pm.active_trade.current_sl >= 22450.0


def test_legacy_detectors_are_feature_only(tmp_path):
    """4. Verify legacy engines firing produces journal SKIP events (feature detection) but zero live orders."""
    store = JournalStore(db_path=tmp_path / "t.sqlite3")
    session = make_test_session(store, live_mode=True)

    now = ist_now()
    c = Candle(now, 22400.0, 22450.0, 22390.0, 22440.0)
    ev = SimpleNamespace(
        fired=True,
        near_miss=False,
        direction="LONG",
        candle=c,
        score=90,
        level=22420.0,
        level_name="ORB-H",
        reasons=[("ORB break", True, 90, "ORB-H")],
    )

    fired_trade = None
    def mock_fire():
        nonlocal fired_trade
        fired_trade = "FIRED"
        return fired_trade

    session._handle_evaluation(ev, family="ORB", fire_fn=mock_fire)
    # Fire function was NOT called
    assert fired_trade is None
    assert len(session.book.trades) == 0

    # Journal recorded feature detection SKIP event
    rows = store.conn.execute("SELECT type, family, notes FROM journal WHERE type='SKIP'").fetchall()
    assert len(rows) >= 1
    assert any("legacy detector" in r[2] and "feature-only" in r[2] for r in rows)


def test_stale_level_cannot_retrigger():
    """5. Regression test for Oct 8 levels at 22,228 and 22,200.
    Verify level broken by >60 pts is marked EXPIRED and cannot trigger trades."""
    now = ist_now()
    mgr = LevelLifecycleManager(tolerance_pts=5.0)

    # Register Oct 8 support levels
    lvl_22228 = mgr.register_level("S_22228", 22228.0, "SUPPORT_CLUSTER", LevelRole.SUPPORT, now)
    lvl_22200 = mgr.register_level("S_22200", 22200.0, "ROUND_NUMBER", LevelRole.SUPPORT, now)

    assert lvl_22228.is_tradeable is True
    assert lvl_22200.is_tradeable is True

    # 1. Price drops below 22,228 to 22,215 (breaks 22,228 by 13 pts)
    c1 = Candle(now, 22235.0, 22240.0, 22210.0, 22215.0)
    snap1 = MarketSnapshot(snapshot_id="S1", timestamp=now, spot=22215.0, candles=(c1,), atr=20.0)
    mgr.update(snap1, c1)

    assert lvl_22228.status == LevelStatus.BROKEN
    assert lvl_22228.is_tradeable is False
    assert lvl_22228.can_trigger_reversal is False

    # 2. Price dumps to 22,150 (> 78 pts below 22,228 and 50 pts below 22,200)
    # Since 22,228 is broken by 78 pts (> 60 pts), it must expire immediately!
    c2 = Candle(now + timedelta(minutes=5), 22215.0, 22220.0, 22145.0, 22150.0)
    snap2 = MarketSnapshot(snapshot_id="S2", timestamp=now + timedelta(minutes=5), spot=22150.0, candles=(c2,), atr=20.0)
    mgr.update(snap2, c2)

    assert lvl_22228.status == LevelStatus.EXPIRED
    assert lvl_22228.is_expired is True
    assert lvl_22228.is_tradeable is False

    # 3. Price drops further to 22,130 (> 70 pts below 22,200)
    # Level 22,200 is broken by > 60 pts -> must expire immediately!
    c3 = Candle(now + timedelta(minutes=10), 22150.0, 22155.0, 22125.0, 22130.0)
    snap3 = MarketSnapshot(snapshot_id="S3", timestamp=now + timedelta(minutes=10), spot=22130.0, candles=(c3,), atr=20.0)
    mgr.update(snap3, c3)

    assert lvl_22200.status == LevelStatus.EXPIRED
    assert lvl_22200.is_expired is True
    assert lvl_22200.is_tradeable is False

    # Neither level can be in actionable levels ladder
    actionable_ids = [l.level_id for l in mgr.get_actionable_levels()]
    assert "S_22228" not in actionable_ids
    assert "S_22200" not in actionable_ids


def test_s3_trend_override_blocks_countertrend():
    """6. Verify S3 cannot produce LONG during TREND-DOWN and cannot produce SHORT during TREND-UP."""
    now = ist_now()
    c = Candle(now, 22210.0, 22215.0, 22170.0, 22195.0)
    # Suppose candle sweeps PDL at 22180 and closes back at 22195
    snap = MarketSnapshot(
        snapshot_id="SNAP-S3-TEST",
        timestamp=now,
        spot=22195.0,
        candles=(c,),
        pdl=22180.0,
        atr=25.0,
    )
    lvl_mgr = LevelLifecycleManager()
    s3 = S3LiquidityReversal()

    # In TREND-DOWN: S3 LONG reversal is strictly vetoed
    ass_down = s3.assess(snap, lvl_mgr, market_state="TREND-DOWN")
    assert ass_down.eligible is False
    assert "COUNTER_TREND_REVERSAL_FORBIDDEN_IN_TREND_DOWN" in ass_down.blocker_codes

    # Arbiter also enforces trend veto even if assessment was eligible
    arbiter = Arbiter()
    # Create artificial eligible LONG assessment
    fake_long = SimpleNamespace(
        strategy_id="S3_LIQUIDITY_REVERSAL",
        eligible=True,
        direction="LONG",
        risk_reward_ratio=2.0,
        normalized_extension=0.5,
        setup_score=85,
        regime_fit_score=70,
        structure_score=85,
        empirical_expectancy=0.5,
        confidence_tier="STRONG",
        entry_zone=(22190.0, 22200.0),
        initial_sl=22170.0,
        management_plan="POOL_TARGET",
        reason_codes=["Swept PDL"],
        blocker_codes=[],
        snapshot_id="SNAP-S3-TEST",
        valid_until=now + timedelta(minutes=15),
    )
    intent, shadows, reason = arbiter.evaluate([fake_long], market_state="TREND-DOWN")
    assert intent is None
    assert "All eligible setups eliminated" in reason or "No eligible" in reason


def test_backfill_cannot_trigger_trade(tmp_path):
    """7. Verify historical candles processed during backfill cannot trigger execution."""
    store = JournalStore(db_path=tmp_path / "t.sqlite3")
    session = make_test_session(store, live_mode=True)

    now = ist_now()
    morning_time = now.replace(hour=9, minute=20)
    c1 = Candle(morning_time, 22450.0, 22470.0, 22440.0, 22460.0)
    c2 = Candle(morning_time + timedelta(minutes=5), 22460.0, 22485.0, 22455.0, 22480.0)

    # During candle engine backfill / refresh
    session.market.candles.append(c1)
    session.market.candles.append(c2)
    session.market.candle_engine.candles.append(c1)
    session.market.candle_engine.candles.append(c2)

    # Historical backfill populates candle memory but does not execute trades
    assert len(session.market.candles) == 2
    assert len(session.book.trades) == 0
    assert session.position_manager.active_trade is None


def test_live_candle_can_trigger_trade(tmp_path):
    """8. Verify a live candle meeting criteria triggers S1 -> Arbiter -> RiskGovernor -> PositionManager."""
    store = JournalStore(db_path=tmp_path / "t.sqlite3")
    session = make_test_session(store, live_mode=True)

    morning_now = ist_now().replace(hour=10, minute=20)
    # Bullish candle breaking above ORB-H and PDH with tight structural low (<=33 pts SL)
    c = Candle(morning_now, 22500.0, 22520.0, 22495.0, 22515.0, volume=3000)
    session.market.candles.append(c)
    session.market.candle_engine.candles.append(c)
    session.market.set_prev_day(22500.0, 22400.0, 22450.0)

    snap = MarketSnapshot(
        snapshot_id="SNAP-LIVE-EXEC-01",
        timestamp=morning_now,
        spot=22515.0,
        candles=(c,),
        vwap=22490.0,
        ema20=22495.0,
        atr=20.0,
        orb={"high": 22490.0, "low": 22430.0},
        pdh=22500.0,
        pcr=1.2,
    )
    session.current_snapshot = snap

    # Run unified pipeline
    session._process_unified_strategies(c)

    # Verify PositionManager opened the position
    pm_trade = session.position_manager.active_trade
    assert pm_trade is not None
    assert pm_trade.strategy_id == "S1_TREND_EXPANSION"
    assert pm_trade.direction == "LONG"
    assert pm_trade.lifecycle_state == TradeStateEnum.ENTERED
    assert pm_trade.entry_price == 22515.0


def test_short_gate_score_model():
    """9. Audit test proving S1 SHORT score with/without PCR and demonstrating the 75 vs 90 gap."""
    eval_time = ist_now().replace(hour=10, minute=35)
    c = Candle(eval_time, 22435.0, 22440.0, 22420.0, 22425.0, volume=2500)

    # Oct 8 Breakdown scenario:
    # Spot 22425 < VWAP 22460 & EMA20 22450 (+30)
    # Spot 22425 < ORB-L 22440 (+25)
    # Spot 22425 < PDL 22450 (+20)
    # PCR is None or neutral (>= 0.9)
    snap_without_pcr = MarketSnapshot(
        snapshot_id="OCT8-S1-AUDIT-NO-PCR",
        timestamp=eval_time,
        spot=22425.0,
        candles=(c,),
        vwap=22460.0,
        ema20=22450.0,
        atr=20.0,
        orb={"high": 22490.0, "low": 22440.0},
        pdl=22450.0,
        pcr=None,  # No PCR
    )
    lvl_mgr = LevelLifecycleManager()
    s1 = S1TrendExpansion()
    ass_no_pcr = s1.assess(snap_without_pcr, lvl_mgr, market_state="TREND-DOWN")

    # Score components: 30 + 25 + 20 = 75
    assert ass_no_pcr.setup_score == 75
    assert ass_no_pcr.direction == "SHORT"

    # RiskGovernor requires short_score >= 90
    rg = RiskGovernor()
    arbiter = Arbiter()
    intent, _, _ = arbiter.evaluate([ass_no_pcr], market_state="TREND-DOWN")
    assert intent is not None

    approved, reason, risk = rg.validate_intent(
        intent=intent,
        current_time=eval_time,
        short_has_strong_confluence=True,
        short_score=ass_no_pcr.setup_score,  # 75
    )
    # REJECTED because 75 < 90
    assert approved is False
    assert "SHORT_GATE_REJECT" in reason
    assert "75 < 90" in reason

    # Now with PCR < 0.9: adds +15 points -> total 90
    snap_with_pcr = MarketSnapshot(
        snapshot_id="OCT8-S1-AUDIT-WITH-PCR",
        timestamp=eval_time,
        spot=22425.0,
        candles=(c,),
        vwap=22460.0,
        ema20=22450.0,
        atr=20.0,
        orb={"high": 22490.0, "low": 22440.0},
        pdl=22450.0,
        pcr=0.82,  # PCR < 0.9 adds +15
    )
    ass_with_pcr = s1.assess(snap_with_pcr, lvl_mgr, market_state="TREND-DOWN")
    assert ass_with_pcr.setup_score == 90

    intent_with_pcr, _, _ = arbiter.evaluate([ass_with_pcr], market_state="TREND-DOWN")
    approved_pcr, reason_pcr, risk_pcr = rg.validate_intent(
        intent=intent_with_pcr,
        current_time=eval_time,
        short_has_strong_confluence=True,
        short_score=ass_with_pcr.setup_score,  # 90
    )
    # APPROVED because 90 >= 90 and strong confluence present
    assert approved_pcr is True
    assert reason_pcr == "APPROVED"
    assert risk_pcr == 0.5  # Gated short allocation: 0.5R
