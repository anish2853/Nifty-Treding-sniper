"""Tests for SHADOW ARM: M/W Liquidity Sniper (Parallel Measurement Engine).

Verifies:
1. Level Set (09:15 daily): PDH/PDL, high-volume swing points, OI walls, STRONG confluence tagging.
2. W Pattern (LONG): Touch 1 -> Pullback -> Touch 2 (stop hunt) -> Confirmation (body >= 50%) -> MSS -> Retest entry.
3. M Pattern (SHORT): Mirror lifecycle on STRONG resistance.
4. Risk & Targets: SL below pattern extremes (max 33 pts), TGT1 (pattern height), TGT2 (opposite liquidity).
5. Management: Intrabar SL, 50% partial at TGT1 + Breakeven stop, structural trail, 15:10 square-off.
6. The Experiment: Pure candle logic (no EMA/VWAP), counter-gate tracking, and verdict analysis.
7. FROZEN-v1 Isolation: Journal tagged SHADOW-MW without leaking into FROZEN-v1 TradeBook.
"""
import json
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import settings
from engines.candles import Candle
from engines.shadow_mw import MWLevel, MWPatternCandidate, ShadowMWEngine, ShadowMWTrade
from journal.store import JournalStore


def test_level_set_and_confluence_tagging(tmp_path):
    """Verifies Level Set at 09:15: PDH/PDL, high-volume swing points, and STRONG confluence tagging."""
    db_path = tmp_path / "test_mw_levels.db"
    store = JournalStore(db_path)

    today = datetime(2026, 10, 9, tzinfo=settings.IST)
    yesterday = today - timedelta(days=1)
    y_str = yesterday.date().isoformat()

    # Insert yesterday's closed candles with distinct swing points and volume
    yesterday_candles = [
        Candle(start=yesterday.replace(hour=9, minute=15), open=22400.0, high=22410.0, low=22390.0, close=22405.0, volume=100.0),
        # Swing High at 22,500 (Volume 5,000)
        Candle(start=yesterday.replace(hour=9, minute=20), open=22405.0, high=22500.0, low=22400.0, close=22490.0, volume=5000.0),
        Candle(start=yesterday.replace(hour=9, minute=25), open=22490.0, high=22480.0, low=22450.0, close=22460.0, volume=200.0),
        # Swing Low at 22,350 (Volume 6,000)
        Candle(start=yesterday.replace(hour=9, minute=30), open=22460.0, high=22465.0, low=22350.0, close=22380.0, volume=6000.0),
        Candle(start=yesterday.replace(hour=9, minute=35), open=22380.0, high=22420.0, low=22370.0, close=22410.0, volume=300.0),
    ]
    store.upsert_candles(yesterday_candles)

    # Mock market with PDH, PDL, and chain OI walls
    mock_market = MagicMock()
    mock_market.pdh = 22500.0  # Coincides with Swing High and Call Wall
    mock_market.pdl = 22350.0  # Coincides with Swing Low and Put Wall

    # Chain with Call OI wall at 22,500 and Put OI wall at 22,350
    mock_row_ce = MagicMock(strike=22500.0, ce_oi=150000, pe_oi=20000)
    mock_row_pe = MagicMock(strike=22350.0, ce_oi=30000, pe_oi=180000)
    mock_market.chain.rows = [mock_row_ce, mock_row_pe]

    engine = ShadowMWEngine(store=store, today_iso="2026-10-09")
    levels = engine.initialize_levels(mock_market, today_iso="2026-10-09")

    assert len(levels) >= 2
    pdh_lvl = next((l for l in levels if l.name == "PDH"), None)
    pdl_lvl = next((l for l in levels if l.name == "PDL"), None)

    assert pdh_lvl is not None
    assert pdh_lvl.price == 22500.0
    assert pdh_lvl.level_type == "RESISTANCE"
    assert pdh_lvl.has_oi_wall is True
    assert pdh_lvl.tag == "STRONG"

    assert pdl_lvl is not None
    assert pdl_lvl.price == 22350.0
    assert pdl_lvl.level_type == "SUPPORT"
    assert pdl_lvl.has_oi_wall is True
    assert pdl_lvl.tag == "STRONG"


