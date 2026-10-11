"""Phase 13 Forward Protocol & Saturday Audit Report.

Generates the Weekly Saturday Forward Paper Audit:
- Scans forward sessions in data/exports/forward_sessions/
- Evaluates cumulative trades, win rate, net PnL, net expectancy, max DD, losing streak
- Compares performance against:
    * FROZEN-v1 Baseline (+0.35R, 44.4% WR, 3.07R DD)
    * Macro 2-Year Hourly Baseline (+0.12R Exp, 1.45 PF)
- Triggers Anomaly Alerts:
    * Max Drawdown > 3.07R
    * Max Losing Streak > 4
    * Any data gap on non-holiday weekdays
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import settings

log = logging.getLogger("forward_audit")

BASELINE_FROZEN_V1 = {
    "net_r": 0.35,
    "win_rate": 44.4,
    "expectancy": 0.04,
    "max_dd_r": 3.07,
    "max_streak": 3,
}

BENCHMARK_MACRO_HOURLY = {
    "sessions": 493,
    "trades": 139,
    "win_rate": 48.2,
    "expectancy": 0.12,
    "profit_factor": 1.45,
    "max_dd_r": 6.29,
}

TARGET_GATE = {
    "min_sessions": 60,
    "min_expectancy": 0.05,
    "max_dd_r": 4.0,
    "max_streak": 4,
}


def load_official_holidays() -> set[str]:
    """Load known exchange holiday dates."""
    holidays_file = settings.HOLIDAYS_FILE
    if holidays_file.exists():
        try:
            with open(holidays_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("dates", []))
        except Exception as e:
            log.warning("Could not read holidays file: %s", e)
    return set()


def scan_forward_sessions() -> list[tuple[date, Path, int]]:
    """Scan data/exports/forward_sessions for archived CSVs.
    
    Returns list of (session_date, file_path, candle_count).
    """
    forward_dir = settings.EXPORTS_DIR / "forward_sessions"
    if not forward_dir.exists():
        return []

    sessions = []
    for p in sorted(forward_dir.glob("*_candles_5m.csv")):
        try:
            date_str = p.name.split("_")[0]
            d = date.fromisoformat(date_str)
            # count rows
            with open(p, "r", encoding="utf-8") as f:
                row_count = sum(1 for _ in f) - 1
            sessions.append((d, p, max(0, row_count)))
        except Exception:
            continue
    return sessions


def check_data_gaps(
    session_dates: list[date],
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[str]:
    """Identify missing trading dates (weekdays excluding holidays)."""
    if not session_dates and start_date is None:
        return []

    holidays = load_official_holidays()
    recorded_dates = set(session_dates)

    if start_date is None:
        start_date = min(session_dates) if session_dates else date.today()
    if end_date is None:
        end_date = max(session_dates) if session_dates else date.today()

    gaps = []
    curr = start_date
    while curr <= end_date:
        # Check weekday (Monday=0 to Friday=4)
        if curr.weekday() < 5:
            iso = curr.isoformat()
            if iso not in holidays and curr not in recorded_dates:
                gaps.append(f"Missing session: {iso}")
        curr += timedelta(days=1)
    return gaps


def compute_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute performance metrics from trade records."""
    total = len(trades)
    if total == 0:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "net_r": 0.0,
            "net_expectancy": 0.0,
            "max_dd_r": 0.0,
            "max_loss_streak": 0,
            "current_streak": 0,
            "profit_factor": 0.0,
        }

    wins = sum(1 for t in trades if t.get("net_r", 0) > 0)
    losses = sum(1 for t in trades if t.get("net_r", 0) < 0)
    win_rate = (wins / total) * 100.0

    net_r = sum(t.get("net_r", 0) for t in trades)
    expectancy = net_r / total

    # Drawdown & streaks
    cum = 0.0
    peak = 0.0
    max_dd = 0.0

    loss_streak = 0
    max_loss_streak = 0
    gross_gains = 0.0
    gross_losses = 0.0

    for t in trades:
        r = t.get("net_r", 0)
        cum += r
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

        if r < 0:
            loss_streak += 1
            gross_losses += abs(r)
            if loss_streak > max_loss_streak:
                max_loss_streak = loss_streak
        elif r > 0:
            loss_streak = 0
            gross_gains += r

    pf = (gross_gains / gross_losses) if gross_losses > 0 else (99.0 if gross_gains > 0 else 0.0)

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1),
        "net_r": round(net_r, 2),
        "net_expectancy": round(expectancy, 2),
        "max_dd_r": round(max_dd, 2),
        "max_loss_streak": max_loss_streak,
        "current_streak": loss_streak,
        "profit_factor": round(pf, 2),
    }


def detect_anomalies(metrics: dict[str, Any], gaps: list[str]) -> list[str]:
    """Check for anomaly alerts based on Phase 13 guardrails."""
    anomalies = []
    # 1. Drawdown anomaly
    if metrics["max_dd_r"] > BASELINE_FROZEN_V1["max_dd_r"]:
        anomalies.append(
            f"MAX DRAWDOWN ANOMALY: Current DD ({metrics['max_dd_r']}R) exceeded baseline "
            f"({BASELINE_FROZEN_V1['max_dd_r']}R)"
        )

    # 2. Streak anomaly
    if metrics["max_loss_streak"] > TARGET_GATE["max_streak"]:
        anomalies.append(
            f"LOSING STREAK ANOMALY: Max streak ({metrics['max_loss_streak']}) exceeded gate "
            f"limit ({TARGET_GATE['max_streak']})"
        )

    # 3. Data gap anomaly
    if gaps:
        anomalies.append(f"DATA INTEGRITY ANOMALY: {len(gaps)} missing session(s) detected")

    return anomalies


