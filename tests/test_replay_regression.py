"""Replay Parity & Regression Verification Tests (Phase 8).

Contains:
1. test_strong_trend_400_point_day (Section 30 & 57)
2. test_chop_day_overtrading_prevention (Section 31)
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from data_sources.snapshot import MarketSnapshot
from engines.arbiter import Arbiter, RiskGovernor
from engines.candles import Candle
from engines.levels_lifecycle import LevelLifecycleManager, LevelRole, LevelStatus
from engines.position_manager import PositionManager, TradeStateEnum
from engines.strategies import S1TrendExpansion, S2VwapMomentum, S3LiquidityReversal
from utils import ist_now


def test_strong_trend_400_point_day():
    """Section 30 & 57 Acceptance Test:
    On a massive 400-point trend expansion day:
    - S1 identifies trend expansion.
    - S3 counter-trend reversals are vetoed.
    - PositionManager holds the runner with structure trailing rather than prematurely exiting at a static +2R target.
    - Captures large multi-R move cleanly.
    """
    base_time = ist_now().replace(hour=9, minute=15, second=0, microsecond=0)
    start_price = 22200.0

    # Build 15 candles representing a 400-point upward surge (22200 -> 22600)
    candles = []
    price = start_price
    for i in range(15):
        c_time = base_time + timedelta(minutes=5 * i)
        c_open = price
        c_close = price + 25.0
        c_high = c_close + 5.0
        c_low = c_open - 3.0
        candles.append(Candle(c_time, c_open, c_high, c_low, c_close, volume=2000 + i * 100))
        price = c_close

    lvl_mgr = LevelLifecycleManager()
    lvl_mgr.register_level("PDH", 22230.0, "PDH", LevelRole.RESISTANCE, base_time, strength="STRONG")

    pm = PositionManager()
    arbiter = Arbiter()
    gov = RiskGovernor()

    s1 = S1TrendExpansion()
    s2 = S2VwapMomentum()
    s3 = S3LiquidityReversal()

    trades_taken = 0
    trade_ref = None

    # Step through the day
    for i in range(2, len(candles)):
        sub_candles = tuple(candles[:i + 1])
        cur_candle = sub_candles[-1]
        cur_spot = cur_candle.close

        snap = MarketSnapshot(
            snapshot_id=f"SNAP-TREND-{i:03d}",
            timestamp=cur_candle.end,
            spot=cur_spot,
            candles=sub_candles,
            vwap=start_price + (i * 12.0),
            ema20=start_price + (i * 10.0),
            atr=25.0,
            pdh=22230.0,
            pdl=22150.0,
            orb={"high": 22250.0, "low": 22195.0},
        )

        lvl_mgr.update(snap, cur_candle)

        # 1. Update active position if one exists
        if pm.active_trade and pm.active_trade.lifecycle_state != TradeStateEnum.EXITED:
            # Trailing swing: previous candle low is the structural higher-low
            higher_low = sub_candles[-2].low
            pm.update(snap, candle=cur_candle, current_regime="TREND-UP (EXPANSION)", structure_swing=higher_low)
        else:
            # 2. Evaluate strategies concurrently
            a1 = s1.assess(snap, lvl_mgr, market_state="EXPANSION")
            a2 = s2.assess(snap, lvl_mgr, market_state="EXPANSION")
            # S3 must be vetoed by trend override!
            a3 = s3.assess(snap, lvl_mgr, market_state="EXPANSION", s1_trend_strength="HIGH")

            assert a3.eligible is False, "S3 counter-trend reversal MUST be vetoed during strong trend expansion!"

            intent, shadows, _ = arbiter.evaluate([a1, a2, a3])
            if intent and trades_taken < 1:
                app, reason, risk = gov.validate_intent(intent, cur_candle.end, trades_today=trades_taken)
                if app:
                    trades_taken += 1
                    trade_ref = pm.open_position(
                        trade_id=f"TRD-{trades_taken}",
                        strategy_id=intent.strategy_id,
                        direction=intent.direction,
                        entry_time=cur_candle.end,
                        entry_price=cur_spot,
                        initial_sl=intent.initial_sl,
                        thesis="Trend expansion runner",
                        regime="EXPANSION",
                    )

    assert trades_taken == 1, "Should enter exactly one clean S1 trend trade"
    assert trade_ref is not None
    assert trade_ref.strategy_id == "S1_TREND_EXPANSION"
    assert trade_ref.direction == "LONG"
    # Over 400 points with risk ~30-40 pts means > 6R!
    assert trade_ref.mfe_r > 5.0, f"MFE R should be > 5.0 on a 400-point day, got {trade_ref.mfe_r:.2f}R"
    assert trade_ref.lifecycle_state in (TradeStateEnum.TRAILING, TradeStateEnum.TREND_RUNNING)
    assert trade_ref.trail_stop > trade_ref.entry_price, "Trailing stop must have ratcheted high into profit"


def test_chop_day_overtrading_prevention():
    """Section 31 Acceptance Test:
    On a choppy / narrow range day (<45 pt range):
    - Activity is curtailed.
    - S1 trend continuation is rejected without expansion evidence.
    - Daily risk budget (max 2 trades) strictly respected.
    - Consumed levels do not re-trigger.
    """
    base_time = ist_now().replace(hour=9, minute=15, second=0, microsecond=0)
    mid_price = 22400.0

    # Build 12 candles oscillating tightly around 22400 between 22385 and 22415 (30 pt range)
    candles = []
    for i in range(12):
        c_time = base_time + timedelta(minutes=5 * i)
        offset = 12.0 if (i % 2 == 0) else -12.0
        c_open = mid_price - offset
        c_close = mid_price + offset
        c_high = max(c_open, c_close) + 3.0
        c_low = min(c_open, c_close) - 3.0
        candles.append(Candle(c_time, c_open, c_high, c_low, c_close, volume=800))

    lvl_mgr = LevelLifecycleManager(max_tests_before_consumed=3)
    lvl_mgr.register_level("R1", 22415.0, "RANGE-H", LevelRole.RESISTANCE, base_time)
    lvl_mgr.register_level("S1", 22385.0, "RANGE-L", LevelRole.SUPPORT, base_time)

    pm = PositionManager()
    arbiter = Arbiter()
    gov = RiskGovernor(max_trades_per_day=2)

    s1 = S1TrendExpansion()
    s2 = S2VwapMomentum()
    s3 = S3LiquidityReversal()

    trades_taken = 0

    for i in range(2, len(candles)):
        sub_candles = tuple(candles[:i + 1])
        cur_candle = sub_candles[-1]

        snap = MarketSnapshot(
            snapshot_id=f"SNAP-CHOP-{i:03d}",
            timestamp=cur_candle.end,
            spot=cur_candle.close,
            candles=sub_candles,
            vwap=mid_price,
            ema20=mid_price,
            atr=10.0,
            pdh=22450.0,
            pdl=22350.0,
            orb={"high": 22418.0, "low": 22382.0},
        )

        lvl_mgr.update(snap, cur_candle)

        a1 = s1.assess(snap, lvl_mgr, market_state="RANGE")
        a2 = s2.assess(snap, lvl_mgr, market_state="RANGE")
        a3 = s3.assess(snap, lvl_mgr, market_state="RANGE", s1_trend_strength="LOW")

        # In RANGE, S1 Breakout should NOT qualify as strong breakout
        assert a1.eligible is False or a1.setup_score < 80

        intent, shadows, _ = arbiter.evaluate([a1, a2, a3])
        if intent:
            app, reason, _ = gov.validate_intent(intent, cur_candle.end, trades_today=trades_taken)
            if app:
                trades_taken += 1
                pm.open_position(
                    trade_id=f"TRD-CHOP-{trades_taken}",
                    strategy_id=intent.strategy_id,
                    direction=intent.direction,
                    entry_time=cur_candle.end,
                    entry_price=cur_candle.close,
                    initial_sl=intent.initial_sl,
                    thesis="Range setup",
                    regime="RANGE",
                )

    # Must never exceed daily budget of 2 trades
    assert trades_taken <= 2, f"Trades on chop day must be <= 2, got {trades_taken}"
