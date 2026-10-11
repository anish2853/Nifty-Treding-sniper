"""Phase 8.1 Counterfactual Simulation Engine.

Simulates the true chronological 58-session causal replay with an MSS gate for S3 in RANGE:
Diagnostic policy: When market_state is RANGE, S3 is only eligible if there is a valid 5m MSS body close.
Compares:
- Phase 8 Baseline (124 trades, -42.42R)
- S3 MSS Counterfactual Replay
"""
import json
import logging
from datetime import time, timedelta
from pathlib import Path
import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_sources.replay_data import prepare_replay_data, DayData
from data_sources.snapshot import MarketSnapshot, _compute_atr, _compute_ema, _compute_vwap
from engines.arbiter import Arbiter, RiskGovernor
from engines.candles import orb_levels, Candle
from engines.day_state import DayStateEngine
from engines.levels_lifecycle import LevelLifecycleManager, LevelRole
from engines.position_manager import PositionManager, TradeStateEnum
from engines.replay_unified_60day import ReplayTradeRecord, DailyReplaySummary
from engines.strategies import S1TrendExpansion, S2VwapMomentum, S3LiquidityReversal
from journal.integrity import assert_trade_lifecycle_invariants

log = logging.getLogger("phase8_1_cf")


def run_mss_counterfactual_replay(require_mss_displacement_in_range: bool = True):
    dataset = prepare_replay_data(days=60, print_table=False)
    valid_dates = sorted(dataset.valid_days.keys())

    all_trades: list[ReplayTradeRecord] = []
    prev_d_data: DayData | None = None

    for idx, trading_day in enumerate(valid_dates, 1):
        day_data = dataset.valid_days[trading_day]
        candles = day_data.five_minute
        if not candles or len(candles) < 70:
            continue

        day_state_engine = DayStateEngine()
        level_mgr = LevelLifecycleManager(max_tests_before_consumed=3)

        pdh = day_data.pdh or (prev_d_data.pdh if prev_d_data else None)
        pdl = day_data.pdl or (prev_d_data.pdl if prev_d_data else None)
        pdc = day_data.pdc or (prev_d_data.pdc if prev_d_data else None)

        day_start_ts = candles[0].start
        if pdh:
            level_mgr.register_level("PDH", float(pdh), "PDH", LevelRole.RESISTANCE, day_start_ts, strength="STRONG")
        if pdl:
            level_mgr.register_level("PDL", float(pdl), "PDL", LevelRole.SUPPORT, day_start_ts, strength="STRONG")

        s1 = S1TrendExpansion()
        s2 = S2VwapMomentum()
        s3 = S3LiquidityReversal()
        arbiter = Arbiter()
        gov = RiskGovernor(max_trades_per_day=3, max_trades_per_dir=2, profit_lock_r=2.0, consecutive_loss_limit=2)
        pm = PositionManager(hard_loss_cap_r=-1.1)

        completed_trades = []
        day_total_r = 0.0
        consecutive_losses = 0
        trades_today = 0
        trades_in_dir = {"LONG": 0, "SHORT": 0}
        square_off_time = time(15, 10)

        for i in range(len(candles)):
            cur_candle = candles[i]
            sub_candles = tuple(candles[: i + 1])
            now = cur_candle.end

            d_state = day_state_engine.update(sub_candles, spot=cur_candle.close, pdh=pdh, pdl=pdl)
            regime = d_state.state

            atr = _compute_atr(sub_candles, period=14) or 25.0
            vwap, _ = _compute_vwap(sub_candles)
            ema20 = _compute_ema([c.close for c in sub_candles], period=20)
            orb = orb_levels(sub_candles)

            snap = MarketSnapshot(
                snapshot_id=f"SNAP-{trading_day}-{i:03d}",
                timestamp=now,
                spot=cur_candle.close,
                candles=sub_candles,
                vwap=vwap,
                ema20=ema20,
                atr=atr,
                pdh=pdh,
                pdl=pdl,
                pdc=pdc,
                eq=((pdh + pdl) / 2.0) if (pdh and pdl) else None,
                orb=orb,
                is_blind=False,
            )

            level_mgr.update(snap, cur_candle)

            # Update Active Position
            if pm.active_trade and pm.active_trade.position_state != TradeStateEnum.EXITED:
                swing = sub_candles[-2].low if pm.active_trade.direction == "LONG" and len(sub_candles) >= 2 else (
                    sub_candles[-2].high if len(sub_candles) >= 2 else None
                )

                intrabar_exit = False
                trade = pm.active_trade
                if trade.direction == "LONG" and cur_candle.low <= trade.current_sl:
                    intrabar_exit = True
                    trade.exit_price = trade.current_sl - 2.0
                    trade.exit_reason = "HARD_SL"
                elif trade.direction == "SHORT" and cur_candle.high >= trade.current_sl:
                    intrabar_exit = True
                    trade.exit_price = trade.current_sl + 2.0
                    trade.exit_reason = "HARD_SL"

                is_square_off = now.time() >= square_off_time

                if intrabar_exit:
                    trade.exit_time = now
                    trade.transition_to(TradeStateEnum.EXITED)
                    exit_trigger = "HARD_SL"
                elif is_square_off:
                    trade.exit_price = cur_candle.close
                    trade.exit_time = now
                    trade.exit_reason = "SESSION_CLOSE"
                    trade.transition_to(TradeStateEnum.EXITED)
                    exit_trigger = "SESSION_CLOSE"
                else:
                    dec = pm.update(snap, candle=cur_candle, current_regime=regime, structure_swing=swing)
                    exit_trigger = dec.exit_trigger if dec.action in ("EXIT", "FULL_EXIT") else None

                if pm.active_trade.position_state == TradeStateEnum.EXITED:
                    t = pm.active_trade
                    t_exit_price = t.exit_price or cur_candle.close
                    risk_pts = t.one_r_pts if t.one_r_pts > 0 else 50.0

                    gross_pts = (t_exit_price - t.entry_price) if t.direction == "LONG" else (t.entry_price - t_exit_price)
                    gross_r = gross_pts / risk_pts
                    gross_r = max(-1.1, min(10.0, gross_r))

                    slippage_pts = 4.0
                    slippage_r = slippage_pts / risk_pts
                    cost_rs = 150.0
                    r_in_rs = risk_pts * 75.0
                    cost_r = cost_rs / r_in_rs if r_in_rs > 0 else 0.04
                    net_r = gross_r - slippage_r - cost_r

                    gross_pnl_rs = gross_pts * 75.0
                    net_pnl_rs = (gross_pts - slippage_pts) * 75.0 - cost_rs

                    rec = ReplayTradeRecord(
                        trade_id=t.trade_id,
                        date=trading_day.isoformat(),
                        strategy=t.strategy_id,
                        direction=t.direction,
                        entry_time=t.entry_time,
                        exit_time=t.exit_time or now,
                        entry_price=t.entry_price,
                        exit_price=t_exit_price,
                        initial_sl=t.initial_sl,
                        risk_pts=risk_pts,
                        exit_reason=t.exit_reason or exit_trigger or "EXIT",
                        thesis_status=t.thesis_status,
                        gross_r=round(gross_r, 2),
                        net_r=round(net_r, 2),
                        mfe_r=round(t.mfe_r, 2),
                        mae_r=round(t.mae_r, 2),
                        duration_mins=round((t.exit_time - t.entry_time).total_seconds() / 60.0, 1) if t.exit_time else 5.0,
                        regime=regime,
                        market_state=regime,
                        liquidity_at_entry="PDL" if t.direction == "SHORT" else "PDH",
                        liquidity_at_exit="TRAIL_SWING",
                        strategy_score=getattr(t, "score", 75.0),
                        arbiter_decision=f"{t.strategy_id}_{t.direction}",
                        risk_decision="APPROVED",
                        slippage_pts=slippage_pts,
                        slippage_r=round(slippage_r, 3),
                        cost_rs=cost_rs,
                        cost_r=round(cost_r, 3),
                        gross_pnl_rs=round(gross_pnl_rs, 2),
                        net_pnl_rs=round(net_pnl_rs, 2),
                    )
                    completed_trades.append(rec)
                    day_total_r += net_r
                    if net_r < 0:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0
                    pm.active_trade = None

            # Step B: Evaluate New Trades
            if pm.active_trade is None and now.time() < time(14, 30):
                a1 = s1.assess(snap, level_mgr, market_state=regime)
                a2 = s2.assess(snap, level_mgr, market_state=regime)
                s1_str = "HIGH" if (a1.eligible and a1.setup_score >= 80) else "LOW"
                a3 = s3.assess(snap, level_mgr, market_state=regime, s1_trend_strength=s1_str)

                # DIAGNOSTIC COUNTERFACTUAL FILTER:
                # If regime is RANGE and require_mss_displacement_in_range is True:
                # Require that a3 has a valid 5m MSS with displacement body close before taking S3 in RANGE
                if require_mss_displacement_in_range and "RANGE" in regime.upper() and a3.eligible:
                    # Evaluate MSS on sub_candles
                    c_trig = cur_candle
                    c_range = c_trig.high - c_trig.low
                    c_body = abs(c_trig.close - c_trig.open)
                    body_pct = (c_body / c_range * 100.0) if c_range > 0 else 0.0
                    
                    # Preceding swing (last 6 candles)
                    has_ref_swing = False
                    has_body_close = False
                    has_impulse = (body_pct >= 60.0 and c_range >= 12.0)
                    
                    if i >= 3:
                        pre_c = candles[max(0, i - 6): i]
                        if a3.direction == "LONG":
                            ref_swing = max(c.high for c in pre_c)
                            has_ref_swing = (ref_swing - c_trig.low) >= 15.0
                            has_body_close = c_trig.close > ref_swing
                        else:
                            ref_swing = min(c.low for c in pre_c)
                            has_ref_swing = (c_trig.high - ref_swing) >= 15.0
                            has_body_close = c_trig.close < ref_swing

                    # If no valid MSS displacement body close, suppress S3!
                    if not (has_ref_swing and has_body_close and has_impulse):
                        from dataclasses import replace
                        a3 = replace(a3, eligible=False, blocker_codes=tuple(list(a3.blocker_codes) + ["MSS_DISPLACEMENT_REQUIRED_IN_RANGE"]))

                intent, shadows, arb_reason = arbiter.evaluate([a1, a2, a3], market_state=regime)

                if intent is not None:
                    short_confluence = True
                    short_sc = 90
                    app, gov_reason, risk_u = gov.validate_intent(
                        intent=intent,
                        current_time=now,
                        day_total_r=day_total_r,
                        trades_today=trades_today,
                        trades_in_dir=trades_in_dir[intent.direction],
                        consecutive_losses=consecutive_losses,
                        is_blind=False,
                        is_event_day=False,
                        short_has_strong_confluence=short_confluence,
                        short_score=short_sc,
                        market_state=regime,
                    )

                    if app:
                        trades_today += 1
                        trades_in_dir[intent.direction] += 1
                        tid = f"TRD-{trading_day}-{trades_today}"
                        actual_fill = cur_candle.close + (2.0 if intent.direction == "LONG" else -2.0)
                        new_trade = pm.open_position(
                            trade_id=tid,
                            strategy_id=intent.strategy_id,
                            direction=intent.direction,
                            entry_time=now,
                            entry_price=actual_fill,
                            initial_sl=intent.initial_sl,
                            thesis=intent.reason_codes[0] if intent.reason_codes else "Strategy entry",
                            regime=regime,
                            target_plan=intent.target_plan,
                        )
                        new_trade.score = intent.setup_score if hasattr(intent, "setup_score") else 75.0

        all_trades.extend(completed_trades)
        prev_d_data = day_data

    # Calculate metrics
    cnt = len(all_trades)
    wins = [t for t in all_trades if t.net_r > 0]
    losses = [t for t in all_trades if t.net_r <= 0]
    wr = len(wins) / cnt * 100.0 if cnt else 0.0
    net_r = sum(t.net_r for t in all_trades)
    exp = net_r / cnt if cnt else 0.0

    # Max Drawdown and Loss Streak
    cum = 0.0
    pk = 0.0
    mdd = 0.0
    cur_streak = 0
    m_streak = 0
    for t in all_trades:
        cum += t.net_r
        if cum > pk:
            pk = cum
        d = pk - cum
        if d > mdd:
            mdd = d
        if t.net_r <= 0:
            cur_streak += 1
            m_streak = max(m_streak, cur_streak)
        else:
            cur_streak = 0

    trend_trades = [t for t in all_trades if t.regime != "RANGE"]
    range_trades = [t for t in all_trades if t.regime == "RANGE"]
    s1_trades = [t for t in all_trades if t.strategy == "S1_TREND_EXPANSION"]
    s2_trades = [t for t in all_trades if t.strategy == "S2_VWAP_MOMENTUM"]
    s3_trades = [t for t in all_trades if t.strategy == "S3_LIQUIDITY_REVERSAL"]
    s3_range = [t for t in s3_trades if t.regime == "RANGE"]
    s3_range_losses = [t for t in s3_range if t.net_r <= 0]

    return {
        "trades": cnt,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(wr, 1),
        "net_r": round(net_r, 2),
        "expectancy": round(exp, 2),
        "max_dd_r": round(mdd, 2),
        "max_streak": m_streak,
        "trend_count": len(trend_trades),
        "trend_net_r": round(sum(t.net_r for t in trend_trades), 2),
        "range_count": len(range_trades),
        "range_net_r": round(sum(t.net_r for t in range_trades), 2),
        "s1_count": len(s1_trades),
        "s1_net_r": round(sum(t.net_r for t in s1_trades), 2),
        "s2_count": len(s2_trades),
        "s2_net_r": round(sum(t.net_r for t in s2_trades), 2),
        "s3_count": len(s3_trades),
        "s3_net_r": round(sum(t.net_r for t in s3_trades), 2),
        "s3_range_count": len(s3_range),
        "s3_range_losses": len(s3_range_losses),
        "s3_range_net_r": round(sum(t.net_r for t in s3_range), 2),
    }


if __name__ == "__main__":
    print("\nRunning MSS Counterfactual Simulation...")
    cf_res = run_mss_counterfactual_replay(require_mss_displacement_in_range=True)
    print("MSS Counterfactual Results:")
    print(json.dumps(cf_res, indent=2))
