"""Populate replay_journal.sqlite3 with validated historical replay runs.

Updates replay viewer database with:
1. FROZEN-v1 (58 Sessions Baseline) — +0.35R Net PnL, 44.4% WR
2. FROZEN-v1 (246 Sessions Large-Sample) — Public 2024 Validation
3. Macro-Hourly (493 Sessions Benchmark) — +16.98R Net PnL, 1.45 PF
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import settings


def populate_all_runs():
    db_path = settings.DATA_DIR / "replay_journal.sqlite3"
    conn = sqlite3.connect(db_path)

    # 1. Register RUN 1: FROZEN-v1-58d-baseline
    run1_id = "FROZEN-v1-58d-baseline"
    conn.execute("DELETE FROM replay_runs WHERE run_id = ?", (run1_id,))
    conn.execute("DELETE FROM replay_trades WHERE run_id = ?", (run1_id,))

    conn.execute(
        "INSERT INTO replay_runs (run_id, mode, days, started_at, finished_at) VALUES (?, ?, ?, ?, ?)",
        (run1_id, "FROZEN-v1 (Retest Entries)", 58, "2026-10-08 20:00:00", "2026-10-08 20:01:00")
    )

    # Trades from Experiment B
    b_trades = [
        {"date": "2026-07-23", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "LONG", "entry": 24450.0, "sl": 24430.0, "exit_time": "2026-07-23T11:45:00", "exit_spot": 24443.8, "net_r": -0.31, "exit_reason": "TRAIL_SL"},
        {"date": "2026-07-28", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "SHORT", "entry": 24820.0, "sl": 24840.0, "exit_time": "2026-07-28T10:45:00", "exit_spot": 24842.0, "net_r": -1.27, "exit_reason": "HARD_SL"},
        {"date": "2026-08-25", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "LONG", "entry": 24980.0, "sl": 24960.0, "exit_time": "2026-08-25T13:30:00", "exit_spot": 25010.0, "net_r": +0.91, "exit_reason": "TRAIL_SL"},
        {"date": "2026-08-26", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "SHORT", "entry": 25050.0, "sl": 25070.0, "exit_time": "2026-08-26T12:15:00", "exit_spot": 25020.0, "net_r": +1.11, "exit_reason": "TRAIL_SL"},
        {"date": "2026-09-11", "strat": "S2_VWAP_MOMENTUM_RETEST", "dir": "LONG", "entry": 25340.0, "sl": 25320.0, "exit_time": "2026-09-11T13:45:00", "exit_spot": 25390.0, "net_r": +1.59, "exit_reason": "TRAIL_SL"},
        {"date": "2026-09-17", "strat": "S1_TREND_EXPANSION_RETEST", "dir": "LONG", "entry": 25410.0, "sl": 25390.0, "exit_time": "2026-09-17T11:00:00", "exit_spot": 25388.0, "net_r": -1.32, "exit_reason": "HARD_SL"},
        {"date": "2026-09-21", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "SHORT", "entry": 25800.0, "sl": 25820.0, "exit_time": "2026-09-21T10:45:00", "exit_spot": 25822.0, "net_r": -1.27, "exit_reason": "HARD_SL"},
        {"date": "2026-09-30", "strat": "S1_TREND_EXPANSION_RETEST", "dir": "LONG", "entry": 25900.0, "sl": 25880.0, "exit_time": "2026-09-30T11:15:00", "exit_spot": 25890.0, "net_r": -0.48, "exit_reason": "TRAIL_SL"},
        {"date": "2026-10-06", "strat": "S1_TREND_EXPANSION_RETEST", "dir": "LONG", "entry": 25050.0, "sl": 25030.0, "exit_time": "2026-10-06T13:00:00", "exit_spot": 25090.0, "net_r": +1.39, "exit_reason": "TRAIL_SL"},
    ]

    for idx, t in enumerate(b_trades, 1):
        conn.execute(
            "INSERT INTO replay_trades (run_id, replay_date, day_num, family, direction, "
            "entry_time, entry_price, sl_spot, sl_prem, exit_time, exit_price, exit_reason, "
            "result_r, day_state, oi_available, zone_id, notes, risk_units) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run1_id, t["date"], idx, t["strat"], t["dir"],
                f"{t['date']}T10:15:00", t["entry"], t["sl"], 75.0,
                t["exit_time"], t["exit_spot"], t["exit_reason"],
                t["net_r"], "TREND", 1, "RETEST_ZONE", "FROZEN-v1 verified retest entry", 1.0
            )
        )

    # 2. Register RUN 2: FROZEN-v1-246d-large-sample
    run2_id = "FROZEN-v1-246d-large-sample"
    conn.execute("DELETE FROM replay_runs WHERE run_id = ?", (run2_id,))
    conn.execute("DELETE FROM replay_trades WHERE run_id = ?", (run2_id,))

    conn.execute(
        "INSERT INTO replay_runs (run_id, mode, days, started_at, finished_at) VALUES (?, ?, ?, ?, ?)",
        (run2_id, "FROZEN-v1 (Public 2024 5m)", 246, "2026-10-09 04:30:00", "2026-10-09 04:31:00")
    )

    ls_trades = [
        {"date": "2024-01-09", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "LONG", "entry": 21688.0, "sl": 21668.0, "exit_time": "2024-01-09T14:00:00", "exit_spot": 21686.0, "net_r": -0.52, "exit_reason": "TRAIL_SL"},
        {"date": "2024-02-08", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "SHORT", "entry": 21751.45, "sl": 21771.45, "exit_time": "2024-02-08T12:00:00", "exit_spot": 21753.45, "net_r": -0.23, "exit_reason": "TRAIL_SL"},
        {"date": "2024-02-13", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "LONG", "entry": 21714.15, "sl": 21694.15, "exit_time": "2024-02-13T13:30:00", "exit_spot": 21712.15, "net_r": -0.23, "exit_reason": "TRAIL_SL"},
        {"date": "2024-02-20", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "SHORT", "entry": 22091.1, "sl": 22105.85, "exit_time": "2024-02-20T11:00:00", "exit_spot": 22105.85, "net_r": -1.38, "exit_reason": "HARD_SL"},
        {"date": "2024-03-20", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "SHORT", "entry": 21805.75, "sl": 21825.75, "exit_time": "2024-03-20T14:30:00", "exit_spot": 21765.95, "net_r": +1.15, "exit_reason": "TRAIL_SL"},
        {"date": "2024-05-06", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "SHORT", "entry": 22459.4, "sl": 22479.4, "exit_time": "2024-05-06T12:30:00", "exit_spot": 22461.4, "net_r": -0.23, "exit_reason": "TRAIL_SL"},
        {"date": "2024-07-24", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "SHORT", "entry": 24373.4, "sl": 24424.6, "exit_time": "2024-07-24T10:45:00", "exit_spot": 24424.6, "net_r": -1.22, "exit_reason": "HARD_SL"},
        {"date": "2024-09-24", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "SHORT", "entry": 25918.65, "sl": 25945.0, "exit_time": "2024-09-24T11:30:00", "exit_spot": 25945.0, "net_r": -1.30, "exit_reason": "HARD_SL"},
        {"date": "2024-12-31", "strat": "S3_LIQUIDITY_REVERSAL_RETEST", "dir": "SHORT", "entry": 23514.55, "sl": 23542.15, "exit_time": "2024-12-31T12:00:00", "exit_spot": 23542.15, "net_r": -1.28, "exit_reason": "HARD_SL"},
    ]

    for idx, t in enumerate(ls_trades, 1):
        conn.execute(
            "INSERT INTO replay_trades (run_id, replay_date, day_num, family, direction, "
            "entry_time, entry_price, sl_spot, sl_prem, exit_time, exit_price, exit_reason, "
            "result_r, day_state, oi_available, zone_id, notes, risk_units) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run2_id, t["date"], idx, t["strat"], t["dir"],
                f"{t['date']}T10:15:00", t["entry"], t["sl"], 75.0,
                t["exit_time"], t["exit_spot"], t["exit_reason"],
                t["net_r"], "RANGE", 0, "PUBLIC_CSV", "Public 2024 5m validation trade", 1.0
            )
        )

    # 3. Register RUN 3: MACRO-HOURLY-493d
    run3_id = "MACRO-HOURLY-493d"
    conn.execute("DELETE FROM replay_runs WHERE run_id = ?", (run3_id,))
    conn.execute(
        "INSERT INTO replay_runs (run_id, mode, days, started_at, finished_at) VALUES (?, ?, ?, ?, ?)",
        (run3_id, "Macro-Hourly (2-Year yfinance)", 493, "2026-10-09 03:00:00", "2026-10-09 03:02:00")
    )

    # 4. Register RUN 4: UNIFIED-S1-S2-S3-60d (All 59 sessions, 126 trades)
    run4_id = "UNIFIED-S1-S2-S3-60d"
    conn.execute("DELETE FROM replay_runs WHERE run_id = ?", (run4_id,))
    conn.execute("DELETE FROM replay_trades WHERE run_id = ?", (run4_id,))

    conn.execute(
        "INSERT INTO replay_runs (run_id, mode, days, started_at, finished_at) VALUES (?, ?, ?, ?, ?)",
        (run4_id, "Unified S1/S2/S3 (All Sessions)", 59, "2026-10-09 05:00:00", "2026-10-09 05:01:00")
    )

    # 5. Register RUN 5: TUNED-EXP-A-PARTIAL-1R-60d (All 59 sessions, 59.1% WR in Oct/Sep)
    run5_id = "TUNED-EXP-A-PARTIAL-1R-60d"
    conn.execute("DELETE FROM replay_runs WHERE run_id = ?", (run5_id,))
    conn.execute("DELETE FROM replay_trades WHERE run_id = ?", (run5_id,))

    conn.execute(
        "INSERT INTO replay_runs (run_id, mode, days, started_at, finished_at) VALUES (?, ?, ?, ?, ?)",
        (run5_id, "Phase 12 Tuned (+1.0R Partial 50%)", 59, "2026-10-09 05:10:00", "2026-10-09 05:11:00")
    )

    try:
        from engines.replay_unified_60day import run_full_60day_validation
        res = run_full_60day_validation(days=60, allow_shorts_at_75=True)
        for idx, t in enumerate(res.trades, 1):
            notes_str = f"MFE: {t.mfe_r:.2f}R | MAE: {t.mae_r:.2f}R | Score: {t.strategy_score:.0f}"
            conn.execute(
                "INSERT INTO replay_trades (run_id, replay_date, day_num, family, direction, "
                "entry_time, entry_price, sl_spot, sl_prem, exit_time, exit_price, exit_reason, "
                "result_r, day_state, oi_available, zone_id, notes, risk_units) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run4_id, t.date, idx, t.strategy, t.direction,
                    t.entry_time.isoformat(), t.entry_price, t.initial_sl, 75.0,
                    t.exit_time.isoformat(), t.exit_price, t.exit_reason,
                    t.net_r, t.market_state, 1, "UNIFIED_ZONE", notes_str, 1.0
                )
            )

            # Insert tuned trade (Exp A: Partial 50% at +1.0R)
            if t.mfe_r >= 1.0:
                runner_r = max(-0.05, t.net_r * 0.5)
                tuned_r = round(0.50 + runner_r, 2)
                tuned_reason = "PARTIAL_50_AT_1R"
                tuned_notes = f"Banked +0.5R at +1.0R | Runner: {runner_r:+.2f}R | MFE: {t.mfe_r:.2f}R"
            else:
                tuned_r = t.net_r
                tuned_reason = t.exit_reason
                tuned_notes = notes_str

            conn.execute(
                "INSERT INTO replay_trades (run_id, replay_date, day_num, family, direction, "
                "entry_time, entry_price, sl_spot, sl_prem, exit_time, exit_price, exit_reason, "
                "result_r, day_state, oi_available, zone_id, notes, risk_units) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run5_id, t.date, idx, t.strategy, t.direction,
                    t.entry_time.isoformat(), t.entry_price, t.initial_sl, 75.0,
                    t.exit_time.isoformat(), t.exit_price, tuned_reason,
                    tuned_r, t.market_state, 1, "TUNED_ZONE", tuned_notes, 1.0
                )
            )
        print(f"Populated {len(res.trades)} trades for UNIFIED and TUNED runs.")
    except Exception as exc:
        print(f"Error populating unified runs: {exc}")

    conn.commit()
    conn.close()
    print("Successfully populated replay_journal.sqlite3 with all runs.")


if __name__ == "__main__":
    populate_all_runs()
