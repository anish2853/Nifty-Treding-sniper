"""Test possible trades across October 2026 and late September 2026 sessions."""
import sys
from datetime import time
from pathlib import Path

ROOT_DIR = Path(r"c:\Users\anish\Desktop\trading")
sys.path.insert(0, str(ROOT_DIR))

from data_sources.replay_data import prepare_replay_data
from data_sources.snapshot import MarketSnapshot, _compute_atr, _compute_ema, _compute_vwap
from engines.arbiter import Arbiter
from engines.candles import orb_levels
from engines.day_state import DayStateEngine
from engines.levels_lifecycle import LevelLifecycleManager, LevelRole
from engines.strategies import S1TrendExpansion, S2VwapMomentum, S3LiquidityReversal

dataset = prepare_replay_data(days=60, print_table=False)
valid_dates = sorted(dataset.valid_days.keys())

# Focus on recent days: late September and October 2026
recent_dates = [d for d in valid_dates if (d.month == 10 and d.year == 2026) or (d.month == 9 and d.year == 2026 and d.day >= 24)]

print("=================================================================")
print(f"TESTING POSSIBLE TRADES ACROSS {len(recent_dates)} RECENT SESSIONS:")
print("Dates:", [d.isoformat() for d in recent_dates])
print("=================================================================")

# Configuration 1: Standard Active Entries (Score >= 75)
trades_c1 = []
for d in recent_dates:
    day_data = dataset.valid_days[d]
    candles = day_data.five_minute
    day_state_engine = DayStateEngine()
    level_mgr = LevelLifecycleManager(max_tests_before_consumed=3)
    pdh, pdl, pdc = day_data.pdh, day_data.pdl, day_data.pdc
    if pdh: level_mgr.register_level("PDH", float(pdh), "PDH", LevelRole.RESISTANCE, candles[0].start, strength="STRONG")
    if pdl: level_mgr.register_level("PDL", float(pdl), "PDL", LevelRole.SUPPORT, candles[0].start, strength="STRONG")

    s1, s2, s3 = S1TrendExpansion(), S2VwapMomentum(), S3LiquidityReversal()
    arbiter = Arbiter()

    active_trade = None
    trades_today = 0
    trades_dir = {"LONG": 0, "SHORT": 0}

    for i, c in enumerate(candles):
        sub_c = tuple(candles[: i + 1])
        now = c.end
        now_time = now.time()
        d_state = day_state_engine.update(sub_c, spot=c.close, pdh=pdh, pdl=pdl)
        regime = d_state.state
        atr = _compute_atr(sub_c, period=14) or 25.0
        vwap, _ = _compute_vwap(sub_c)
        ema20 = _compute_ema([x.close for x in sub_c], period=20)
        orb = orb_levels(sub_c)
        snap = MarketSnapshot(
            snapshot_id=f"{d}-{i}", timestamp=c.end, spot=c.close, candles=sub_c,
            vwap=vwap, ema20=ema20, atr=atr, pdh=pdh, pdl=pdl, pdc=pdc, orb=orb, is_blind=False
        )
        level_mgr.update(snap, c)

        # Check Active Trade Exit
        if active_trade is not None:
            t = active_trade
            direction = t["direction"]
            entry_px = t["entry_price"]
            risk_pts = t["risk_pts"]
            current_sl = t["current_sl"]

            if direction == "LONG":
                mfe_r = (c.high - entry_px) / risk_pts
                hit_sl = c.low <= current_sl
            else:
                mfe_r = (entry_px - c.low) / risk_pts
                hit_sl = c.high >= current_sl

            t["mfe_r"] = max(t["mfe_r"], mfe_r)

            exit_now = False
            exit_px = c.close
            exit_reason = "EXIT"

            if hit_sl:
                exit_now = True
                exit_px = current_sl
                exit_reason = "HARD_SL" if current_sl == t["initial_sl"] else "TRAIL_SL"
            elif now_time >= time(15, 10):
                exit_now = True
                exit_px = c.close
                exit_reason = "SESSION_CLOSE"

            if exit_now:
                pts = (exit_px - entry_px) if direction == "LONG" else (entry_px - exit_px)
                r = pts / risk_pts
                net_r = r - (4.0 / risk_pts) - 0.04
                t["exit_time"] = now
                t["exit_spot"] = exit_px
                t["exit_reason"] = exit_reason
                t["result_r"] = round(net_r, 2)
                trades_c1.append(t)
                active_trade = None
            else:
                if t["mfe_r"] >= 0.7:
                    t["current_sl"] = entry_px
                if t["mfe_r"] >= 1.5 and len(sub_c) >= 2:
                    prior = sub_c[-2]
                    if direction == "LONG" and prior.low > t["current_sl"]:
                        t["current_sl"] = prior.low
                    elif direction == "SHORT" and prior.high < t["current_sl"]:
                        t["current_sl"] = prior.high

        # Check Entry
        if active_trade is None and trades_today < 2 and time(9, 30) <= now_time <= time(14, 30):
            a1 = s1.assess(snap, level_mgr, market_state=regime)
            a2 = s2.assess(snap, level_mgr, market_state=regime)
            s1_str = "HIGH" if (a1.eligible and a1.setup_score >= 80) else "LOW"
            a3 = s3.assess(snap, level_mgr, market_state=regime, s1_trend_strength=s1_str)
            candidates = [a for a in [a1, a2, a3] if a.eligible and a.setup_score >= 75]
            if candidates:
                intent, _, _ = arbiter.evaluate(candidates, market_state=regime)
                if intent and trades_dir[intent.direction] < 1:
                    p_dir = intent.direction
                    entry_px = c.close + (2.0 if p_dir == "LONG" else -2.0)
                    sl_px = intent.initial_sl or (entry_px - 25.0 if p_dir == "LONG" else entry_px + 25.0)
                    risk_pts = max(15.0, min(40.0, abs(entry_px - sl_px)))
                    active_trade = {
                        "date": d.isoformat(),
                        "family": intent.strategy_id,
                        "direction": p_dir,
                        "entry_time": now,
                        "entry_price": entry_px,
                        "initial_sl": sl_px,
                        "current_sl": sl_px,
                        "risk_pts": risk_pts,
                        "mfe_r": 0.0,
                    }
                    trades_today += 1
                    trades_dir[p_dir] += 1

print("\n--- CONFIG 1: ACTIVE CANDIDATE SETUPS (Score >= 75) ---")
print(f"Total Trades: {len(trades_c1)}")
wins = [t for t in trades_c1 if t["result_r"] > 0]
losses = [t for t in trades_c1 if t["result_r"] <= 0]
win_rate = (len(wins) / len(trades_c1) * 100) if trades_c1 else 0
net_pnl = sum(t["result_r"] for t in trades_c1)
print(f"Win Rate:     {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)")
print(f"Net PnL:      {net_pnl:+.2f}R")
for t in trades_c1:
    print(f"  {t['date']} {t['entry_time'].strftime('%H:%M')} {t['family']} {t['direction']} -> {t['exit_reason']} MFE={t['mfe_r']:.2f}R NetR={t['result_r']:+.2f}R")