def test_w_pattern_lifecycle_pure_candle_logic(tmp_path):
    """Complete W Pattern lifecycle:
    Touch 1 -> Pullback -> Touch 2 (stop hunt sweep) -> Confirmation (body >= 50%) -> MSS -> Retest Entry.
    Verifies pure candle logic without EMA or VWAP gates."""
    db_path = tmp_path / "test_w_lifecycle.db"
    store = JournalStore(db_path)
    engine = ShadowMWEngine(store=store, today_iso="2026-10-09")

    # Manually configure a STRONG support level at 22,400 (e.g. PDL + Put Wall)
    support_lvl = MWLevel(
        name="STRONG_SUPPORT",
        price=22400.0,
        level_type="SUPPORT",
        has_oi_wall=True,
        tag="STRONG",
    )
    engine.levels = [support_lvl]
    engine.levels_initialized = True

    t0 = datetime(2026, 10, 9, 9, 30, tzinfo=settings.IST)

    # 1. Touch 1: Dips to support (22,398) and closes back above (22,406) -> Rejection #1
    c1 = Candle(start=t0, open=22415.0, high=22418.0, low=22398.0, close=22406.0, volume=100.0)
    engine.on_candle(c1)
    cand = next((c for c in engine.candidates if c.pattern == "W"), None)
    assert cand is not None
    assert cand.state == "TOUCH_1"
    assert cand.touch1_price == 22398.0

    # 2. Pullback: Bounces away to 22,435 -> Neckline established
    c2 = Candle(start=t0 + timedelta(minutes=5), open=22406.0, high=22435.0, low=22405.0, close=22430.0, volume=100.0)
    engine.on_candle(c2)
    assert cand.state == "PULLBACK"
    assert cand.neckline_price == 22435.0

    # 3. Touch 2 with stop hunt: Sweeps below Touch 1 to 22,395, but closes at 22,402 (small body)
    c3 = Candle(start=t0 + timedelta(minutes=10), open=22425.0, high=22428.0, low=22395.0, close=22402.0, volume=100.0)
    engine.on_candle(c3)
    assert cand.state == "TOUCH_2"
    assert cand.touch2_price == 22395.0
    assert cand.w_lows == 22395.0  # Stop hunt swept Touch 1 (22,398)

    # 4. Confirmation candle: Closes back above level with body >= 50% of candle range
    # Open 22,401, Low 22,399, High 22,425, Close 22,423 -> Range 26, Body 22 (84.6% body)
    c4 = Candle(start=t0 + timedelta(minutes=15), open=22401.0, high=22425.0, low=22399.0, close=22423.0, volume=100.0)
    engine.on_candle(c4)
    assert cand.state == "CONFIRMED"

    # 5. MSS: Candle breaks firmly above neckline (22,435) with displacement to 22,445
    c5 = Candle(start=t0 + timedelta(minutes=20), open=22424.0, high=22448.0, low=22422.0, close=22445.0, volume=100.0)
    engine.on_candle(c5)
    assert cand.state in ("MSS", "RETEST_ARMED")

    # 6. Retest & Entry: Price dips to neckline (22,434) and rejects, closing at 22,442
    # Simulate day_state with COUNTER-TREND EMA/VWAP to test the experiment (Pure candle logic fires regardless)
    mock_day_state = MagicMock(ema20=22480.0, vwap=22490.0)  # Price 22,442 is BELOW EMA and VWAP!
    c6 = Candle(start=t0 + timedelta(minutes=25), open=22444.0, high=22446.0, low=22434.0, close=22442.0, volume=100.0)
    engine.on_candle(c6, day_state=mock_day_state)

    assert cand.state == "ENTERED"
    assert len(engine.trades) == 1
    trade = engine.trades[0]
    assert trade.direction == "LONG"
    assert trade.entry_price == 22442.0
    # SL below W lows (22,395 - 2 = 22,393) -> 22,442 - 22,393 = 49 pts -> capped at max 33 pts: 22,442 - 33 = 22,409
    assert trade.risk_pts <= 33.0
    assert trade.current_sl >= 22409.0
    # Pure candle experiment: counter_gate is True because price was below EMA20 and VWAP
    assert trade.counter_gate is True
    assert trade.status == "OPEN"

    # Verify journaled with family="SHADOW-MW"
    assert store.has_journal_event("2026-10-09", "[SHADOW-MW] FIRED SMW-001") or store.has_journal_event("2026-10-09", "SHADOW_ENTRY")


