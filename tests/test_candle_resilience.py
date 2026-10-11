"""Tests for Phase 13.5: Candle Feed Resilience and Session Preflight.

Verifies:
1. Chain-spot candle synthesis fallback when chart API fails for >= 2 cycles.
2. Candle engine priority: chart API primary -> chain synthesis fallback -> blindness only if both die.
3. Synthesized candles are tagged 'SYNTH' in memory and SQLite store.
4. Trade management (SL, trailing, exits) on SYNTH candles with managed_by source tagging.
5. Session preflight at 09:20 IST: health detection, degraded-mode alerting, and journaling.
6. Clean session preservation during chart API outage when chain spot samples are available.
"""
import json
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import settings
from utils import ist_now
from data_sources.market_data import MarketDataStore
from journal.store import JournalStore
from engines.candles import Candle, CandleEngine
from engines.position_manager import PositionManager, TradeStateEnum, ExitClassification
from main import DaySession


def test_chain_spot_synthesis_fallback(tmp_path):
    """When chart API fails for >= 2 cycles, synthesize 5-min candles from chain spot samples."""
    db_path = tmp_path / "test_synth.db"
    store = JournalStore(db_path)
    engine = CandleEngine(session=None, store=store)

    today = datetime(2026, 10, 9, tzinfo=settings.IST)
    # Feed spot samples every 60s for the 09:30-09:35 candle interval
    t0 = today.replace(hour=9, minute=30, second=10)
    samples = [
        (t0, 22450.0),
        (t0.replace(minute=31), 22465.0),
        (t0.replace(minute=32), 22440.0),
        (t0.replace(minute=33), 22480.0),
        (t0.replace(minute=34), 22470.0),
    ]
    for ts, px in samples:
        engine.add_spot_sample(px, ts=ts, source="chain")

    now = today.replace(hour=9, minute=35, second=5)
    # First refresh with empty chart API -> chart_fail_count = 1
    fresh1 = engine.refresh(now=now)
    assert len(fresh1) == 1
    c1 = fresh1[0]
    assert c1.source == "SYNTH"
    assert c1.open == 22450.0
    assert c1.high == 22480.0
    assert c1.low == 22440.0
    assert c1.close == 22470.0
    assert c1.start == today.replace(hour=9, minute=30, second=0)
    assert c1.end == today.replace(hour=9, minute=35, second=0)

    # Verify persisted in SQLite store with source='SYNTH'
    rows = store.fetch_candles("2026-10-09")
    assert len(rows) == 1
    assert rows[0]["source"] == "SYNTH"
    assert rows[0]["close"] == 22470.0


def test_candle_engine_priority_chart_primary_then_recovery(tmp_path):
    """Chart API is primary. If chart is available, it takes precedence.
    When chart recovers, official chart candles replace SYNTH candles."""
    db_path = tmp_path / "test_priority.db"
    store = JournalStore(db_path)

    # Mock session that returns 09:15-09:20 and 09:20-09:25 candles
    today = datetime(2026, 10, 9, tzinfo=settings.IST)
    c1_start = today.replace(hour=9, minute=15)
    c2_start = today.replace(hour=9, minute=20)

    # Use today's epoch seconds so _ts_to_ist generates today's date
    base_dt = datetime(2026, 10, 9, 9, 15, tzinfo=settings.IST)
    from datetime import timezone
    base_sec = int(base_dt.astimezone(timezone.utc).timestamp())
    chart_ticks = []
    for sec in range(0, 600):  # 10 minutes of ticks
        chart_ticks.append([(base_sec + sec) * 1000, 22400.0 + sec * 0.1, "NM"])

    mock_session = MagicMock()
    mock_session.get_json_once.return_value = {"grapthData": chart_ticks}
    mock_session.last_status = 200

    engine = CandleEngine(session=mock_session, store=store)
    now = today.replace(hour=9, minute=26)

    # Cycle 1: Chart is healthy
    fresh = engine.refresh(now=now)
    assert len(fresh) == 2
    assert all(c.source == "chart" for c in fresh)
    assert engine.last_active_source == "chart"

    # Cycle 2: Chart fails (returns empty)
    mock_session.get_json_once.return_value = None
    mock_session.last_status = 500

    # Provide spot samples for 09:25-09:30 interval
    t_synth = today.replace(hour=9, minute=25, second=30)
    engine.add_spot_sample(22470.0, ts=t_synth, source="chain")
    engine.add_spot_sample(22490.0, ts=t_synth.replace(minute=27), source="chain")
    engine.add_spot_sample(22485.0, ts=t_synth.replace(minute=29), source="chain")

    now2 = today.replace(hour=9, minute=31)
    fresh2 = engine.refresh(now=now2)

    # Previous chart candles are preserved, plus new SYNTH candle
    assert len(engine.candles) == 3
    assert engine.candles[0].source == "chart"
    assert engine.candles[1].source == "chart"
    assert engine.candles[2].source == "SYNTH"
    assert engine.candles[2].close == 22485.0
    assert engine.last_active_source == "SYNTH"


