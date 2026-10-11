"""Unit tests for SignalLifecycle and Section 60 regression test."""
from datetime import timedelta
from engines.signals import SignalLifecycle, SignalStatus
from utils import ist_now


def test_expired_signal_cannot_execute():
    """Section 60 Acceptance Test: A signal past valid_until CANNOT execute
    even if price reaches the entry zone."""
    now = ist_now()
    sig = SignalLifecycle(
        signal_id="SIG-001",
        strategy_id="S1_TREND_EXPANSION",
        created_at=now,
        valid_until=now + timedelta(minutes=10),
        market_snapshot_id="SNAP-001",
        direction="LONG",
        entry_zone=(22480.0, 22500.0),
        sl_spot=22450.0,
        target_plan="STRUCTURE_TRAIL",
        score=85,
        status=SignalStatus.ENTRY_PENDING,
    )

    # 1. Inside validity window (5 min later) and inside entry zone -> can execute!
    t1 = now + timedelta(minutes=5)
    assert sig.is_expired(t1) is False
    assert sig.can_execute(t1, current_spot=22490.0) is True

    # 2. Outside validity window (15 min later) and inside entry zone -> CANNOT execute!
    t2 = now + timedelta(minutes=15)
    assert sig.is_expired(t2) is True
    assert sig.can_execute(t2, current_spot=22490.0) is False
    assert sig.status == SignalStatus.EXPIRED


def test_signal_execution():
    now = ist_now()
    sig = SignalLifecycle(
        signal_id="SIG-002",
        strategy_id="S3_LIQUIDITY_REVERSAL",
        created_at=now,
        valid_until=now + timedelta(minutes=15),
        market_snapshot_id="SNAP-002",
        direction="SHORT",
        entry_zone=(22500.0, 22515.0),
        sl_spot=22535.0,
        target_plan="OPPOSITE_POOL",
        score=92,
        status=SignalStatus.ENTRY_PENDING,
    )

    t_exec = now + timedelta(minutes=3)
    assert sig.can_execute(t_exec, current_spot=22508.0) is True
    sig.mark_executed(t_exec, fill_price=22508.0)
    assert sig.status == SignalStatus.EXECUTED
    assert sig.execution_price == 22508.0