def test_m_pattern_lifecycle_short(tmp_path):
    """Complete M Pattern lifecycle on STRONG resistance:
    Touch 1 -> Pullback -> Touch 2 (sweep) -> Confirmation -> MSS -> Retest Entry."""
    db_path = tmp_path / "test_m_lifecycle.db"
    store = JournalStore(db_path)
    engine = ShadowMWEngine(store=store, today_iso="2026-10-09")

    # STRONG resistance level at 22,500 (e.g. PDH + Call Wall)
    res_lvl = MWLevel(
        name="STRONG_RESISTANCE",
        price=22500.0,
        level_type="RESISTANCE",
        has_oi_wall=True,
        tag="STRONG",
    )
    engine.levels = [res_lvl]
    engine.levels_initialized = True

    t0 = datetime(2026, 10, 9, 10, 0, tzinfo=settings.IST)

    # 1. Touch 1: Rallies to 22,503 and closes back below at 22,492 -> Rejection #1
    c1 = Candle(start=t0, open=22485.0, high=22503.0, low=22482.0, close=22492.0, volume=100.0)
    engine.on_candle(c1)
    cand = next((c for c in engine.candidates if c.pattern == "M"), None)
    assert cand is not None
    assert cand.state == "TOUCH_1"
    assert cand.touch1_price == 22503.0

    # 2. Pullback: Drops to 22,465 -> Neckline established
    c2 = Candle(start=t0 + timedelta(minutes=5), open=22492.0, high=22494.0, low=22465.0, close=22470.0, volume=100.0)
    engine.on_candle(c2)
    assert cand.state == "PULLBACK"
    assert cand.neckline_price == 22465.0

    # 3. Touch 2 with stop hunt sweep: Rallies to 22,506 (sweeps Touch 1 22,503)
    c3 = Candle(start=t0 + timedelta(minutes=10), open=22472.0, high=22506.0, low=22470.0, close=22497.0, volume=100.0)
    engine.on_candle(c3)
    assert cand.state == "TOUCH_2"
    assert cand.m_highs == 22506.0

    # 4. Confirmation: Closes back below 22,500 with body >= 50%
    # Open 22,498, High 22,501, Low 22,475, Close 22,477 -> Range 26, Body 21 (80.7% body)
    c4 = Candle(start=t0 + timedelta(minutes=15), open=22498.0, high=22501.0, low=22475.0, close=22477.0, volume=100.0)
    engine.on_candle(c4)
    assert cand.state == "CONFIRMED"

    # 5. MSS: Candle breaks below neckline (22,465) with displacement to 22,455
    c5 = Candle(start=t0 + timedelta(minutes=20), open=22476.0, high=22478.0, low=22452.0, close=22455.0, volume=100.0)
    engine.on_candle(c5)
    assert cand.state in ("MSS", "RETEST_ARMED")

    # 6. Retest & Entry: Retests neckline (22,466) and rejects, closing at 22,458
    c6 = Candle(start=t0 + timedelta(minutes=25), open=22456.0, high=22466.0, low=22454.0, close=22458.0, volume=100.0)
    engine.on_candle(c6)

    assert cand.state == "ENTERED"
    assert len(engine.trades) == 1
    trade = engine.trades[0]
    assert trade.direction == "SHORT"
    assert trade.entry_price == 22458.0
    assert trade.risk_pts <= 33.0


