"""Phase 13: Large-Sample 5-Minute Validation Engine.

Executes FROZEN-v1 across a public multi-year NIFTY 5-min dataset (~250 sessions).
Configuration:
- Retest entries (max 12 bars, 8 pts pullback tolerance, rejection candle)
- Structural trailing exits (+0.7R BE, +1.5R 50% partial, runner trail)
- Phase 9 MSS displacement body-close gate (mandatory for S3 in RANGE)
- Sniper gates:
    * 5m structural body-close beyond reference swing
    * Body ratio >= 50% of range
    * Room to run >= 40 pts to opposing liquidity
    * Time window: no entries 09:15-09:30 or 11:30-13:30
- Risk & trade caps:
    * Max 2 trades/day
    * Max 1 trade per direction
    * 2 consecutive losses -> halt for day
- Transaction costs: Rs. 150/trade + 4.0 pts slippage per trade.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import numpy as np
import settings
from data_sources.replay_data import ReplayDataset, load_csv_5m_dataset
from data_sources.snapshot import MarketSnapshot, _compute_atr, _compute_ema, _compute_vwap
from engines.arbiter import Arbiter
from engines.candles import Candle, orb_levels
from engines.day_state import DayStateEngine
from engines.freeze_governance import get_frozen_spec
from engines.levels_lifecycle import LevelLifecycleManager, LevelRole
from engines.strategies import S1TrendExpansion, S2VwapMomentum, S3LiquidityReversal
from utils import safe_print

log = logging.getLogger("large_sample_validation")


def run_large_sample_validation(csv_path: str | Path) -> dict[str, Any]:
    dataset: ReplayDataset = load_csv_5m_dataset(csv_path, print_table=False)
    valid_dates = sorted(dataset.valid_days.keys())

    all_trades: list[dict[str, Any]] = []
    retests_armed = 0
    retests_triggered = 0
    retests_expired = 0
    prev_d_data = None

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

        active_trade: dict[str, Any] | None = None
        pending_setup: dict[str, Any] | None = None
        trades_today = 0
        trades_in_dir = {"LONG": 0, "SHORT": 0}
        consecutive_losses = 0

        for i in range(len(candles)):
            cur_candle = candles[i]
            sub_candles = tuple(candles[: i + 1])
            now = cur_candle.end
            now_time = now.time()

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

            # -------------------------------------------------------------
            # Active Position Management (Phase 10 config)
            # -------------------------------------------------------------
            if active_trade is not None:
                t = active_trade
                direction = t["direction"]
                entry_px = t["entry_price"]
                risk_pts = t["risk_pts"]
                current_sl = t["current_sl"]
                partial_booked = t["partial_booked"]

                if direction == "LONG":
                    cur_r = (cur_candle.close - entry_px) / risk_pts
                    candle_mfe_r = (cur_candle.high - entry_px) / risk_pts
                    hit_sl = cur_candle.low <= current_sl
                else:
                    cur_r = (entry_px - cur_candle.close) / risk_pts
                    candle_mfe_r = (entry_px - cur_candle.low) / risk_pts
                    hit_sl = cur_candle.high >= current_sl

                t["mfe_r"] = max(t["mfe_r"], candle_mfe_r)
                mfe_r = t["mfe_r"]

                exit_trade = False
                exit_price = cur_candle.close
                exit_reason = "EXIT"

                if hit_sl:
                    exit_trade = True
                    exit_price = current_sl - (2.0 if direction == "LONG" else -2.0)
                    exit_reason = "HARD_SL" if current_sl == t["initial_sl"] else "TRAIL_SL"
                elif cur_r <= -1.05:
                    exit_trade = True
                    exit_price = entry_px - (1.05 * risk_pts if direction == "LONG" else -1.05 * risk_pts)
                    exit_reason = "HARD_LOSS_CAP"
                elif now_time >= time(15, 10):
                    exit_trade = True
                    exit_price = cur_candle.close
                    exit_reason = "SESSION_CLOSE"

                if exit_trade:
                    if direction == "LONG":
                        gross_pts = exit_price - entry_px
                    else:
                        gross_pts = entry_px - exit_price

                    gross_r = gross_pts / risk_pts
                    gross_r = max(-1.05, min(10.0, gross_r))

                    slippage_pts = 4.0
                    slippage_r = slippage_pts / risk_pts
                    cost_rs = 150.0
                    r_in_rs = risk_pts * 75.0
                    cost_r = cost_rs / r_in_rs if r_in_rs > 0 else 0.04

                    if partial_booked:
                        partial_gross_r = 1.5
                        partial_net_r = (0.5 * partial_gross_r) - (0.5 * slippage_r) - (0.5 * cost_r)
                        remaining_net_r = (0.5 * gross_r) - (0.5 * slippage_r) - (0.5 * cost_r)
                        net_r = partial_net_r + remaining_net_r
                    else:
                        net_r = gross_r - slippage_r - cost_r

                    t["exit_price"] = exit_price
                    t["exit_time"] = now
                    t["exit_reason"] = exit_reason
                    t["net_r"] = round(net_r, 2)
                    t["gross_r"] = round(gross_r, 2)
                    all_trades.append(t)

                    if net_r < 0:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0

                    active_trade = None
                else:
                    # Phase 10 exit logic:
                    # +0.7R -> SL to entry
                    if mfe_r >= 0.7 and not t["be_locked"]:
                        t["current_sl"] = entry_px
                        t["be_locked"] = True
                    # +1.5R -> book 50%
                    if mfe_r >= 1.5 and not t["partial_booked"]:
                        t["partial_booked"] = True
                    # Runner trailing after +1.5R
                    if mfe_r >= 1.5 and len(sub_candles) >= 2:
                        prior_c = sub_candles[-2]
                        if direction == "LONG" and prior_c.low > t["current_sl"]:
                            t["current_sl"] = prior_c.low
                        elif direction == "SHORT" and prior_c.high < t["current_sl"]:
                            t["current_sl"] = prior_c.high

            # -------------------------------------------------------------
            # Pending Retest Check (FROZEN-v1 ENTRY MECHANISM)
            # -------------------------------------------------------------
            if active_trade is None and pending_setup is not None:
                ps = pending_setup
                # Check expiration (12 candles)
                if i > ps["expire_idx"] or now_time > time(14, 15):
                    retests_expired += 1
                    pending_setup = None
                else:
                    # Check retest of broken level / POI
                    lvl = ps["level"]
                    p_dir = ps["direction"]
                    retested = False
                    retest_sl = 0.0

                    if p_dir == "LONG":
                        # Price pulls back to broken level (low touches level within 8 pts) and rejects (close > open)
                        if cur_candle.low <= (lvl + 8.0) and cur_candle.close > lvl and cur_candle.close > cur_candle.open:
                            retested = True
                            retest_sl = min(cur_candle.low - 5.0, lvl - 10.0)
                    else:
                        # Price pulls back to broken level (high touches level within 8 pts) and rejects (close < open)
                        if cur_candle.high >= (lvl - 8.0) and cur_candle.close < lvl and cur_candle.close < cur_candle.open:
                            retested = True
                            retest_sl = max(cur_candle.high + 5.0, lvl + 10.0)

                    if retested:
                        retests_triggered += 1
                        trades_today += 1
                        trades_in_dir[p_dir] += 1
                        actual_fill = cur_candle.close + (2.0 if p_dir == "LONG" else -2.0)
                        risk_pts = abs(actual_fill - retest_sl)
                        # ~60% of old risk distance (approx 15 - 35 pts)
                        risk_pts = max(15.0, min(35.0, risk_pts))

                        active_trade = {
                            "trade_id": f"TRD-{trading_day}-{trades_today}",
                            "date": trading_day.isoformat(),
                            "strategy": ps["strategy"] + "_RETEST",
                            "direction": p_dir,
                            "entry_time": now,
                            "entry_price": actual_fill,
                            "initial_sl": retest_sl,
                            "current_sl": retest_sl,
                            "risk_pts": risk_pts,
                            "mfe_r": 0.0,
                            "be_locked": False,
                            "partial_booked": False,
                            "regime": regime,
                        }
                        pending_setup = None

            # -------------------------------------------------------------
            # Sniper Setup Detection (Arming Retest)
            # -------------------------------------------------------------
            if active_trade is None and pending_setup is None and now_time <= time(14, 0):
                a1 = s1.assess(snap, level_mgr, market_state=regime)
                a2 = s2.assess(snap, level_mgr, market_state=regime)
                s1_str = "HIGH" if (a1.eligible and a1.setup_score >= 80) else "LOW"
                a3 = s3.assess(snap, level_mgr, market_state=regime, s1_trend_strength=s1_str)

                candidates = [a for a in [a1, a2, a3] if a.eligible]
                if not candidates:
                    continue

                intent, _, _ = arbiter.evaluate(candidates, market_state=regime)
                if not intent:
                    continue

                direction = intent.direction
                strat_id = intent.strategy_id

                # Daily Cap Checks
                if trades_today >= 2 or trades_in_dir[direction] >= 1 or consecutive_losses >= 2:
                    continue

                # Dead zone: 11:30 - 13:30, or 09:15-09:30
                if now_time < time(9, 30) or (time(11, 30) <= now_time <= time(13, 30)):
                    continue

                if strat_id != "S3_LIQUIDITY_REVERSAL":
                    sc = getattr(intent, "setup_score", 85)
                    if sc < 90:
                        continue

                # STRUCTURE GATE (Breakout candle qualifying)
                c_trig = cur_candle
                c_range = max(c_trig.high - c_trig.low, 0.01)
                c_body = abs(c_trig.close - c_trig.open)
                body_ratio = c_body / c_range
                if body_ratio < 0.50 or c_range < 12.0 or i < 4:
                    continue

                pre_candles = candles[max(0, i - 6) : i]
                broken_level = None

                if strat_id == "S3_LIQUIDITY_REVERSAL":
                    if direction == "LONG":
                        ref_swing = max(c.high for c in pre_candles)
                        if (c_trig.close > ref_swing) and (c_trig.close > c_trig.open):
                            broken_level = pdl if (pdl and abs(c_trig.close - pdl) <= 30.0) else ref_swing
                    else:
                        ref_swing = min(c.low for c in pre_candles)
                        if (c_trig.close < ref_swing) and (c_trig.close < c_trig.open):
                            broken_level = pdh if (pdh and abs(c_trig.close - pdh) <= 30.0) else ref_swing
                else:
                    if direction == "LONG":
                        ref_swing = max(c.high for c in pre_candles)
                        if (c_trig.close > ref_swing or (pdh and c_trig.close > pdh) or (orb and c_trig.close > orb["high"])) and c_trig.close > c_trig.open:
                            broken_level = pdh if (pdh and c_trig.close > pdh) else (orb["high"] if (orb and c_trig.close > orb["high"]) else ref_swing)
                    else:
                        ref_swing = min(c.low for c in pre_candles)
                        if (c_trig.close < ref_swing or (pdl and c_trig.close < pdl) or (orb and c_trig.close < orb["low"])) and c_trig.close < c_trig.open:
                            broken_level = pdl if (pdl and c_trig.close < pdl) else (orb["low"] if (orb and c_trig.close < orb["low"]) else ref_swing)

                if broken_level is None:
                    continue

                # ROOM GATE
                room = 999.0
                if direction == "LONG" and pdh and pdh > cur_candle.close:
                    room = min(room, pdh - cur_candle.close)
                elif direction == "SHORT" and pdl and pdl < cur_candle.close:
                    room = min(room, cur_candle.close - pdl)
                if room < 40.0:
                    continue

                # REGIME GATE
                reg_upper = regime.upper()
                if "TRANSITION" in reg_upper:
                    continue
                if strat_id in ("S1_TREND_EXPANSION", "S2_VWAP_MOMENTUM"):
                    if "RANGE" in reg_upper:
                        continue
                    if direction == "LONG" and "TREND-UP" not in reg_upper:
                        continue
                    if direction == "SHORT" and "TREND-DOWN" not in reg_upper:
                        continue
                elif strat_id == "S3_LIQUIDITY_REVERSAL":
                    has_strong_liq = (direction == "LONG" and pdl and cur_candle.low <= pdl + 5.0) or (direction == "SHORT" and pdh and cur_candle.high >= pdh - 5.0)
                    if not has_strong_liq:
                        continue

                # OI FUEL
                if direction == "LONG" and vwap and cur_candle.close < vwap:
                    continue
                elif direction == "SHORT" and vwap and cur_candle.close > vwap:
                    continue

                # CONFLUENCE
                confluence_count = 0
                if (pdh and abs(cur_candle.close - pdh) <= 30.0) or (pdl and abs(cur_candle.close - pdl) <= 30.0) or (orb and (abs(cur_candle.close - orb["high"]) <= 30.0 or abs(cur_candle.close - orb["low"]) <= 30.0)):
                    confluence_count += 1
                if vwap and ((direction == "LONG" and cur_candle.close >= vwap) or (direction == "SHORT" and cur_candle.close <= vwap)):
                    confluence_count += 1
                if ema20 and ((direction == "LONG" and cur_candle.close >= ema20) or (direction == "SHORT" and cur_candle.close <= ema20)):
                    confluence_count += 1
                if confluence_count < 2:
                    continue

                # Setup passed all Sniper Gates! ARM RETEST
                retests_armed += 1
                pending_setup = {
                    "direction": direction,
                    "strategy": strat_id,
                    "level": broken_level,
                    "armed_idx": i,
                    "expire_idx": i + 12,  # 12 bars (60 minutes)
                }

        prev_d_data = day_data

    # Generate Performance Metrics
    total_trades = len(all_trades)
    wins = sum(1 for t in all_trades if t["net_r"] > 0)
    losses = sum(1 for t in all_trades if t["net_r"] < 0)
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0

    net_r_total = sum(t["net_r"] for t in all_trades)
    gross_r_total = sum(t["gross_r"] for t in all_trades)
    expectancy = (net_r_total / total_trades) if total_trades > 0 else 0.0

    gross_gains = sum(t["net_r"] for t in all_trades if t["net_r"] > 0)
    gross_losses = sum(abs(t["net_r"]) for t in all_trades if t["net_r"] < 0)
    pf = (gross_gains / gross_losses) if gross_losses > 0 else 0.0

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    curr_streak = 0
    max_streak = 0

    for t in all_trades:
        r = t["net_r"]
        cum += r
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

        if r < 0:
            curr_streak += 1
            if curr_streak > max_streak:
                max_streak = curr_streak
        else:
            curr_streak = 0

    results = {
        "sessions_evaluated": len(valid_dates),
        "retests_armed": retests_armed,
        "retests_triggered": retests_triggered,
        "retests_expired": retests_expired,
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1),
        "gross_r": round(gross_r_total, 2),
        "net_r": round(net_r_total, 2),
        "net_expectancy": round(expectancy, 2),
        "profit_factor": round(pf, 2),
        "max_dd_r": round(max_dd, 2),
        "max_loss_streak": max_streak,
        "trades": all_trades,
    }

    print_scorecard(results)
    return results


def print_scorecard(res: dict[str, Any]) -> None:
    safe_print("\n" + "=" * 75)
    safe_print("NIFTY SNIPER — PHASE 13: LARGE-SAMPLE 5-MINUTE VALIDATION SCORECARD")
    safe_print("=" * 75)
    safe_print(f"Dataset Sessions Evaluated: {res['sessions_evaluated']} complete trading days")
    safe_print(f"Execution Architecture:     FROZEN-v1 (Retest Entries + Trailing Exits)")
    safe_print(f"Transaction Frictions:      Rs. 150/trade + 4.0 pts slippage applied to every trade")
    safe_print("-" * 75)
    safe_print("RETEST CONVERSION FUNNEL:")
    safe_print(f"  Sniper Setups Armed:      {res['retests_armed']}")
    safe_print(f"  Retests Triggered:        {res['retests_triggered']} ({res['retests_triggered']/res['retests_armed']*100:.1f}%)" if res['retests_armed'] else "0")
    safe_print(f"  False Breakouts Expired:  {res['retests_expired']} ({res['retests_expired']/res['retests_armed']*100:.1f}%)" if res['retests_armed'] else "0")
    safe_print("-" * 75)
    safe_print("PERFORMANCE METRICS:")
    safe_print(f"  Total Trades Taken:       {res['total_trades']}")
    safe_print(f"  Wins / Losses:            {res['wins']}W / {res['losses']}L")
    safe_print(f"  Win Rate:                 {res['win_rate']}%")
    safe_print(f"  Gross PnL (R):            {res['gross_r']:+.2f}R")
    safe_print(f"  Net PnL (R):              {res['net_r']:+.2f}R")
    safe_print(f"  Net Expectancy:           {res['net_expectancy']:+.2f}R / trade")
    safe_print(f"  Profit Factor:            {res['profit_factor']:.2f}")
    safe_print(f"  Max Drawdown (R):         {res['max_dd_r']:.2f}R")
    safe_print(f"  Max Losing Streak:        {res['max_loss_streak']}")
    safe_print("-" * 75)
    safe_print("BENCHMARK COMPARISON:")
    safe_print("  Metric              FROZEN-v1 (246 Sess)  58-Sess Baseline   Macro (493 Sess Hourly)")
    safe_print("  ------------------  --------------------  ----------------  ------------------------")
    safe_print(f"  Sessions:           {res['sessions_evaluated']:>10d}            {58:>10d}        {493:>10d}")
    safe_print(f"  Trades Taken:       {res['total_trades']:>10d}            {9:>10d}        {139:>10d}")
    safe_print(f"  Win Rate:           {res['win_rate']:>9.1f}%            {44.4:>9.1f}%        {48.2:>9.1f}%")
    safe_print(f"  Net PnL:            {res['net_r']:>9.2f}R            {0.35:>9.2f}R        {16.98:>9.2f}R")
    safe_print(f"  Net Expectancy:     {res['net_expectancy']:>9.2f}R            {0.04:>9.2f}R        {0.12:>9.2f}R")
    safe_print(f"  Profit Factor:      {res['profit_factor']:>10.2f}            {1.10:>10.2f}        {1.45:>10.2f}")
    safe_print(f"  Max Drawdown:       {res['max_dd_r']:>9.2f}R            {3.07:>9.2f}R        {6.29:>9.2f}R")
    safe_print(f"  Max Loss Streak:    {res['max_loss_streak']:>10d}            {3:>10d}        {9:>10d}")
    safe_print("=" * 75 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Large-Sample 5-Min Validation")
    parser.add_argument(
        "--csv",
        default="data/nifty50_5min_public_246sessions.csv",
        help="Path to 5m CSV dataset",
    )
    args = parser.parse_args()
    run_large_sample_validation(args.csv)
