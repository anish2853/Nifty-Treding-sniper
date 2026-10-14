"""Tests for Streamlit Visibility Upgrade & Funnel Decoder."""
import sqlite3
from datetime import datetime, time
import pytest
from dashboard.funnel_decoder import (
    check_heartbeat_health,
    decode_activity_funnel,
    get_forward_progress_data,
)


def test_heartbeat_health_off_hours():
    # Saturday at 11:00 AM IST
    sat_dt = datetime(2026, 10, 10, 11, 0, 0)
    is_healthy, msg = check_heartbeat_health(candle_age_min=10.0, chain_age_min=15.0, now_dt=sat_dt)
    assert is_healthy is True
    assert "OFF-HOURS" in msg


def test_heartbeat_health_market_hours_fresh():
    # Wednesday at 10:15 AM IST
    wed_dt = datetime(2026, 10, 7, 10, 15, 0)
    is_healthy, msg = check_heartbeat_health(candle_age_min=2.0, chain_age_min=1.5, now_dt=wed_dt)
    assert is_healthy is True
    assert "HEALTHY" in msg


def test_heartbeat_health_market_hours_blind():
    # Wednesday at 10:15 AM IST with stale candles (> 5 min)
    wed_dt = datetime(2026, 10, 7, 10, 15, 0)
    is_healthy, msg = check_heartbeat_health(candle_age_min=7.5, chain_age_min=2.0, now_dt=wed_dt)
    assert is_healthy is False
    assert "Candle age: 7.5m" in msg


def test_decode_activity_funnel_blank_day():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE journal (id INTEGER PRIMARY KEY, ts TEXT, type TEXT, direction TEXT, strike REAL, spot REAL, score REAL, reasons_json TEXT, exit_reason TEXT, notes TEXT)")
    
    # Insert sample skips for today
    today_iso = "2026-10-09"
    conn.execute("INSERT INTO journal (ts, type, score, notes) VALUES (?, 'SKIP', 60, 'structure break failure: candle body < 50%')", (today_iso + "T09:45:00",))
    conn.execute("INSERT INTO journal (ts, type, score, notes) VALUES (?, 'SKIP', 85, 'signal blocked by regime: RANGE day suppresses continuation')", (today_iso + "T10:05:00",))
    conn.execute("INSERT INTO journal (ts, type, score, notes) VALUES (?, 'SKIP', 75, 'signal blocked by time: 11:30 dead-zone')", (today_iso + "T11:45:00",))
    conn.execute("INSERT INTO journal (ts, type, score, notes) VALUES (?, 'SKIP', 80, 'no room: opposing wall < 40 pts')", (today_iso + "T13:45:00",))
    conn.execute("INSERT INTO journal (ts, type, score, notes) VALUES (?, 'SKIP', 90, 'retest armed at PDH 25000')", (today_iso + "T10:15:00",))
    conn.execute("INSERT INTO journal (ts, type, score, notes) VALUES (?, 'SKIP', 90, 'retest expired after 12 bars')", (today_iso + "T11:15:00",))

    funnel = decode_activity_funnel(conn, today_iso)
    assert funnel["candidates"] == 6
    assert funnel["fired"] == 0
    assert funnel["gates"]["structure"] == 1
    assert funnel["gates"]["regime"] == 1
    assert funnel["gates"]["time"] == 1
    assert funnel["gates"]["room"] == 1
    assert len(funnel["armed"]) == 1
    assert funnel["expired"] == 1
    assert "WHY BLANK" in funnel["summary_text"]
    assert "1 structure" in funnel["summary_text"]
    assert "1 regime" in funnel["summary_text"]
    assert "1 time" in funnel["summary_text"]
    assert "1 room" in funnel["summary_text"]


def test_decode_activity_funnel_active_day():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE journal (id INTEGER PRIMARY KEY, ts TEXT, type TEXT, direction TEXT, strike REAL, spot REAL, score REAL, reasons_json TEXT, exit_reason TEXT, notes TEXT)")
    today_iso = "2026-10-09"
    conn.execute("INSERT INTO journal (ts, type, direction, strike, spot, score) VALUES (?, 'SIGNAL', 'LONG', 25000, 25005, 95)", (today_iso + "T10:30:00",))
    conn.execute("INSERT INTO journal (ts, type, score, notes) VALUES (?, 'SKIP', 60, 'structure break failure')", (today_iso + "T09:45:00",))

    funnel = decode_activity_funnel(conn, today_iso)
    assert funnel["candidates"] == 2
    assert funnel["fired"] == 1
    assert "ACTIVITY FUNNEL" in funnel["summary_text"]
    assert "1 fired" in funnel["summary_text"]


def test_get_forward_progress_data():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE journal (id INTEGER PRIMARY KEY, ts TEXT, type TEXT, direction TEXT, result_r REAL, exit_reason TEXT)")
    conn.execute("INSERT INTO journal (ts, type, direction, result_r, exit_reason) VALUES ('2026-10-09T10:00:00', 'EXIT', 'LONG', 1.2, 'TRAIL_SL')")
    conn.execute("INSERT INTO journal (ts, type, direction, result_r, exit_reason) VALUES ('2026-10-09T14:00:00', 'EXIT', 'SHORT', -0.5, 'HARD_SL')")

    res = get_forward_progress_data(conn)
    assert res["target_sessions"] == 60
    assert res["target_trades_checkpoint1"] == 20
    assert res["trades_taken"] == 2
    assert res["win_rate"] == 50.0
    assert res["net_r"] == 0.7
    assert res["real_money_gate"] == "BLOCKED"
    assert res["system_status"] == "PAPER ONLY"
    assert res["config_version"] == "FROZEN-v1"


def test_dashboard_historical_backtest_data():
    """Verify that get_historical_backtest_data loads all 189 sessions with both arms."""
    from dashboard.app import get_historical_backtest_data
    df = get_historical_backtest_data()
    assert not df.empty
    assert len(df) >= 200
    expected_cols = ["Date", "Time", "Trade ID", "Arm", "Direction", "Instrument", "Spot Entry", "Spot SL", "Spot Exit", "Buy Prem", "SL Prem", "Exit Prem", "Realized R", "Outcome", "Exit Reason", "Setup", "Notes"]
    for col in expected_cols:
        assert col in df.columns

    # Verify both Arms are present and firing
    arm1_trades = df[df["Arm"].str.contains("Arm 1")]
    arm2_trades = df[df["Arm"].str.contains("Arm 2")]
    assert len(arm1_trades) > 50, f"Expected Arm 1 sweeps > 50, got {len(arm1_trades)}"
    assert len(arm2_trades) > 50, f"Expected Arm 2 TL-FIB > 50, got {len(arm2_trades)}"

    # Verify overall performance
    total_trades = len(df)
    wins = len(df[df["Realized R"] > 0])
    win_rate = wins / total_trades * 100.0
    total_r = df["Realized R"].sum()
    assert win_rate >= 60.0, f"Expected win rate >= 60%, got {win_rate:.1f}%"
    assert total_r > 80.0, f"Expected total R > +80R, got {total_r:.2f}R"