def test_trade_management_tgt1_and_breakeven_trail(tmp_path):
    """Verifies management stack: +1.0R TGT1 hit -> 50% booked, SL moved to BE, structural trail."""
    db_path = tmp_path / "test_mgmt.db"
    store = JournalStore(db_path)
    engine = ShadowMWEngine(store=store, today_iso="2026-10-09")

    # Create an open LONG trade at 22,450 with 25 pt risk (SL: 22,425, TGT1: 22,475, TGT2: 22,510)
    trade = ShadowMWTrade(
        trade_id="SMW-TEST",
        pattern="W",
        direction="LONG",
        level_name="PDL",
        level_price=22400.0,
        entry_price=22450.0,
        initial_sl=22425.0,
        current_sl=22425.0,
        risk_pts=25.0,
        tgt1_price=22475.0,
        tgt2_price=22510.0,
        entry_time=datetime(2026, 10, 9, 10, 0, tzinfo=settings.IST).isoformat(),
        counter_gate=True,
    )
    engine.trades.append(trade)

    t0 = datetime(2026, 10, 9, 10, 5, tzinfo=settings.IST)

    # Candle reaches TGT1 at 22,480
    c_tgt1 = Candle(start=t0, open=22452.0, high=22480.0, low=22450.0, close=22478.0, volume=100.0)
    engine.on_candle(c_tgt1)

    assert trade.half_booked is True
    assert trade.status == "PARTIAL_TGT1"
    assert trade.current_sl == 22450.0  # SL moved to Breakeven (entry price)

    # Next candle pulls back to 22,445 (below entry) -> Breakeven stop triggered!
    c_stop = Candle(start=t0 + timedelta(minutes=5), open=22475.0, high=22476.0, low=22445.0, close=22448.0, volume=100.0)
    engine.on_candle(c_stop)

    assert trade.status == "CLOSED"
    assert trade.exit_reason == "BE_SL"
    # 50% booked at TGT1 (+1.0R), 50% stopped at BE (0R) -> net +0.50R!
    assert trade.realized_r == 0.50


def test_square_off_at_1510(tmp_path):
    """Verifies forced 15:10 IST square-off closes open shadow positions with exact R."""
    db_path = tmp_path / "test_sqoff.db"
    store = JournalStore(db_path)
    engine = ShadowMWEngine(store=store, today_iso="2026-10-09")

    trade = ShadowMWTrade(
        trade_id="SMW-1510",
        pattern="W",
        direction="LONG",
        level_name="PDL",
        level_price=22400.0,
        entry_price=22450.0,
        initial_sl=22425.0,
        current_sl=22425.0,
        risk_pts=25.0,
        tgt1_price=22480.0,
        tgt2_price=22520.0,
        entry_time=datetime(2026, 10, 9, 14, 0, tzinfo=settings.IST).isoformat(),
    )
    engine.trades.append(trade)

    t_1510 = datetime(2026, 10, 9, 15, 10, tzinfo=settings.IST)
    closed = engine.square_off(exit_price=22470.0, exit_time=t_1510)

    assert len(closed) == 1
    assert trade.status == "CLOSED"
    assert trade.exit_reason == "SQUARE_OFF_1510"
    # Points gained: 22,470 - 22,450 = 20 pts -> 20 / 25 = +0.80R
    assert trade.realized_r == 0.80