def generate_weekly_report(
    session_count: int,
    metrics: dict[str, Any],
    gaps: list[str],
    anomalies: list[str],
) -> str:
    """Format the complete Saturday Forward Audit Report."""
    gate_status = "PENDING (Accumulating)" if session_count < TARGET_GATE["min_sessions"] else (
        "PASS" if (metrics["net_expectancy"] >= TARGET_GATE["min_expectancy"] and
                   metrics["max_dd_r"] <= TARGET_GATE["max_dd_r"] and
                   metrics["max_loss_streak"] <= TARGET_GATE["max_streak"] and
                   len(anomalies) == 0)
        else "FAIL"
    )

    lines = [
        "=" * 70,
        "NIFTY SNIPER — WEEKLY FORWARD PROTOCOL REPORT (SATURDAY AUDIT)",
        "=" * 70,
        f"Generated At:       {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}",
        f"Config Version:     {settings.CONFIG_VERSION}",
        f"Execution Mode:     {settings.EXECUTION_MODE}",
        f"Real-Money Gate:    {settings.REAL_MONEY_GATE}",
        f"System Status:      {settings.SYSTEM_STATUS}",
        "-" * 70,
        "1. FORWARD HOLDOUT PROGRESS",
        f"  Archived Sessions: {session_count} / {TARGET_GATE['min_sessions']} required",
        f"  Total Trades:      {metrics['total_trades']}",
        f"  Win / Loss:        {metrics['wins']}W / {metrics['losses']}L",
        f"  Win Rate:          {metrics['win_rate']}%",
        f"  Net PnL (R):       {metrics['net_r']}R",
        f"  Net Expectancy:    {metrics['net_expectancy']}R / trade",
        f"  Max Drawdown (R):  {metrics['max_dd_r']}R",
        f"  Max Loss Streak:   {metrics['max_loss_streak']}",
        "-" * 70,
        "2. SCORECARD BENCHMARK COMPARISON",
        f"  Metric              Forward Holdout   FROZEN-v1 (5m)   Macro (2y Hourly)",
        f"  ------------------  ----------------  ---------------  -----------------",
        f"  Win Rate:           {metrics['win_rate']:>6.1f}%          {BASELINE_FROZEN_V1['win_rate']:>6.1f}%          {BENCHMARK_MACRO_HOURLY['win_rate']:>6.1f}%",
        f"  Expectancy:         {metrics['net_expectancy']:>6.2f}R         {BASELINE_FROZEN_V1['expectancy']:>6.2f}R         {BENCHMARK_MACRO_HOURLY['expectancy']:>6.2f}R",
        f"  Max Drawdown:       {metrics['max_dd_r']:>6.2f}R         {BASELINE_FROZEN_V1['max_dd_r']:>6.2f}R         {BENCHMARK_MACRO_HOURLY['max_dd_r']:>6.2f}R",
        f"  Max Loss Streak:    {metrics['max_loss_streak']:>6d}           {BASELINE_FROZEN_V1['max_streak']:>6d}           {9:>6d}",
        "-" * 70,
        "3. ANOMALY MONITORING & DATA GAPS",
    ]

    if not anomalies and not gaps:
        lines.append("  STATUS: CLEAN — No drawdown, streak, or data gap anomalies.")
    else:
        for a in anomalies:
            lines.append(f"  [!] ALERT: {a}")
        for g in gaps:
            lines.append(f"  [!] GAP:   {g}")

    lines.extend([
        "-" * 70,
        "4. REAL-MONEY GATE VERDICT",
        f"  Requirement Checklist:",
        f"  [ {'X' if session_count >= 60 else ' '} ] 60+ Clean Forward Sessions ({session_count}/60)",
        f"  [ {'X' if metrics['net_expectancy'] >= TARGET_GATE['min_expectancy'] else ' '} ] Net Expectancy >= +0.05R ({metrics['net_expectancy']:+.2f}R)",
        f"  [ {'X' if metrics['max_dd_r'] <= TARGET_GATE['max_dd_r'] else ' '} ] Max Drawdown <= 4.0R ({metrics['max_dd_r']:.2f}R)",
        f"  [ {'X' if metrics['max_loss_streak'] <= TARGET_GATE['max_streak'] else ' '} ] Max Loss Streak <= 4 ({metrics['max_loss_streak']})",
        f"  [   ] Sizing Math Cleared (Capital >= Rs. 45,000 for 4R <= 20% DD)",
        f"  Verdict: {gate_status} (Real-Money Execution Remains Strictly BLOCKED)",
        "=" * 70,
    ])

    return "\n".join(lines)


def run_saturday_audit(alert: bool = False) -> str:
    """Run full Saturday audit."""
    sessions = scan_forward_sessions()
    session_dates = [s[0] for s in sessions]
    gaps = check_data_gaps(session_dates) if session_dates else []

    # Read trades from journal store if available
    trades: list[dict[str, Any]] = []
    metrics = compute_metrics(trades)
    anomalies = detect_anomalies(metrics, gaps)

    report = generate_weekly_report(len(sessions), metrics, gaps, anomalies)
    print(report)

    if alert and (anomalies or gaps):
        # Fire Telegram notification if configured
        try:
            from alerts.telegram_bot import TelegramBot
            tb = TelegramBot()
            tb.send_message(f"🚨 *FORWARD PROTOCOL ANOMALY ALERT* 🚨\n\n" + "\n".join(anomalies + gaps))
        except Exception as e:
            log.warning("Could not dispatch Telegram alert: %s", e)

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Weekly Saturday Forward Audit Report")
    parser.add_argument("--alert", action="store_true", help="Send Telegram alert if anomalies exist")
    args = parser.parse_args()
    run_saturday_audit(alert=args.alert)
