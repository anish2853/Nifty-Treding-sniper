"""Two-Year Hourly Resolution Replay Variant (Strategy-Existence Test).

Uses yfinance 60m data across 2 years (~490 trading days):
1. First-Hour ORB: 09:15-10:15 IST defines Opening Range High (ORB-H) and Low (ORB-L).
2. Hourly Displacement: Subsequent candles (10:15 - 14:15) evaluate closed candle breaks
   with institutional displacement (body >= 50% of hourly range).
3. Evaluates both breakout and retest mechanics across a massive multi-year macro dataset.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytz
import yfinance as yf

from utils import safe_print

log = logging.getLogger("replay_hourly")
IST = pytz.timezone("Asia/Kolkata")


@dataclass
class HourlyTradeRecord:
    trade_id: str
    date: str
    direction: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    initial_sl: float
    risk_pts: float
    gross_r: float
    net_r: float
    exit_reason: str
    mfe_r: float


@dataclass
class HourlyReplaySummary:
    total_sessions: int
    trades_taken: int
    wins: int
    losses: int
    win_rate: float
    net_pnl_r: float
    net_expectancy_r: float
    profit_factor: float
    max_dd_r: float
    max_streak: int
    trades: list[HourlyTradeRecord]


def fetch_hourly_2y_data(symbol: str = "^NSEI") -> pd.DataFrame:
    """Fetch 2 years of 60-minute data from yfinance."""
    ticker = yf.Ticker(symbol)
    df = ticker.history(period="2y", interval="1h")
    if df.empty:
        log.error("Failed to fetch 2y hourly data for %s", symbol)
        return df

    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)

    return df


def run_hourly_2y_replay(df: pd.DataFrame | None = None, mode: str = "retest") -> HourlyReplaySummary:
    """Execute the 2-year hourly strategy-existence test.
    
    mode: 'breakout' | 'retest'
    """
    if df is None or df.empty:
        df = fetch_hourly_2y_data()

    if df.empty:
        return HourlyReplaySummary(0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, [])

    # Group by trading date
    day_groups = df.groupby(df.index.date)
    sorted_dates = sorted(day_groups.groups.keys())

    trades: list[HourlyTradeRecord] = []
    total_sessions = len(sorted_dates)

    for d in sorted_dates:
        day_df = day_groups.get_group(d).sort_index()
        # Must have at least 5 hourly bars during market hours
        if len(day_df) < 5:
            continue

        # Candle 0: 09:15 - 10:15 IST -> First Hour ORB
        first_bar = day_df.iloc[0]
        orb_h = float(first_bar["High"])
        orb_l = float(first_bar["Low"])
        orb_range = orb_h - orb_l

        # Volatility filter on first hour
        if orb_range < 30.0:
            continue

        traded_today = False
        trade_dir = None
        entry_px = 0.0
        init_sl = 0.0
        risk_pts = 0.0
        entry_idx = -1

        pending_retest = None

        # Subsequent hours: Candle 1 to Candle N-1
        for idx in range(1, len(day_df)):
            bar = day_df.iloc[idx]
            bar_ts = day_df.index[idx]
            bar_o = float(bar["Open"])
            bar_h = float(bar["High"])
            bar_l = float(bar["Low"])
            bar_c = float(bar["Close"])
            bar_range = max(bar_h - bar_l, 0.01)
            bar_body = abs(bar_c - bar_o)
            body_ratio = bar_body / bar_range

            if traded_today:
                break

            # If waiting for retest
            if pending_retest is not None:
                p_dir = pending_retest["direction"]
                target_lvl = pending_retest["level"]
                retested = False

                if p_dir == "LONG":
                    # Pullback to ORB-H and bounce
                    if bar_l <= (target_lvl + 15.0) and bar_c > target_lvl and bar_c > bar_o:
                        retested = True
                        entry_px = bar_c
                        init_sl = min(bar_l - 10.0, target_lvl - 25.0)
                else:
                    # Pullback to ORB-L and reject
                    if bar_h >= (target_lvl - 15.0) and bar_c < target_lvl and bar_c < bar_o:
                        retested = True
                        entry_px = bar_c
                        init_sl = max(bar_h + 10.0, target_lvl + 25.0)

                if retested:
                    trade_dir = p_dir
                    entry_idx = idx
                    risk_pts = max(30.0, min(80.0, abs(entry_px - init_sl)))
                    traded_today = True
                    break
                elif idx >= pending_retest["expire_idx"]:
                    pending_retest = None

            # Look for initial hourly breakout with displacement (body >= 50%)
            if not traded_today and pending_retest is None and idx <= 4:
                is_long_disp = (bar_c > orb_h) and (bar_c > bar_o) and (body_ratio >= 0.50)
                is_short_disp = (bar_c < orb_l) and (bar_c < bar_o) and (body_ratio >= 0.50)

                if is_long_disp:
                    if mode == "breakout":
                        traded_today = True
                        trade_dir = "LONG"
                        entry_px = bar_c
                        init_sl = orb_h - (0.5 * orb_range)
                        risk_pts = max(30.0, min(80.0, abs(entry_px - init_sl)))
                        entry_idx = idx
                        break
                    else:
                        pending_retest = {"direction": "LONG", "level": orb_h, "expire_idx": idx + 3}

                elif is_short_disp:
                    if mode == "breakout":
                        traded_today = True
                        trade_dir = "SHORT"
                        entry_px = bar_c
                        init_sl = orb_l + (0.5 * orb_range)
                        risk_pts = max(30.0, min(80.0, abs(entry_px - init_sl)))
                        entry_idx = idx
                        break
                    else:
                        pending_retest = {"direction": "SHORT", "level": orb_l, "expire_idx": idx + 3}

        # If a trade was entered, simulate through remaining session bars to 15:30 EOD
        if traded_today and trade_dir:
            last_bar = day_df.iloc[-1]
            exit_px = float(last_bar["Close"])
            exit_reason = "SESSION_CLOSE"
            max_mfe = 0.0

            # Step through remaining candles
            for fut_idx in range(entry_idx + 1, len(day_df)):
                f_bar = day_df.iloc[fut_idx]
                f_h = float(f_bar["High"])
                f_l = float(f_bar["Low"])

                if trade_dir == "LONG":
                    cur_mfe = (f_h - entry_px) / risk_pts
                    max_mfe = max(max_mfe, cur_mfe)
                    if f_l <= init_sl:
                        exit_px = init_sl
                        exit_reason = "HARD_SL"
                        break
                else:
                    cur_mfe = (entry_px - f_l) / risk_pts
                    max_mfe = max(max_mfe, cur_mfe)
                    if f_h >= init_sl:
                        exit_px = init_sl
                        exit_reason = "HARD_SL"
                        break

            if trade_dir == "LONG":
                gross_pts = exit_px - entry_px
            else:
                gross_pts = entry_px - exit_px

            gross_r = gross_pts / risk_pts
            # Friction: 4 pts slippage + ₹150 transaction cost
            slippage_r = 4.0 / risk_pts
            cost_r = 150.0 / (risk_pts * 75.0)
            net_r = gross_r - slippage_r - cost_r

            trades.append(HourlyTradeRecord(
                trade_id=f"H-TRD-{d.isoformat()}",
                date=d.isoformat(),
                direction=trade_dir,
                entry_time=day_df.index[entry_idx].strftime("%H:%M"),
                entry_price=round(entry_px, 1),
                exit_time=day_df.index[-1].strftime("%H:%M"),
                exit_price=round(exit_px, 1),
                initial_sl=round(init_sl, 1),
                risk_pts=round(risk_pts, 1),
                gross_r=round(gross_r, 2),
                net_r=round(net_r, 2),
                exit_reason=exit_reason,
                mfe_r=round(max_mfe, 2),
            ))

    total_tr = len(trades)
    wins = [t for t in trades if t.net_r > 0]
    losses = [t for t in trades if t.net_r <= 0]
    wr = (len(wins) / total_tr * 100.0) if total_tr > 0 else 0.0
    tot_pnl = sum(t.net_r for t in trades)
    exp = (tot_pnl / total_tr) if total_tr > 0 else 0.0

    gross_wins = sum(t.net_r for t in wins)
    gross_losses = abs(sum(t.net_r for t in losses))
    pf = (gross_wins / gross_losses) if gross_losses > 0 else 0.0

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    streak = 0
    max_streak = 0

    for t in trades:
        equity += t.net_r
        if equity > peak: peak = equity
        dd = peak - equity
        if dd > max_dd: max_dd = dd
        if t.net_r <= 0:
            streak += 1
            if streak > max_streak: max_streak = streak
        else:
            streak = 0

    return HourlyReplaySummary(
        total_sessions=total_sessions,
        trades_taken=total_tr,
        wins=len(wins),
        losses=len(losses),
        win_rate=round(wr, 1),
        net_pnl_r=round(tot_pnl, 2),
        net_expectancy_r=round(exp, 2),
        profit_factor=round(pf, 2),
        max_dd_r=round(max_dd, 2),
        max_streak=max_streak,
        trades=trades,
    )


if __name__ == "__main__":
    safe_print("Fetching 2y hourly data and running macro strategy-existence test...")
    df = fetch_hourly_2y_data()
    safe_print(f"Loaded {len(df)} hourly candles.")

    res_retest = run_hourly_2y_replay(df, mode="retest")
    safe_print("\n============================================================")
    safe_print("2-YEAR HOURLY REPLAY (RETEST MODE) — STRATEGY EXISTENCE TEST")
    safe_print("============================================================")
    safe_print(f"Total Sessions:    {res_retest.total_sessions}")
    safe_print(f"Trades Taken:      {res_retest.trades_taken}")
    safe_print(f"Wins / Losses:     {res_retest.wins}W / {res_retest.losses}L")
    safe_print(f"Win Rate:          {res_retest.win_rate}%")
    safe_print(f"Net PnL (R):       {res_retest.net_pnl_r:+.2f}R")
    safe_print(f"Net Expectancy:    {res_retest.net_expectancy_r:+.2f}R")
    safe_print(f"Profit Factor:     {res_retest.profit_factor}")
    safe_print(f"Max Drawdown (R):  {res_retest.max_dd_r:.2f}R")
    safe_print(f"Max Losing Streak: {res_retest.max_streak}")
    safe_print("============================================================")

    res_bo = run_hourly_2y_replay(df, mode="breakout")
    safe_print("\n============================================================")
    safe_print("2-YEAR HOURLY REPLAY (BREAKOUT MODE)")
    safe_print("============================================================")
    safe_print(f"Trades Taken:      {res_bo.trades_taken}")
    safe_print(f"Win Rate:          {res_bo.win_rate}%")
    safe_print(f"Net PnL (R):       {res_bo.net_pnl_r:+.2f}R")
    safe_print(f"Net Expectancy:    {res_bo.net_expectancy_r:+.2f}R")
    safe_print(f"Profit Factor:     {res_bo.profit_factor}")
    safe_print(f"Max Drawdown (R):  {res_bo.max_dd_r:.2f}R")
    safe_print("============================================================")