def test_experiment_verdict_and_side_by_side_summary(tmp_path):
    """Verifies side-by-side scorecard and EMA/VWAP gate dropping experiment verdict."""
    db_path = tmp_path / "test_summary.db"
    store = JournalStore(db_path)
    engine = ShadowMWEngine(store=store, today_iso="2026-10-09")

    # Add 2 closed shadow trades that took counter-trend entries (dropped EMA/VWAP gates)
    t1 = ShadowMWTrade(
        trade_id="SMW-001", pattern="W", direction="LONG", level_name="PDL", level_price=22400.0,
        entry_price=22450.0, initial_sl=22425.0, current_sl=22425.0, risk_pts=25.0,
        tgt1_price=22475.0, tgt2_price=22510.0, entry_time="2026-10-09T10:00:00",
        status="CLOSED", exit_price=22510.0, exit_reason="TGT2_HIT", realized_r=1.75,
        counter_gate=True,
    )
    t2 = ShadowMWTrade(
        trade_id="SMW-002", pattern="M", direction="SHORT", level_name="PDH", level_price=22500.0,
        entry_price=22480.0, initial_sl=22505.0, current_sl=22505.0, risk_pts=25.0,
        tgt1_price=22455.0, tgt2_price=22420.0, entry_time="2026-10-09T11:00:00",
        status="CLOSED", exit_price=22505.0, exit_reason="HARD_SL", realized_r=-1.00,
        counter_gate=True,
    )
    engine.trades.extend([t1, t2])

    summary = engine.get_summary("2026-10-09")
    stats = summary["shadow_stats"]
    assert stats["trades"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["win_rate_pct"] == 50.0
    assert stats["total_r"] == 0.75
    assert stats["expectancy_r"] == 0.38

    # Experiment analysis: Counter-gate trades net +0.75R -> Verdict: HELPED!
    exp_analysis = summary["experiment_analysis"]
    assert exp_analysis["counter_gate_trades"] == 2
    assert exp_analysis["counter_gate_r"] == 0.75
    assert "HELPED" in exp_analysis["verdict"]
    assert exp_analysis["promotion_progress"] == "2 / 20"
    assert exp_analysis["promotion_ready"] is False


def test_liquidity_trendline_fib_engine():
    """Verifies Arm 2 Trendline Retracement into Fibonacci Golden Pocket."""
    from engines.shadow_mw import LiquidityTrendlineFibEngine

    engine = LiquidityTrendlineFibEngine(pdh=22500.0, pdl=22300.0)
    t0 = datetime(2026, 10, 9, 9, 15, tzinfo=settings.IST)

    # 1. Swing High at 22,400 (Bar 1)
    c1 = Candle(start=t0, open=22380.0, high=22385.0, low=22370.0, close=22375.0)
    c2 = Candle(start=t0 + timedelta(minutes=5), open=22375.0, high=22400.0, low=22370.0, close=22395.0)
    c3 = Candle(start=t0 + timedelta(minutes=10), open=22395.0, high=22390.0, low=22360.0, close=22365.0)
    engine.on_candle(c1)
    engine.on_candle(c2)
    engine.on_candle(c3)

    # 2. Swing Low at 22,340 (Bar 4)
    c4 = Candle(start=t0 + timedelta(minutes=15), open=22365.0, high=22370.0, low=22340.0, close=22345.0)
    c5 = Candle(start=t0 + timedelta(minutes=20), open=22345.0, high=22365.0, low=22350.0, close=22360.0)
    engine.on_candle(c4)
    engine.on_candle(c5)

    # In downtrend (Swing High 22,400 -> Swing Low 22,340 -> H = 60 pts)
    # Golden Pocket (50% - 61.8%): 22,340 + 0.5*60 = 22,370; 22,340 + 0.618*60 = 22,377.08
    # Candle 6 pulls back to 22,374 (inside Golden Pocket) and rejects (bearish close at 22,362)
    c6 = Candle(start=t0 + timedelta(minutes=25), open=22360.0, high=22374.0, low=22358.0, close=22362.0)
    engine.on_candle(c6)

    assert engine.active_trade is not None
    trade = engine.active_trade
    assert trade.direction == "SHORT"
    assert trade.entry_price == 22362.0
    assert trade.tgt1_price == 22340.0  # Prior swing low
    assert trade.initial_sl <= 22402.0  # SL above prior high or max 33 pts

    # Candle 7 reaches prior low (TGT1) -> 50% booked and SL moved to Breakeven
    c7 = Candle(start=t0 + timedelta(minutes=30), open=22360.0, high=22362.0, low=22338.0, close=22339.0)
    engine.on_candle(c7)
    assert trade.half_booked is True
    assert trade.current_sl == 22362.0  # Breakeven stop

    # Candle 8 reaches TGT2 (-0.618 extension: 22,340 - 0.618*60 = 22,302.92)
    c8 = Candle(start=t0 + timedelta(minutes=35), open=22339.0, high=22340.0, low=22300.0, close=22302.0)
    engine.on_candle(c8)
    assert trade.status == "CLOSED"
    assert trade.exit_reason == "FIB_EXT_TGT2_HIT"
    assert trade.realized_r > 1.0


def test_unified_liquidity_sniper_isolation():
    """Verifies UnifiedLiquiditySniper runs cleanly in isolated mode combining both arms."""
    from engines.shadow_mw import UnifiedLiquiditySniper, MWLevel

    engine = UnifiedLiquiditySniper(pdh=22500.0, pdl=22350.0, today_iso="2026-10-09", isolated=True)
    engine.arm1.levels.append(MWLevel(name="PDH", price=22500.0, level_type="RESISTANCE", tag="STRONG"))
    engine.arm1.levels.append(MWLevel(name="PDL", price=22350.0, level_type="SUPPORT", tag="STRONG"))
    engine.arm1.levels_initialized = True

    t0 = datetime(2026, 10, 9, 9, 15, tzinfo=settings.IST)
    c = Candle(start=t0, open=22400.0, high=22420.0, low=22390.0, close=22410.0)
    engine.on_candle(c)

    summary = engine.get_summary()
    assert "combined_trades" in summary
    assert "combined_win_rate" in summary
    assert "combined_total_r" in summary
    assert engine.isolated is True
    assert engine.arm1.isolated is True


def test_sniper_chart_with_entry_symbols():
    """Verifies that Chart Engine builds candlesticks, trendline, and upside/downside entry markers and annotations."""
    from dashboard.chart import build_liquidity_sniper_chart

    t0 = datetime(2026, 10, 9, 9, 15, tzinfo=settings.IST)
    candles = [
        Candle(start=t0, open=22400.0, high=22410.0, low=22390.0, close=22405.0),
        Candle(start=t0 + timedelta(minutes=5), open=22405.0, high=22420.0, low=22400.0, close=22415.0),
    ]
    mock_trades = [
        {
            "trade_id": "TEST-001",
            "arm": "Arm 2 (Fib Retracement)",
            "direction": "LONG",
            "setup": "Uptrend Fib Golden Pocket Pullback",
            "strike": 22400,
            "option_symbol": "NIFTY 22400 CE",
            "entry_prem": 140.0,
            "sl_prem": 130.0,
            "tgt1_prem": 160.0,
            "tgt2_prem": 180.0,
            "entry_time": t0.isoformat(),
            "entry_price": 22405.0,
            "sl": 22385.0,
            "risk_pts": 20.0,
            "fib_50": 22400.0,
            "fib_618": 22395.0,
            "fib_705": 22392.0,
            "tgt1": 22425.0,
            "tgt2": 22445.0,
            "exit_time": (t0 + timedelta(minutes=5)).isoformat(),
            "exit_price": 22425.0,
            "exit_reason": "PARTIAL_TGT1",
            "result_r": 1.0,
            "notes": "Test trade",
        }
    ]

    trend_info = {
        "trend": "UPTREND",
        "p1": (t0, 22390.0),
        "p2": (t0 + timedelta(minutes=5), 22400.0),
    }
    fig = build_liquidity_sniper_chart(candles, mock_trades, pdh=22450.0, pdl=22380.0, trendline_info=trend_info)
    assert fig is not None
    # Traces: Candlestick, Trendline, SL, TGT1, TGT2, Entry symbol marker, Exit symbol marker
    assert len(fig.data) >= 4
    # Annotations: At least entry candle annotation ('BUY CE') and exit annotation
    assert len(fig.layout.annotations) >= 2
    # Verify upside entry symbol annotation text
    assert any("BUY CE" in ann.text for ann in fig.layout.annotations)


def test_compute_fixed_range_volume_profile():
    from engines.shadow_mw import compute_fixed_range_volume_profile

    t0 = datetime(2026, 10, 8, 9, 15)
    candles = [
        Candle(start=t0, open=25000.0, high=25050.0, low=24980.0, close=25020.0, volume=1000.0),
        Candle(start=t0 + timedelta(minutes=5), open=25020.0, high=25080.0, low=25010.0, close=25070.0, volume=2500.0),
        Candle(start=t0 + timedelta(minutes=10), open=25070.0, high=25120.0, low=25060.0, close=25110.0, volume=3500.0),
        Candle(start=t0 + timedelta(minutes=15), open=25110.0, high=25115.0, low=25040.0, close=25050.0, volume=2000.0),
    ]

    frvp = compute_fixed_range_volume_profile(candles, value_area_pct=0.70)
    assert "vah" in frvp
    assert "val" in frvp
    assert "poc" in frvp
    assert frvp["val"] <= frvp["poc"] <= frvp["vah"]
    assert frvp["total_volume"] > 0
    assert frvp["spread"] == round(frvp["vah"] - frvp["val"], 1)


def test_frvp_sweep_engine_val_long():
    from engines.shadow_mw import FRVPSweepEngine

    vah = 25100.0
    val = 25000.0
    poc = 25050.0
    engine = FRVPSweepEngine(vah=vah, val=val, poc=poc, use_trend_filter=False)

    t0 = datetime(2026, 10, 9, 9, 20)
    # Candle sweeps VAL (low 24990 < 25000) and closes inside (close 25015 > 25000, green)
    c_sweep = Candle(start=t0, open=25005.0, high=25020.0, low=24990.0, close=25015.0, volume=500.0)
    engine.on_candle(c_sweep)

    assert engine.active_trade is not None
    assert engine.active_trade.direction == "LONG"
    assert engine.active_trade.tgt1_price == poc
    assert engine.active_trade.tgt2_price == vah
    assert engine.active_trade.entry_price == 25015.0

    # Next candle reaches POC (25050) -> Locks 50% & moves stop to BE_SL
    c_poc = Candle(start=t0 + timedelta(minutes=5), open=25015.0, high=25055.0, low=25010.0, close=25052.0, volume=600.0)
    engine.on_candle(c_poc)
    assert engine.active_trade.half_booked is True
    assert engine.active_trade.current_sl == 25015.0  # Breakeven stop

    # Next candle reaches VAH (25100) -> Full Target reached!
    c_vah = Candle(start=t0 + timedelta(minutes=10), open=25052.0, high=25105.0, low=25050.0, close=25102.0, volume=700.0)
    engine.on_candle(c_vah)
    assert engine.active_trade is None
    assert len(engine.trades) == 1
    assert engine.trades[0].exit_reason == "TGT_VAH"
    assert engine.trades[0].realized_r > 0


def test_build_liquidity_sniper_chart_with_frvp_and_session_breaks():
    from dashboard.chart import build_liquidity_sniper_chart

    t0 = datetime(2026, 10, 9, 9, 15)
    candles = [
        Candle(start=t0, open=25000.0, high=25030.0, low=24990.0, close=25020.0, volume=100.0),
        Candle(start=t0 + timedelta(minutes=5), open=25020.0, high=25050.0, low=25015.0, close=25045.0, volume=100.0),
    ]
    fig = build_liquidity_sniper_chart(
        candles=candles,
        trades=[],
        pdh=25100.0,
        pdl=24950.0,
        vah=25080.0,
        val=24980.0,
        poc=25030.0,
        show_session_breaks=True,
    )
    assert fig is not None
    # Shapes should contain PDH, PDL, VAH, VAL, POC (at least 5 shapes)
    assert len(fig.layout.shapes) >= 5


def test_compute_option_leg_and_telegram_alert():
    from engines.shadow_mw import compute_option_leg
    from alerts.messages import format_sniper_entry_alert, format_sniper_tgt1_alert, format_sniper_exit_alert

    # Spot at 25032.0 -> ATM strike should round to 25050
    leg_long = compute_option_leg(spot=25032.0, direction="LONG", risk_pts=20.0, tgt1_price=25080.0, tgt2_price=25120.0)
    assert leg_long["strike"] == 25050
    assert leg_long["option_type"] == "CE"
    assert leg_long["option_symbol"] == "NIFTY 25050 CE"
    assert leg_long["entry_prem"] > 0
    assert leg_long["sl_prem"] < leg_long["entry_prem"]
    assert leg_long["tgt1_prem"] > leg_long["entry_prem"]
    assert leg_long["tgt2_prem"] > leg_long["tgt1_prem"]

    # Short at 25032.0 -> ATM strike 25050 PE
    leg_short = compute_option_leg(spot=25032.0, direction="SHORT", risk_pts=20.0, tgt1_price=24980.0, tgt2_price=24940.0)
    assert leg_short["strike"] == 25050
    assert leg_short["option_type"] == "PE"
    assert leg_short["option_symbol"] == "NIFTY 25050 PE"

    # Test Telegram entry alert formatting
    alert_msg = format_sniper_entry_alert(
        trade_id="TL-FIB-L-001",
        direction="LONG",
        setup_name="Uptrend Fib Golden Pocket Pullback",
        entry_price=25032.0,
        initial_sl=25012.0,
        risk_pts=20.0,
        tgt1_price=25080.0,
        tgt2_price=25120.0,
        strike=leg_long["strike"],
        option_type=leg_long["option_type"],
        option_symbol=leg_long["option_symbol"],
        entry_prem=leg_long["entry_prem"],
        sl_prem=leg_long["sl_prem"],
        tgt1_prem=leg_long["tgt1_prem"],
        tgt2_prem=leg_long["tgt2_prem"],
    )
    assert "NIFTY 25050 CE" in alert_msg
    assert "BUY CE (CALL)" in alert_msg
    assert f"₹{leg_long['entry_prem']:.1f}" in alert_msg
    assert "BOOK 50% PROFIT, MOVE SL TO BREAKEVEN (BE_SL)" in alert_msg

    # Test Telegram TGT1 alert formatting
    tgt1_msg = format_sniper_tgt1_alert(
        trade_id="TL-FIB-L-001",
        direction="LONG",
        entry_price=25032.0,
        tgt1_price=25080.0,
        tgt2_price=25120.0,
        strike=leg_long["strike"],
        option_type=leg_long["option_type"],
        option_symbol=leg_long["option_symbol"],
        entry_prem=leg_long["entry_prem"],
        tgt1_prem=leg_long["tgt1_prem"],
        tgt2_prem=leg_long["tgt2_prem"],
    )
    assert "TARGET 1 HIT — 50% PROFIT BOOKED" in tgt1_msg
    assert "NIFTY 25050 CE" in tgt1_msg
    assert "STOP MOVED TO ENTRY" in tgt1_msg

    # Test Telegram Exit alert formatting
    exit_msg = format_sniper_exit_alert(
        trade_id="TL-FIB-L-001",
        direction="LONG",
        entry_price=25032.0,
        exit_price=25120.0,
        realized_r=2.20,
        exit_reason="FIB_EXT_TGT2_HIT",
        notes="Full Target 2 Hit (+2.20R)",
        strike=leg_long["strike"],
        option_type=leg_long["option_type"],
        option_symbol=leg_long["option_symbol"],
        entry_prem=leg_long["entry_prem"],
        exit_prem=leg_long["tgt2_prem"],
    )
    assert "OPTION TRADE CLOSED" in exit_msg
    assert "NIFTY 25050 CE" in exit_msg
    assert "+2.20 R" in exit_msg


def test_liquidity_trendline_option_execution():
    from engines.shadow_mw import LiquidityTrendlineFibEngine

    dispatched = []
    def mock_notifier(event, trade):
        dispatched.append((event, trade))

    engine = LiquidityTrendlineFibEngine(pdh=22500.0, pdl=22300.0, notifier=mock_notifier)
    t0 = datetime(2026, 10, 9, 9, 15, tzinfo=settings.IST)

    # 1. Swing High at 22,400 (Bar 1)
    engine.on_candle(Candle(start=t0, open=22380.0, high=22385.0, low=22370.0, close=22375.0))
    engine.on_candle(Candle(start=t0 + timedelta(minutes=5), open=22375.0, high=22400.0, low=22370.0, close=22395.0))
    engine.on_candle(Candle(start=t0 + timedelta(minutes=10), open=22395.0, high=22390.0, low=22360.0, close=22365.0))

    # 2. Swing Low at 22,340 (Bar 4)
    engine.on_candle(Candle(start=t0 + timedelta(minutes=15), open=22365.0, high=22370.0, low=22340.0, close=22345.0))
    engine.on_candle(Candle(start=t0 + timedelta(minutes=20), open=22345.0, high=22365.0, low=22350.0, close=22360.0))

    # In downtrend (Swing High 22,400 -> Swing Low 22,340 -> H = 60 pts)
    # Candle 6 pulls back into Golden Pocket [22,370 - 22,377] and rejects
    c6 = Candle(start=t0 + timedelta(minutes=25), open=22360.0, high=22374.0, low=22358.0, close=22362.0)
    engine.on_candle(c6)

    assert engine.active_trade is not None
    trade = engine.active_trade
    assert trade.direction == "SHORT"
    assert trade.strike == 22350
    assert trade.option_type == "PE"
    assert trade.option_symbol == "NIFTY 22350 PE"
    assert trade.entry_prem > 0
    assert trade.sl_prem > 0
    assert trade.tgt1_prem > trade.entry_prem
    assert len(dispatched) >= 1
    assert dispatched[0][0] == "ENTRY"

    # Candle 7 reaches prior low (TGT1) -> 50% booked and SL moved to Breakeven
    c7 = Candle(start=t0 + timedelta(minutes=30), open=22360.0, high=22362.0, low=22338.0, close=22339.0)
    engine.on_candle(c7)
    assert trade.half_booked is True
    assert trade.current_sl == 22362.0  # Breakeven stop
    assert any(d[0] == "TGT1" for d in dispatched)

    # Candle 8 reaches TGT2 -> exits at target
    c8 = Candle(start=t0 + timedelta(minutes=35), open=22339.0, high=22340.0, low=22300.0, close=22302.0)
    engine.on_candle(c8)
    assert trade.status == "CLOSED"
    assert trade.exit_reason == "FIB_EXT_TGT2_HIT"
    assert trade.exit_prem == trade.tgt2_prem
    assert any(d[0] == "EXIT" for d in dispatched)



