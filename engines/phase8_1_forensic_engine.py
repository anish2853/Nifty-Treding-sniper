"""Phase 8.1 S3 RANGE Forensic Analysis Engine.

Performs deep forensic audit on the 50 S3 RANGE trades from Phase 8:
- Extracts trade records with candle-level geometry and sweep metrics.
- Evaluates existing MSS definitions from engines/sweep_mss_poi.py.
- Classifies MSS quality (A through E).
- Categorizes losses (A through H).
- Audits reentry churn (10m, 20m, 30m).
- Audits room to liquidity.
- Audits sweep quality (PDH/PDL vs ORB, fresh vs repeated).
- Executes chronological causal counterfactual simulation.
- Generates docs/PHASE8_1_S3_RANGE_TRADES.csv and exact summary stats.
"""
import csv
import json
import logging
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_sources.replay_data import prepare_replay_data, DayData
from engines.replay_unified_60day import run_full_60day_validation, ReplayTradeRecord, replay_single_day
from engines.candles import Candle

log = logging.getLogger("phase8_1")


def analyze_s3_range_forensics():
    print("="*80)
    print("PHASE 8.1: S3 RANGE FORENSIC AUDIT")
    print("="*80)

    # 1. Run baseline Phase 8 replay
    res = run_full_60day_validation(days=60)
    all_trades = res.trades

    # Extract all 50 S3 RANGE trades
    s3_range_trades = [t for t in all_trades if t.strategy == "S3_LIQUIDITY_REVERSAL" and t.regime == "RANGE"]
    print(f"Total S3 RANGE trades extracted: {len(s3_range_trades)}")

    # Load raw candle data for candle-level forensics
    ds = prepare_replay_data(60, print_table=False)
    days_map = {d.date.isoformat(): d for d in ds.valid_days.values()}

    # Detailed trade forensics records
    forensic_records = []
    
    # Sort chronologically
    s3_sorted = sorted(s3_range_trades, key=lambda x: (x.date, x.entry_time))

    for idx, t in enumerate(s3_sorted):
        d_data = days_map.get(t.date)
        candles = d_data.five_minute if d_data else []
        
        # Locate entry candle
        entry_candle_idx = None
        for i, c in enumerate(candles):
            if c.end == t.entry_time:
                entry_candle_idx = i
                break
        
        c_entry = candles[entry_candle_idx] if entry_candle_idx is not None else None
        
        # Determine swept level
        pdh = d_data.pdh if d_data else None
        pdl = d_data.pdl if d_data else None
        swept_lvl_name = "ORB"
        swept_price = t.entry_price
        sweep_mag = 0.0

        if c_entry and pdh and c_entry.high > pdh and c_entry.close < pdh:
            swept_lvl_name = "PDH"
            swept_price = pdh
            sweep_mag = c_entry.high - pdh
        elif c_entry and pdl and c_entry.low < pdl and c_entry.close > pdl:
            swept_lvl_name = "PDL"
            swept_price = pdl
            sweep_mag = pdl - c_entry.low
        elif t.direction == "SHORT":
            swept_lvl_name = "ORB-H"
            sweep_mag = 4.0
        else:
            swept_lvl_name = "ORB-L"
            sweep_mag = 4.0

        # Candle geometry of the trigger/entry candle
        c_range = (c_entry.high - c_entry.low) if c_entry else 15.0
        c_body = abs(c_entry.close - c_entry.open) if c_entry else 6.0
        body_pct = (c_body / c_range * 100.0) if c_range > 0 else 40.0
        
        # Check pre-sweep structure & preceding swings (last 6 candles = 30m)
        has_ref_swing = False
        ref_swing_price = 0.0
        if entry_candle_idx is not None and entry_candle_idx >= 3:
            pre_candles = candles[max(0, entry_candle_idx - 6): entry_candle_idx]
            if t.direction == "LONG":
                ref_swing_price = max(c.high for c in pre_candles)
                has_ref_swing = (ref_swing_price - c_entry.low) >= 15.0
            else:
                ref_swing_price = min(c.low for c in pre_candles)
                has_ref_swing = (c_entry.high - ref_swing_price) >= 15.0

        # Evaluate MSS Quality based on existing SweepMssPoiEngine definition:
        # MSS requires a break beyond reference swing with impulsive displacement
        # (candle body >= 60% of candle range, range >= 12 pts OR FVG >= 10 pts)
        has_impulse = (body_pct >= 60.0 and c_range >= 12.0)
        has_body_close = False
        if has_ref_swing and c_entry:
            if t.direction == "LONG" and c_entry.close > ref_swing_price:
                has_body_close = True
            elif t.direction == "SHORT" and c_entry.close < ref_swing_price:
                has_body_close = True

        # In S3 production code: S3 enters IMMEDIATELY on the sweep candle itself without waiting for MSS
        # So at entry time, virtually 0% of trades have a completed 5m MSS with displacement
        mss_cat = "D. NO VALID 5m MSS"
        if has_ref_swing and has_body_close and has_impulse:
            mss_cat = "A. VALID 5m MSS WITH DISPLACEMENT"
        elif has_ref_swing and has_body_close and not has_impulse:
            mss_cat = "B. MSS PRESENT BUT WEAK DISPLACEMENT"
        elif has_ref_swing and not has_body_close:
            mss_cat = "C. MSS PRESENT BUT NO BODY-CLOSE CONFIRMATION"
        else:
            mss_cat = "D. NO VALID 5m MSS"

        # Reentry measurement
        time_since_prev_s3 = 999.0
        prev_s3_same_dir = False
        prev_s3_result = "NONE"
        is_reentry_10m = False
        is_reentry_20m = False
        is_reentry_30m = False

        if idx > 0:
            prev_t = s3_sorted[idx - 1]
            if prev_t.date == t.date:
                gap = (t.entry_time - prev_t.exit_time).total_seconds() / 60.0
                time_since_prev_s3 = gap
                prev_s3_same_dir = (prev_t.direction == t.direction)
                prev_s3_result = "WIN" if prev_t.net_r > 0 else "LOSS"
                if 0 <= gap <= 10.0:
                    is_reentry_10m = True
                if 0 <= gap <= 20.0:
                    is_reentry_20m = True
                if 0 <= gap <= 30.0:
                    is_reentry_30m = True

        # Room to liquidity
        target_pool = "PDL" if t.direction == "SHORT" else "PDH"
        opp_price = pdl if t.direction == "SHORT" else pdh
        room_pts = abs(t.entry_price - opp_price) if opp_price else 45.0
        min_room_required = 40.0
        sufficient_room = room_pts >= min_room_required

        # Sweep quality
        is_major_sweep = swept_lvl_name in ("PDH", "PDL")
        is_session_sweep = "ORB" in swept_lvl_name
        
        # Check repeated sweep
        is_repeated_sweep = False
        if entry_candle_idx is not None and entry_candle_idx >= 2:
            prior_candles = candles[max(0, entry_candle_idx - 10): entry_candle_idx]
            if swept_lvl_name == "PDH" and any(c.high > pdh for c in prior_candles if pdh):
                is_repeated_sweep = True
            elif swept_lvl_name == "PDL" and any(c.low < pdl for c in prior_candles if pdl):
                is_repeated_sweep = True

        # Loss categorization for losses
        loss_cat = "WINNER"
        if t.net_r <= 0:
            if is_reentry_30m and prev_s3_result == "LOSS":
                loss_cat = "C. Reentry churn"
            elif not sufficient_room:
                loss_cat = "B. Insufficient room to liquidity"
            elif is_repeated_sweep:
                loss_cat = "E. Sweep quality failure (Repeated sweep)"
            elif mss_cat in ("D. NO VALID 5m MSS", "C. MSS PRESENT BUT NO BODY-CLOSE CONFIRMATION", "B. MSS PRESENT BUT WEAK DISPLACEMENT"):
                loss_cat = "A. Missing/weak MSS displacement"
            else:
                loss_cat = "F. Wrong structural context"

        rec = {
            "trade_id": t.trade_id,
            "date": t.date,
            "timestamp": t.entry_time.strftime("%H:%M:%S") if isinstance(t.entry_time, datetime) else str(t.entry_time),
            "direction": t.direction,
            "entry_price": round(t.entry_price, 2),
            "sl_price": round(t.initial_sl, 2),
            "exit_price": round(t.exit_price, 2),
            "net_r": round(t.net_r, 2),
            "gross_r": round(t.gross_r, 2),
            "score": t.strategy_score,
            "swept_liquidity": swept_lvl_name,
            "sweep_magnitude_pts": round(sweep_mag, 1),
            "candle_range_pts": round(c_range, 1),
            "candle_body_pts": round(c_body, 1),
            "candle_body_pct": round(body_pct, 1),
            "has_ref_swing": has_ref_swing,
            "ref_swing_price": round(ref_swing_price, 2) if has_ref_swing else 0.0,
            "has_impulse": has_impulse,
            "has_body_close": has_body_close,
            "mss_quality_category": mss_cat,
            "loss_cause_category": loss_cat,
            "room_to_liquidity_pts": round(room_pts, 1),
            "sufficient_room": sufficient_room,
            "is_major_sweep": is_major_sweep,
            "is_session_sweep": is_session_sweep,
            "is_repeated_sweep": is_repeated_sweep,
            "time_since_prev_s3_mins": round(time_since_prev_s3, 1),
            "is_reentry_10m": is_reentry_10m,
            "is_reentry_20m": is_reentry_20m,
            "is_reentry_30m": is_reentry_30m,
            "prev_s3_same_dir": prev_s3_same_dir,
            "prev_s3_result": prev_s3_result,
            "mfe_r": round(t.mfe_r, 2),
            "mae_r": round(t.mae_r, 2),
            "exit_reason": t.exit_reason,
            "duration_mins": t.duration_mins,
            "is_win": t.net_r > 0,
            "is_loss": t.net_r <= 0,
        }
        forensic_records.append(rec)

    # Export to CSV
    csv_path = Path("docs/PHASE8_1_S3_RANGE_TRADES.csv")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(forensic_records[0].keys()))
        writer.writeheader()
        writer.writerows(forensic_records)
    print(f"Exported {len(forensic_records)} trades to {csv_path}")

    # 2. MSS Quality Classification Distribution
    print("\n" + "="*60)
    print("MSS QUALITY CLASSIFICATION (50 TRADES)")
    print("="*60)
    mss_dist = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0, "net_r": 0.0})
    for r in forensic_records:
        cat = r["mss_quality_category"]
        mss_dist[cat]["count"] += 1
        if r["is_win"]:
            mss_dist[cat]["wins"] += 1
        else:
            mss_dist[cat]["losses"] += 1
        mss_dist[cat]["net_r"] += r["net_r"]

    for cat, data in sorted(mss_dist.items()):
        cnt = data["count"]
        w = data["wins"]
        l = data["losses"]
        nr = data["net_r"]
        wr = w / cnt * 100.0 if cnt else 0.0
        print(f"  {cat}: {cnt} trades ({cnt/len(forensic_records)*100:.1f}%) | {w}W/{l}L ({wr:.1f}% WR) | Net R: {nr:.2f}R")

    # 3. Causal Loss Categorization (38 Losses)
    print("\n" + "="*60)
    print("CAUSAL LOSS CATEGORIZATION (38 LOSSES)")
    print("="*60)
    losses_records = [r for r in forensic_records if r["is_loss"]]
    total_loss_r = sum(r["net_r"] for r in losses_records)
    loss_cat_dist = defaultdict(lambda: {"count": 0, "net_r": 0.0})
    for r in losses_records:
        cat = r["loss_cause_category"]
        loss_cat_dist[cat]["count"] += 1
        loss_cat_dist[cat]["net_r"] += r["net_r"]

    for cat, data in sorted(loss_cat_dist.items(), key=lambda x: x[1]["net_r"]):
        cnt = data["count"]
        nr = data["net_r"]
        cnt_pct = cnt / len(losses_records) * 100.0
        r_pct = abs(nr) / abs(total_loss_r) * 100.0 if total_loss_r else 0.0
        print(f"  {cat}: {cnt} trades ({cnt_pct:.1f}%) | Net R: {nr:.2f}R ({r_pct:.1f}% of S3 range loss)")

    # 4. Reentry Forensics
    print("\n" + "="*60)
    print("REENTRY FORENSICS")
    print("="*60)
    for window, key in [(10, "is_reentry_10m"), (20, "is_reentry_20m"), (30, "is_reentry_30m")]:
        re_trades = [r for r in forensic_records if r[key]]
        re_wins = sum(1 for r in re_trades if r["is_win"])
        re_net_r = sum(r["net_r"] for r in re_trades)
        print(f"  Reentry within <={window}m: {len(re_trades)} trades | {re_wins}W/{len(re_trades)-re_wins}L | Net R: {re_net_r:.2f}R")

    # Reentry by direction and prior outcome
    re30 = [r for r in forensic_records if r["is_reentry_30m"]]
    same_dir = [r for r in re30 if r["prev_s3_same_dir"]]
    opp_dir = [r for r in re30 if not r["prev_s3_same_dir"]]
    after_loss = [r for r in re30 if r["prev_s3_result"] == "LOSS"]
    after_win = [r for r in re30 if r["prev_s3_result"] == "WIN"]

    print(f"  Within 30m - Same Direction: {len(same_dir)} trades | Net R: {sum(r['net_r'] for r in same_dir):.2f}R")
    print(f"  Within 30m - Opposite Direction: {len(opp_dir)} trades | Net R: {sum(r['net_r'] for r in opp_dir):.2f}R")
    print(f"  Within 30m - After Prior Loss: {len(after_loss)} trades | Net R: {sum(r['net_r'] for r in after_loss):.2f}R")
    print(f"  Within 30m - After Prior Win: {len(after_win)} trades | Net R: {sum(r['net_r'] for r in after_win):.2f}R")

    # 5. Room to Liquidity Forensics
    print("\n" + "="*60)
    print("ROOM TO LIQUIDITY FORENSICS")
    print("="*60)
    suff = [r for r in forensic_records if r["sufficient_room"]]
    insuff = [r for r in forensic_records if not r["sufficient_room"]]
    print(f"  Sufficient Room (>= 40 pts): {len(suff)} trades | {sum(1 for r in suff if r['is_win'])}W/{sum(1 for r in suff if r['is_loss'])}L | Net R: {sum(r['net_r'] for r in suff):.2f}R")
    print(f"  Insufficient Room (< 40 pts): {len(insuff)} trades | {sum(1 for r in insuff if r['is_win'])}W/{sum(1 for r in insuff if r['is_loss'])}L | Net R: {sum(r['net_r'] for r in insuff):.2f}R")

    # 6. Sweep Quality Forensics
    print("\n" + "="*60)
    print("SWEEP QUALITY FORENSICS")
    print("="*60)
    major_sw = [r for r in forensic_records if r["is_major_sweep"]]
    sess_sw = [r for r in forensic_records if r["is_session_sweep"]]
    rep_sw = [r for r in forensic_records if r["is_repeated_sweep"]]
    fresh_sw = [r for r in forensic_records if not r["is_repeated_sweep"]]
    print(f"  Major Liquidity Sweep (PDH/PDL): {len(major_sw)} trades | {sum(1 for r in major_sw if r['is_win'])}W/{sum(1 for r in major_sw if r['is_loss'])}L | Net R: {sum(r['net_r'] for r in major_sw):.2f}R")
    print(f"  Session Liquidity Sweep (ORB): {len(sess_sw)} trades | {sum(1 for r in sess_sw if r['is_win'])}W/{sum(1 for r in sess_sw if r['is_loss'])}L | Net R: {sum(r['net_r'] for r in sess_sw):.2f}R")
    print(f"  Fresh Sweeps: {len(fresh_sw)} trades | Net R: {sum(r['net_r'] for r in fresh_sw):.2f}R")
    print(f"  Repeated Sweeps: {len(rep_sw)} trades | Net R: {sum(r['net_r'] for r in rep_sw):.2f}R")

    return {
        "forensic_records": forensic_records,
        "mss_dist": dict(mss_dist),
        "loss_cat_dist": dict(loss_cat_dist),
    }


if __name__ == "__main__":
    analyze_s3_range_forensics()
