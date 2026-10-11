"""Phase 8 Replay Validation Script.
Executes the true chronological candle-by-candle 58-session replay
with RANGE_GATE active and prints all required comparison metrics.
"""
import json
import logging
from pathlib import Path
import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engines.replay_unified_60day import run_full_60day_validation

log = logging.getLogger("phase8_val")


def run_phase8_validation():
    print("="*80)
    print("RUNNING TRUE CAUSAL 58-SESSION REPLAY (PHASE 8 RANGE_GATE)")
    print("="*80)

    result = run_full_60day_validation(days=60)
    trades = result.trades

    print(f"\nCompleted sessions: {len(result.daily_summaries)}")
    print(f"Total trades taken: {len(trades)}")

    wins = [t for t in trades if t.net_r > 0]
    losses = [t for t in trades if t.net_r <= 0]
    wr = len(wins) / len(trades) * 100.0 if trades else 0.0
    net_r = sum(t.net_r for t in trades)
    net_exp = net_r / len(trades) if trades else 0.0
    gross_r = sum(t.gross_r for t in trades)
    gross_exp = gross_r / len(trades) if trades else 0.0

    print(f"Wins: {len(wins)}, Losses: {len(losses)}, Win Rate: {wr:.1f}%")
    print(f"Total Gross R: {gross_r:.2f}R (Exp: {gross_exp:.2f}R)")
    print(f"Total Net R: {net_r:.2f}R (Exp: {net_exp:.2f}R)")
    print(f"Max Drawdown: {result.max_drawdown_r:.2f}R ({result.max_drawdown_pct:.1f}%)")
    print(f"Max Loss Streak: {result.max_losing_streak}")
    print(f"Avg MFE: {result.avg_mfe_r:.2f}R, Avg MAE: {result.avg_mae_r:.2f}R")

    # Regime Breakdown
    trend_trades = [t for t in trades if t.regime != "RANGE"]
    range_trades = [t for t in trades if t.regime == "RANGE"]

    def stats_for(sublist, name=""):
        if not sublist:
            return {
                "name": name, "trades": 0, "wins": 0, "losses": 0,
                "wr": 0.0, "net_r": 0.0, "exp": 0.0, "max_dd": 0.0, "max_streak": 0
            }
        w = [t for t in sublist if t.net_r > 0]
        l = [t for t in sublist if t.net_r <= 0]
        nr = sum(t.net_r for t in sublist)
        cum = 0.0
        peak = 0.0
        mdd = 0.0
        streak = 0
        mstreak = 0
        for t in sublist:
            cum += t.net_r
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > mdd:
                mdd = dd
            if t.net_r <= 0:
                streak += 1
                mstreak = max(mstreak, streak)
            else:
                streak = 0
        return {
            "name": name,
            "trades": len(sublist),
            "wins": len(w),
            "losses": len(l),
            "wr": round(len(w) / len(sublist) * 100.0, 1),
            "net_r": round(nr, 2),
            "exp": round(nr / len(sublist), 2),
            "max_dd": round(mdd, 2),
            "max_streak": mstreak,
        }

    trend_s = stats_for(trend_trades, "TREND")
    range_s = stats_for(range_trades, "RANGE")

    print("\nRegime Breakdown:")
    print(f"  TREND: {trend_s}")
    print(f"  RANGE: {range_s}")

    # Strategy Breakdown
    s1_trades = [t for t in trades if t.strategy == "S1_TREND_EXPANSION"]
    s2_trades = [t for t in trades if t.strategy == "S2_VWAP_MOMENTUM"]
    s3_trades = [t for t in trades if t.strategy == "S3_LIQUIDITY_REVERSAL"]

    s1_s = stats_for(s1_trades, "S1_TREND_EXPANSION")
    s2_s = stats_for(s2_trades, "S2_VWAP_MOMENTUM")
    s3_s = stats_for(s3_trades, "S3_LIQUIDITY_REVERSAL")

    print("\nStrategy Breakdown:")
    print(f"  S1: {s1_s}")
    print(f"  S2: {s2_s}")
    print(f"  S3: {s3_s}")

    # S2 in RANGE check:
    s2_in_range = [t for t in s2_trades if t.regime == "RANGE"]
    print(f"\n>>> CRITICAL CHECK: S2 trades taken in RANGE = {len(s2_in_range)} <<<")

    # Strategy by Regime cross-table
    print("\nStrategy x Regime Cross Table:")
    for strat_name, s_list in [("S1_TREND_EXPANSION", s1_trades), ("S2_VWAP_MOMENTUM", s2_trades), ("S3_LIQUIDITY_REVERSAL", s3_trades)]:
        tr_list = [t for t in s_list if t.regime != "RANGE"]
        rg_list = [t for t in s_list if t.regime == "RANGE"]
        print(f"  {strat_name}:")
        print(f"    In TREND: count={len(tr_list)}, net_r={sum(t.net_r for t in tr_list):.2f}R")
        print(f"    In RANGE: count={len(rg_list)}, net_r={sum(t.net_r for t in rg_list):.2f}R")

    return {
        "total": stats_for(trades, "TOTAL"),
        "trend": trend_s,
        "range": range_s,
        "s1": s1_s,
        "s2": s2_s,
        "s3": s3_s,
        "s2_in_range_count": len(s2_in_range),
    }


if __name__ == "__main__":
    run_phase8_validation()
