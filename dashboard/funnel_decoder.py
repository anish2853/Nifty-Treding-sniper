"""Funnel Decoder, Machine Heartbeat Watchdog, and Forward Progress Helpers.

Pure observability helpers for Streamlit dashboard and Telegram EOD summary.
Reads from SQLite journal and forward session CSVs ONLY.
FROZEN-v1 rules and parameters remain untouched.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import settings
from utils import ist_now

log = logging.getLogger("funnel_decoder")

FROZEN_BASELINE = {
    "win_rate": 44.4,
    "net_r": 0.35,
    "expectancy": 0.04,
    "max_dd_r": 3.07,
    "max_streak": 3,
}

MACRO_HOURLY_BENCHMARK = {
    "sessions": 493,
    "trades": 139,
    "win_rate": 48.2,
    "net_r": 16.98,
    "expectancy": 0.12,
    "profit_factor": 1.45,
    "max_dd_r": 6.29,
    "max_streak": 9,
}


def check_heartbeat_health(
    candle_age_min: float | None,
    chain_age_min: float | None,
    now_dt: datetime | None = None,
) -> tuple[bool, str]:
    """Check machine heartbeat freshness.
    
    Rule: Any data age > 5 min during market hours (09:15-15:30 IST on weekdays)
    triggers DATA BLINDNESS.
    """
    now = now_dt or ist_now()
    is_weekday = now.weekday() < 5
    market_start = time(9, 15)
    market_end = time(15, 30)
    is_market_hours = is_weekday and (market_start <= now.time() <= market_end)

    if not is_market_hours:
        return True, "OFF-HOURS / HEALTHY (Outside 09:15-15:30 IST market window)"

    blind_reasons = []
    if candle_age_min is None or candle_age_min > 5.0:
        c_txt = f"{candle_age_min:.1f}m" if candle_age_min is not None else "NO DATA"
        blind_reasons.append(f"Candle age: {c_txt} (> 5m threshold)")

    if chain_age_min is None or chain_age_min > 5.0:
        ch_txt = f"{chain_age_min:.1f}m" if chain_age_min is not None else "NO DATA"
        blind_reasons.append(f"Chain poll age: {ch_txt} (> 5m threshold)")

    if blind_reasons:
        return False, " | ".join(blind_reasons)

    return True, "HEALTHY (Feeds fresh < 5m)"


def decode_activity_funnel(conn, iso_date: str) -> dict[str, Any]:
    """Decode today's activity funnel from journal rows for blank-day explanation."""
    fired_rows = conn.execute(
        "SELECT id, ts, direction, strike, spot, score, reasons_json FROM journal "
        "WHERE type = 'SIGNAL' AND substr(ts, 1, 10) = ? ORDER BY id",
        (iso_date,),
    ).fetchall()
    fired_count = len(fired_rows)

    skips = conn.execute(
        "SELECT score, notes, reasons_json, ts FROM journal "
        "WHERE type = 'SKIP' AND substr(ts, 1, 10) = ? ORDER BY id",
        (iso_date,),
    ).fetchall()

    gates = {
        "structure": 0,
        "regime": 0,
        "time": 0,
        "room": 0,
        "caps": 0,
        "other": 0,
    }

    armed_setups: list[dict[str, Any]] = []
    expired_count = 0
    candidate_count = fired_count

    for score, notes, r_json, ts in skips:
        n = (notes or "").lower()
        if score is not None or "blocked" in n or "gate" in n or "near-miss" in n or "setup" in n or "retest" in n:
            candidate_count += 1
            if "retest armed" in n or "setup armed" in n:
                armed_setups.append({
                    "ts": ts,
                    "notes": notes,
                    "score": score,
                })
            elif "retest expired" in n or "expired" in n:
                expired_count += 1
            elif "room" in n or "wall" in n or "no room" in n:
                gates["room"] += 1
            elif "regime" in n or "range" in n or "trend" in n:
                gates["regime"] += 1
            elif "time" in n or "dead-zone" in n or "09:15" in n or "11:30" in n or "14:30" in n:
                gates["time"] += 1
            elif "cap reached" in n or "halt" in n or "consecutive" in n:
                gates["caps"] += 1
            elif "structure" in n or "body" in n or "close" in n or "wick" in n or "score" in n:
                gates["structure"] += 1
            else:
                gates["other"] += 1

    rejected_count = sum(gates.values())

    if fired_count == 0:
        armed_note = f"{len(armed_setups)} armed, retests never came" if len(armed_setups) > 0 else "0 armed"
        summary_text = (
            f"WHY BLANK: {candidate_count} candidates | {rejected_count} rejected "
            f"({gates['structure']} structure, {gates['regime']} regime, {gates['time']} time, {gates['room']} room) | "
            f"{armed_note}"
        )
    else:
        summary_text = (
            f"ACTIVITY FUNNEL: {candidate_count} candidates | {rejected_count} rejected "
            f"({gates['structure']} structure, {gates['regime']} regime, {gates['time']} time, {gates['room']} room) | "
            f"{len(armed_setups)} armed | {fired_count} fired"
        )

    return {
        "candidates": candidate_count,
        "rejected": rejected_count,
        "gates": gates,
        "armed": armed_setups,
        "expired": expired_count,
        "fired": fired_count,
        "summary_text": summary_text,
    }