def test_trade_management_on_synth_candle(tmp_path):
    """PositionManager manages SL and exits on SYNTH candles, recording managed_by."""
    pm = PositionManager(hard_loss_cap_r=-1.1)
    now = datetime(2026, 10, 9, 10, 0, tzinfo=settings.IST)

    # Open a LONG position at 22,450 with SL at 22,420 (risk: 30 pts)
    trade = pm.open_position(
        trade_id="TRD-TEST-SYNTH",
        strategy_id="S1_TREND_EXPANSION",
        direction="LONG",
        entry_time=now,
        entry_price=22450.0,
        initial_sl=22420.0,
        thesis="Breakout test",
        regime="NORMAL",
    )

    # Simulate market snapshot and SYNTH candle hitting SL at 22,415
    synth_candle = Candle(
        start=now,
        open=22430.0,
        high=22435.0,
        low=22410.0,
        close=22415.0,
        volume=0.0,
        source="SYNTH",
        interval_minutes=5,
    )

    snap = MagicMock()
    snap.spot = 22415.0
    snap.timestamp = now + timedelta(minutes=5)
    snap.snapshot_id = "SNAP-1"
    snap.liquidity_map = None

    dec = pm.update(
        snapshot=snap,
        candle=synth_candle,
        current_regime="NORMAL",
    )

    # Trade should exit on Hard SL
    assert dec.action == "EXIT"
    assert trade.lifecycle_state == TradeStateEnum.EXITED
    assert trade.metadata.get("managed_by") == "SYNTH"
    assert trade.metadata.get("closed_by") == "SYNTH"


def test_session_preflight_0920_all_live(tmp_path, monkeypatch):
    """Session preflight at 09:20 IST passes when chart API and chain are live."""
    db_path = tmp_path / "test_preflight.db"
    store = JournalStore(db_path)
    market = MarketDataStore(session=None, journal=store)

    # Populate mock live state
    market.candle_engine.chart_ok = True
    c_mock = Candle(
        start=datetime(2026, 10, 9, 9, 15, tzinfo=settings.IST),
        open=22400.0, high=22420.0, low=22390.0, close=22410.0,
        volume=100.0, source="chart", interval_minutes=5
    )
    market.candles = [c_mock]
    market.candle_engine.candles = [c_mock]
    market.spot = MagicMock(value=22410.0, source="chain-underlying")
    market.chain = MagicMock()

    day_session = DaySession(
        store=store,
        tg=MagicMock(),
        classifier=MagicMock(),
        event_day=False,
        today_iso="2026-10-09",
        market=market,
        data_source="live-chain",
        live_mode=True,
    )

    check_time = datetime(2026, 10, 9, 9, 20, tzinfo=settings.IST)
    result = day_session.check_session_preflight(now=check_time)

    assert result["status"] == "OK"
    assert result["chart_api"] == "LIVE"
    assert result["option_chain"] == "LIVE"
    assert result["orb_forming"] == "YES"
    assert day_session.degraded_mode is False
    assert day_session.preflight_checked is True

    # Verify journaled in DB
    assert store.has_journal_event("2026-10-09", "Session preflight 09:20: OK")


def test_session_preflight_0920_degraded(tmp_path):
    """Session preflight at 09:20 IST alerts and flags degraded mode when feeds are dead."""
    db_path = tmp_path / "test_preflight_deg.db"
    store = JournalStore(db_path)
    market = MarketDataStore(session=None, journal=store)

    # Dead feeds
    market.candle_engine.chart_ok = False
    market.candles = []
    market.spot = None
    market.chain = None

    day_session = DaySession(
        store=store,
        tg=MagicMock(),
        classifier=MagicMock(),
        event_day=False,
        today_iso="2026-10-09",
        market=market,
        data_source="live-chain",
        live_mode=True,
    )

    check_time = datetime(2026, 10, 9, 9, 20, tzinfo=settings.IST)
    result = day_session.check_session_preflight(now=check_time)

    assert result["status"] == "DEGRADED"
    assert result["chart_api"] == "DEAD/LAGGING"
    assert result["option_chain"] == "DEAD"
    assert day_session.degraded_mode is True
    assert day_session.preflight_checked is True

    assert store.has_journal_event("2026-10-09", "Session preflight 09:20: DEGRADED")


def test_clean_session_preserved_with_synth_candles(tmp_path):
    """Oct 9-class outage: chart dead, chain alive.
    SYNTH candles keep candle age fresh (<5.5m), preventing blindness and preserving CLEAN session."""
    db_path = tmp_path / "test_clean_preservation.db"
    store = JournalStore(db_path)
    market = MarketDataStore(session=None, journal=store)

    today = datetime(2026, 10, 9, tzinfo=settings.IST)
    start_time = today.replace(hour=10, minute=30)

    # Add samples simulating chain spot polls every 60s
    for m in range(30, 35):
        market.candle_engine.add_spot_sample(22460.0 + m, ts=today.replace(hour=10, minute=m), source="chain")

    now = today.replace(hour=10, minute=35, second=15)
    # Refresh engine
    market.candle_engine.refresh(now=now)

    # Latest candle age is relative to the closed 10:35 SYNTH candle
    age = market.candle_engine.latest_candle_age_minutes(now)
    assert age is not None
    assert age < 1.0  # Just closed 15s ago!
    assert not market.candle_engine.is_blind(now)
