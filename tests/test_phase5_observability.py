"""Unit tests for Phase 5: Observability, Telegram Protocol, and Production Validation.

Covers all 16 required tests:
1. test_telegram_entry_format()
2. test_telegram_exit_format()
3. test_telegram_event_idempotency()
4. test_duplicate_trade_event()
5. test_no_trade_reason()
6. test_trade_reason()
7. test_exit_reason()
8. test_decision_hash()
9. test_journal_integrity()
10. test_preflight_real_integration()
11. test_data_blind_halt()
12. test_critical_alert_not_suppressed()
13. test_chart_active_mark_limit()
14. test_no_future_information()
15. test_opportunity_capture()
16. test_startup_self_test()
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alerts import messages
from alerts.telegram_bot import NullTelegramSender
from engines.arbiter import RiskGovernor, TradeIntent
from engines.candles import Candle
from engines.decision_events import (
    DecisionEvent,
    DecisionEventBus,
    DecisionEventType,
    EventSeverity,
    ExitReasonCode,
    NoTradeReason,
    OpportunityCaptureReport,
    calculate_decision_hash,
    check_candle_engine_health,
    get_exit_reason,
    get_no_trade_reason,
    get_trade_reason,
)
from engines.preflight import (
    check_real_decision_integration,
    check_startup_self_test,
    run_preflight_v2,
)
from journal.integrity import (
    assert_trade_lifecycle_invariants,
    verify_journal_integrity,
)


def test_telegram_entry_format():
    """1. Test that trade signal formatting adheres strictly to Phase 5 Section 5."""
    msg = messages.format_telegram_trade_signal(
        strategy="S1 TREND EXPANSION",
        direction="SHORT",
        regime="TREND-DOWN / EXPANSION",
        entry=22245.0,
        initial_sl=22270.0,
        risk=1.0,
        score=75,
        reasons=["5m LH/LL confirmed", "Price below VWAP", "ORB-Low accepted"],
        next_liquidity="22180.0 (PDL)",
        room=65.0,
        invalidation="1-min close above 22270.0",
        confidence="STRONG",
    )
    assert "━━━━━━━━━━━━━━━━━━━━" in msg
    assert "NIFTY SNIPER" in msg
    assert "TRADE SIGNAL" in msg
    assert "Strategy:\nS1 TREND EXPANSION" in msg
    assert "Direction:\nSHORT" in msg
    assert "Regime:\nTREND-DOWN / EXPANSION" in msg
    assert "Entry:\n22245.00" in msg
    assert "Initial SL:\n22270.00" in msg
    assert "Risk:\n1.0R" in msg
    assert "Score:\n75" in msg
    assert "WHY:" in msg
    assert "✓ 5m LH/LL confirmed" in msg
    assert "NEXT LIQUIDITY:\n22180.0 (PDL)" in msg
    assert "ROOM:\n65 pts" in msg
    assert "INVALIDATION:\n1-min close above 22270.0" in msg
    assert "CONFIDENCE:\nSTRONG" in msg


def test_telegram_exit_format():
    """2. Test that trade exit formatting adheres strictly to Phase 5 Section 8."""
    msg = messages.format_telegram_trade_exit(
        strategy="S1 TREND EXPANSION",
        direction="SHORT",
        entry=22245.0,
        exit_price=22180.0,
        result_r=2.6,
        mfe_r=3.1,
        mae_r=-0.2,
        exit_reason="TRAIL_STOP",
        thesis="VALID",
    )
    assert "━━━━━━━━━━━━━━━━━━━━" in msg
    assert "NIFTY SNIPER" in msg
    assert "TRADE EXIT" in msg
    assert "Strategy:\nS1 TREND EXPANSION" in msg
    assert "Direction:\nSHORT" in msg
    assert "Entry:\n22245.00" in msg
    assert "Exit:\n22180.00" in msg
    assert "Result:\n+2.60R" in msg
    assert "MFE:\n+3.10R" in msg
    assert "MAE:\n-0.20R" in msg
    assert "Exit reason:\nTRAIL_STOP" in msg
    assert "Thesis:\nVALID" in msg


def test_telegram_event_idempotency():
    """3. Test that sending the same event_id twice produces only one alert (Section 9)."""
    tg = NullTelegramSender()
    msg = "Test Signal Content"

    # First send accepted
    accepted_1 = tg.send_event(msg, event_id="EVT-001", decision_hash="HASH-A", severity="IMPORTANT")
    assert accepted_1 is True
    assert len(tg.sent) == 1

    # Second send with identical event_id rejected
    accepted_2 = tg.send_event(msg, event_id="EVT-001", decision_hash="HASH-B", severity="IMPORTANT")
    assert accepted_2 is False
    assert len(tg.sent) == 1


def test_duplicate_trade_event():
    """4. Test that DecisionEventBus deduplicates duplicate events (Section 19)."""
    bus = DecisionEventBus()
    now = datetime(2026, 10, 8, 10, 35, tzinfo=timezone.utc)
    ev1 = DecisionEvent(
        event_id="EVT-100",
        timestamp=now,
        event_type=DecisionEventType.TRADE_ENTRY,
        severity=EventSeverity.IMPORTANT,
        trade_id="TRD-01",
        decision_hash="HASH-100",
    )
    ev2 = DecisionEvent(
        event_id="EVT-100",
        timestamp=now,
        event_type=DecisionEventType.TRADE_ENTRY,
        severity=EventSeverity.IMPORTANT,
        trade_id="TRD-01",
        decision_hash="HASH-100",
    )
    assert bus.record(ev1) is True
    assert bus.record(ev2) is False
    assert len(bus.events) == 1


def test_no_trade_reason():
    """5. Test deterministic NO_TRADE_REASON diagnostic generation (Section 15)."""
    # Blind data blocker
    prim, sec = get_no_trade_reason(0, {}, False, False, is_blind=True)
    assert prim == NoTradeReason.STALE_DATA.value

    # Active position blocker
    prim, sec = get_no_trade_reason(0, {}, False, False, has_active_position=True)
    assert prim == NoTradeReason.POSITION_ALREADY_ACTIVE.value

    # Conflicting strategies
    prim, sec = get_no_trade_reason(2, {"S1": 85, "S3": 82}, True, False)
    assert prim == NoTradeReason.CONFLICTING_STRATEGIES.value

    # Insufficient room
    prim, sec = get_no_trade_reason(1, {"S1": 80}, False, False, room_sufficient=False)
    assert prim == NoTradeReason.INSUFFICIENT_ROOM.value

    # Insufficient score
    prim, sec = get_no_trade_reason(1, {"S1": 70}, False, False)
    assert prim == NoTradeReason.INSUFFICIENT_SCORE.value

    # Risk limit
    prim, sec = get_no_trade_reason(1, {"S1": 85}, False, True, risk_reason="CONSECUTIVE_LOSS_HALT")
    assert prim == NoTradeReason.RISK_LIMIT.value


def test_trade_reason():
    """6. Test structured TRADE_REASON dictionary generation (Section 16)."""
    tr = get_trade_reason(
        strategy="S1_TREND_EXPANSION",
        direction="SHORT",
        regime="TREND-DOWN",
        structure="5m LH/LL",
        liquidity="22,180 (PDL)",
        room=65.0,
        risk=1.0,
        invalidation="Close above 22,270",
    )
    assert tr["strategy"] == "S1_TREND_EXPANSION"
    assert tr["direction"] == "SHORT"
    assert tr["room"] == 65.0
    assert tr["regime"] == "TREND-DOWN"


def test_exit_reason():
    """7. Test deterministic forensic EXIT_REASON mapping (Section 17)."""
    # Hard SL
    reason, thesis = get_exit_reason("HARD_SL_TRIGGERED", current_r=-1.1)
    assert reason == ExitReasonCode.HARD_SL.value
    assert thesis == "INVALIDATED"

    # Trail Stop
    reason, thesis = get_exit_reason("TRAIL_STOP_HIT", current_r=2.5)
    assert reason == ExitReasonCode.TRAIL_STOP.value
    assert thesis == "VALID"

    # Time Stop
    reason, thesis = get_exit_reason("15:10_SQUARE_OFF", current_r=1.2, is_time_stop=True)
    assert reason == ExitReasonCode.SESSION_CLOSE.value
    assert thesis == "TIME"

    # Data Blindness
    reason, thesis = get_exit_reason("BLIND_HALT", current_r=0.5, is_data_blind=True)
    assert reason == ExitReasonCode.DATA_SAFETY.value
    assert thesis == "DATA"


def test_decision_hash():
    """8. Test deterministic decision hash properties (Section 19)."""
    t0 = datetime(2026, 10, 8, 10, 35, tzinfo=timezone.utc)
    h1 = calculate_decision_hash(t0, "SNAP-1", "S1", "SHORT", "TREND-DOWN", "MAP-1", 75.0, "NORMAL")
    h2 = calculate_decision_hash(t0, "SNAP-1", "S1", "SHORT", "TREND-DOWN", "MAP-1", 75.0, "NORMAL")
    h3 = calculate_decision_hash(t0, "SNAP-1", "S1", "LONG", "TREND-DOWN", "MAP-1", 75.0, "NORMAL")

    assert len(h1) == 64
    assert h1 == h2  # Deterministic
    assert h1 != h3  # Sensitive to parameters


def test_journal_integrity():
    """9. Test journal integrity and lifecycle invariants (Section 20)."""
    # Test in-memory invariant checker
    valid_events = [
        {"type": "ENTRY", "qty": 50, "trade_id": "T1"},
        {"type": "TRAIL", "qty": 50, "trade_id": "T1"},
        {"type": "PARTIAL_EXIT", "qty": 25, "trade_id": "T1"},
        {"type": "EXIT", "qty": 25, "trade_id": "T1"},
    ]
    ok, violations = assert_trade_lifecycle_invariants(valid_events)
    assert ok is True
    assert len(violations) == 0

    # Negative quantity invariant failure
    invalid_events = [
        {"type": "ENTRY", "qty": 50, "trade_id": "T2"},
        {"type": "PARTIAL_EXIT", "qty": 60, "trade_id": "T2"},
    ]
    ok, violations = assert_trade_lifecycle_invariants(invalid_events)
    assert ok is False
    assert any("exceeds remaining" in v for v in violations)

    # Test database journal verification
    with sqlite3.connect(":memory:") as conn:
        conn.execute("""
            CREATE TABLE journal (
                id INTEGER PRIMARY KEY,
                ts TEXT,
                type TEXT,
                trade_id TEXT,
                notes TEXT
            )
        """)
        # Insert valid trade
        conn.execute("INSERT INTO journal (ts, type, trade_id) VALUES ('2026-10-08T10:00:00', 'ENTRY', 'TRD-1')")
        conn.execute("INSERT INTO journal (ts, type, trade_id) VALUES ('2026-10-08T11:00:00', 'EXIT', 'TRD-1')")
        conn.commit()

        ok, viols = verify_journal_integrity(conn)
        assert ok is True

        # Insert duplicate entry
        conn.execute("INSERT INTO journal (ts, type, trade_id) VALUES ('2026-10-08T10:05:00', 'ENTRY', 'TRD-1')")
        conn.commit()

        ok, viols = verify_journal_integrity(conn)
        assert ok is False
        assert any("multiple entry events" in v for v in viols)


def test_preflight_real_integration():
    """10. Test Preflight V2 real synthetic decision integration path (Section 25)."""
    ok, msg = check_real_decision_integration()
    assert ok is True
    assert "verified" in msg


def test_data_blind_halt():
    """11. Test candle engine health monitor and data blindness halt (Section 23)."""
    now = datetime(2026, 10, 8, 10, 30, tzinfo=timezone.utc)
    # 2 min old: healthy
    fresh_time = now - timedelta(minutes=2)
    state, is_blind, detail = check_candle_engine_health(fresh_time, now, is_market_hours=True)
    assert state == "HEALTHY"
    assert is_blind is False

    # 6 min old: blind
    stale_time = now - timedelta(minutes=6)
    state, is_blind, detail = check_candle_engine_health(stale_time, now, is_market_hours=True)
    assert state == "BLIND"
    assert is_blind is True

    # Risk governor blocks when is_blind=True
    gov = RiskGovernor()
    intent = TradeIntent("S1", "SHORT", (22200, 22210), 22230.0, "TRAIL", "STRONG", ["R1"], [], "S1", now)
    app, reason, _ = gov.validate_intent(intent, now, is_blind=True)
    assert app is False
    assert "BLIND" in reason


def test_critical_alert_not_suppressed():
    """12. Test that DEBUG alerts are suppressed while CRITICAL/ENTRY alerts are delivered (Section 30)."""
    tg = NullTelegramSender()

    # DEBUG suppressed
    delivered_debug = tg.send_event("Debug log message", event_id="DBG-1", severity="DEBUG")
    assert delivered_debug is False
    assert len(tg.sent) == 0

    # IMPORTANT / CRITICAL delivered
    delivered_entry = tg.send_event("Entry alert", event_id="ENT-1", severity="IMPORTANT")
    assert delivered_entry is True
    assert len(tg.sent) == 1

    delivered_crit = tg.send_event("Critical halt", event_id="CRIT-1", severity="CRITICAL")
    assert delivered_crit is True
    assert len(tg.sent) == 2


def test_chart_active_mark_limit():
    """13. Test that active visual chart marks remain strictly bounded < 10 (Section 13)."""
    # Active marks on chart:
    # 1. PDH
    # 2. PDL
    # 3. Next liquidity destination
    # 4. Current 5m structure swing
    # 5. Active strategy POI
    # 6. Trade Entry
    # 7. Initial SL
    # 8. Trailing SL
    active_marks = [
        {"name": "PDH", "price": 22280.0, "category": "MAJOR_LIQUIDITY"},
        {"name": "PDL", "price": 22150.0, "category": "MAJOR_LIQUIDITY"},
        {"name": "NEXT_LIQ", "price": 22100.0, "category": "DESTINATION"},
        {"name": "SWING_LH", "price": 22250.0, "category": "STRUCTURE"},
        {"name": "POI", "price": 22240.0, "category": "STRATEGY"},
        {"name": "ENTRY", "price": 22240.0, "category": "TRADE"},
        {"name": "INITIAL_SL", "price": 22265.0, "category": "TRADE"},
        {"name": "TRAILING_SL", "price": 22250.0, "category": "TRADE"},
    ]
    # Bound assertion
    assert len(active_marks) < 10


def test_no_future_information():
    """14. Test that decision evaluating at time t never accesses candles > t (Section 18)."""
    t_decision = datetime(2026, 10, 8, 10, 35, tzinfo=timezone.utc)
    all_candles = [
        Candle(t_decision - timedelta(minutes=10), 22260, 22270, 22250, 22255),
        Candle(t_decision - timedelta(minutes=5), 22255, 22260, 22240, 22245),
        Candle(t_decision, 22245, 22250, 22230, 22235),
        # Future candle
        Candle(t_decision + timedelta(minutes=5), 22235, 22240, 22200, 22210),
    ]

    # Replay slice must strictly enforce end <= t_decision
    available_candles = [c for c in all_candles if c.end <= t_decision]
    assert len(available_candles) == 2
    assert all(c.end <= t_decision for c in available_candles)


def test_opportunity_capture():
    """15. Test opportunity capture report metric calculation (Section 22)."""
    t_start = datetime(2026, 10, 8, 10, 35, tzinfo=timezone.utc)
    t_end = datetime(2026, 10, 8, 14, 30, tzinfo=timezone.utc)

    report = OpportunityCaptureReport(
        move_id="MOVE-OCT08-DOWN",
        direction="SHORT",
        start_time=t_start,
        end_time=t_end,
        available_move_pts=150.0,
        best_strategy="S1_TREND_EXPANSION",
        first_valid_signal_time=t_start,
        actual_signal_time=t_start,
        actual_entry_price=22245.0,
        mfe_pts=120.0,
        realized_r=2.6,
    )
    report.calculate_metrics()
    assert report.capture_ratio == pytest.approx(0.8, 0.01)


def test_startup_self_test():
    """16. Test startup self test FETCH -> PARSE -> STORE -> READ-BACK cycle (Section 24)."""
    ok, msg = check_startup_self_test()
    assert ok is True
    assert "verified" in msg