def get_forward_progress_data(conn=None) -> dict[str, Any]:
    """Retrieve forward session count and cumulative progress metrics."""
    forward_dir = settings.EXPORTS_DIR / "forward_sessions"
    session_files = sorted(forward_dir.glob("*_candles_5m.csv")) if forward_dir.exists() else []
    clean_session_files = [f for f in session_files if "NON_CLEAN" not in f.name]
    session_count = len(clean_session_files)

    # Trades from forward journal (Clean trades only; INVALID trades excluded)
    trades: list[dict[str, Any]] = []
    if conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(journal)").fetchall()]
        has_notes = "notes" in cols
        if has_notes:
            rows = conn.execute(
                "SELECT ts, direction, result_r, exit_reason, notes FROM journal "
                "WHERE type = 'EXIT' ORDER BY id"
            ).fetchall()
            for ts, direction, result_r, exit_reason, notes in rows:
                if "INVALID" in (exit_reason or "") or "INVALID" in (notes or ""):
                    continue
                trades.append({
                    "ts": ts,
                    "direction": direction,
                    "net_r": result_r or 0.0,
                    "exit_reason": exit_reason or "EXIT",
                })
        else:
            rows = conn.execute(
                "SELECT ts, direction, result_r, exit_reason FROM journal "
                "WHERE type = 'EXIT' ORDER BY id"
            ).fetchall()
            for ts, direction, result_r, exit_reason in rows:
                if "INVALID" in (exit_reason or ""):
                    continue
                trades.append({
                    "ts": ts,
                    "direction": direction,
                    "net_r": result_r or 0.0,
                    "exit_reason": exit_reason or "EXIT",
                })

    trade_count = len(trades)
    wins = sum(1 for t in trades if t["net_r"] > 0)
    losses = sum(1 for t in trades if t["net_r"] < 0)
    win_rate = (wins / trade_count * 100.0) if trade_count > 0 else 0.0
    net_r = sum(t["net_r"] for t in trades)
    expectancy = (net_r / trade_count) if trade_count > 0 else 0.0

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
    for t in trades:
        r = t["net_r"]
        cum += r
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
        if r < 0:
            streak += 1
            if streak > max_streak:
                max_streak = streak
        else:
            streak = 0

    # Projected checkpoint-1 date (20 trades: at ~1 trade every 6 sessions -> ~120 trading days)
    # Estimate from today
    est_sessions_needed = max(0, (20 - trade_count) * 6)
    proj_date = ist_now().date() + timedelta(days=int(est_sessions_needed * 1.4))

    return {
        "sessions_accumulated": session_count,
        "target_sessions": 60,
        "trades_taken": trade_count,
        "target_trades_checkpoint1": 20,
        "checkpoint1_proj_date": proj_date.isoformat(),
        "win_rate": round(win_rate, 1),
        "net_r": round(net_r, 2),
        "net_expectancy": round(expectancy, 2),
        "max_dd_r": round(max_dd, 2),
        "max_streak": max_streak,
        "real_money_gate": "BLOCKED",
        "system_status": "PAPER ONLY",
        "config_version": getattr(settings, "CONFIG_VERSION", "FROZEN-v1"),
        "trades": trades,
    }
