"""Unit tests for PositionManager and Section 59 regression test."""
from datetime import datetime, timedelta, time as dtime
from types import SimpleNamespace

from engines.candles import Candle
from engines.position_manager import PositionManager, TradeStateEnum
from utils import ist_now


def test_trade_uses_current_state_not_original_target():
    """Section 59 Acceptance Test: A trade in a trend expansion does NOT
    exit simply because an old static target was touched. Instead it ratchets
    trailing stop with current market structure."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()

    # Entry: Long at 22400, SL at 22350 (Risk = 50 pts = 1R). Original target = 22450 (+1R)
    trade = pm.open_position(
        trade_id="TRD-001",
        strategy_id="S1_TREND_EXPANSION",
        direction="LONG",
        entry_time=now,
        entry_price=22400.0,
        initial_sl=22350.0,
        thesis="Trend breakout",
        regime="TREND-UP (EXPANSION)",
        original_target=22450.0,
    )

    assert trade.lifecycle_state == TradeStateEnum.ENTERED
    assert trade.one_r_pts == 50.0

    # Candle 1: Spot reaches 22460 (touches and exceeds original static target 22450)
    # Market is in EXPANSION: It must NOT exit! It moves to PROFIT_PROTECTION & TREND_RUNNING!
    c1 = Candle(now + timedelta(minutes=5), 22410, 22465, 22405, 22460)
    state, reason = pm.update(
        SimpleNamespace(spot=22460.0, timestamp=now + timedelta(minutes=5)),
        candle=c1,
        current_regime="TREND-UP (EXPANSION)",
    )

    assert state in (TradeStateEnum.PROFIT_PROTECTION, TradeStateEnum.TREND_RUNNING)
    assert trade.lifecycle_state != TradeStateEnum.EXITED, "Trade must NOT exit merely because static target was touched!"
    assert trade.current_sl == 22400.0, "SL should be moved to breakeven at +1R"

    # Candle 2: Trend explodes higher to 22580 (+3.6R!). Structure higher low forms at 22510.
    c2 = Candle(now + timedelta(minutes=10), 22460, 22590, 22455, 22580)
    state, reason = pm.update(
        SimpleNamespace(spot=22580.0, timestamp=now + timedelta(minutes=10)),
        candle=c2,
        current_regime="TREND-UP (EXPANSION)",
        structure_swing=22510.0,  # New structural swing low
    )

    assert trade.lifecycle_state == TradeStateEnum.TRAILING
    assert trade.current_sl == 22510.0, "Structure trail stop must ratchet to 22510!"
    assert trade.current_r > 3.0


def test_hard_loss_cap():
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager(hard_loss_cap_r=-1.1)

    trade = pm.open_position(
        trade_id="TRD-002",
        strategy_id="S1_TREND_EXPANSION",
        direction="LONG",
        entry_time=now,
        entry_price=22400.0,
        initial_sl=22350.0,
        thesis="Breakout",
        regime="NORMAL",
    )

    # Catastrophic gap down to 22300 (-2.0R)
    c1 = Candle(now + timedelta(minutes=5), 22300, 22310, 22290, 22300)
    state, reason = pm.update(
        SimpleNamespace(spot=22300.0, timestamp=now + timedelta(minutes=5)),
        candle=c1,
    )

    assert state == TradeStateEnum.EXITED
    assert trade.realized_r == -1.1, "Loss must be hard-capped at -1.1R!"
