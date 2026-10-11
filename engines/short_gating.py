"""Short Gating Engine (measured, not deleted).

Rules:
- SHORT entries while gated:
  1. score >= 90
  2. STRONG-level confluence required: PDH, PDL, OI wall, OB, or EQ
  3. Risk units = 0.5 while gated
- LONG entries: unchanged (score >= 80, risk units = 1.0)
- Tag all shorts: GATED-SHORT
- EOD: report shorts' running expectancy
- After 60 total trades:
  - Expectancy < 0 -> Telegram recommendation to disable short entries
  - Expectancy >= 0 -> gates lift automatically
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

import settings

log = logging.getLogger(__name__)

STRONG_LEVEL_PREFIXES = ("PDH", "PDL", "OI", "OB", "EQ", "DEMAND", "SUPPLY")


def is_strong_level(level_name: str | None) -> bool:
    """Check if the level represents a STRONG reference (PDH/PDL/OI wall/OB/EQ)."""
    if not level_name:
        return False
    name_upper = level_name.upper()
    return any(name_upper.startswith(prefix) or prefix in name_upper
               for prefix in STRONG_LEVEL_PREFIXES)


def get_trades_expectancy(conn: sqlite3.Connection, table: str = "journal") -> dict[str, Any]:
    """Calculate running expectancy and trade counts for all and short trades."""
    try:
        if table == "journal":
            rows = conn.execute(
                "SELECT direction, result_r FROM journal WHERE type = 'EXIT' AND result_r IS NOT NULL"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT direction, result_r FROM replay_trades WHERE exit_time IS NOT NULL AND result_r IS NOT NULL"
            ).fetchall()
    except Exception as exc:
        log.warning("Could not query trade expectancy from %s: %exc", table, exc)
        return {
            "total_trades": 0,
            "short_trades": 0,
            "short_expectancy": 0.0,
            "short_win_rate": 0.0,
            "is_gated": True,
            "recommendation": "MONITORING (<60 trades)",
        }

    total_count = len(rows)
    short_r_list = [float(r[1]) for r in rows if r[0] == "SHORT" and r[1] is not None]
    short_count = len(short_r_list)

    if short_count > 0:
        short_exp = sum(short_r_list) / short_count
        short_wins = sum(1 for r in short_r_list if r > 0)
        short_wr = (short_wins / short_count) * 100.0
    else:
        short_exp = 0.0
        short_wr = 0.0

    if total_count < 60:
        is_gated = True
        recommendation = f"MONITORING ({total_count}/60 total trades evaluated)"
    else:
        if short_exp >= 0:
            is_gated = False
            recommendation = "GATES LIFTED (positive short expectancy after 60+ trades)"
        else:
            is_gated = True
            recommendation = "RECOMMENDATION: DISABLE SHORTS (negative short expectancy after 60+ trades)"

    return {
        "total_trades": total_count,
        "short_trades": short_count,
        "short_expectancy": round(short_exp, 2),
        "short_win_rate": round(short_wr, 1),
        "is_gated": is_gated,
        "recommendation": recommendation,
    }


def evaluate_short_gate(
    direction: str,
    score: int,
    level_name: str | None = None,
    is_gated: bool = True,
) -> tuple[bool, float, str, str]:
    """Evaluate short gating rules.
    
    Returns:
        (allowed, risk_units, tag, block_reason)
    """
    if direction == "LONG":
        return True, 1.0, "", ""

    # Direction is SHORT
    if not is_gated:
        return True, 1.0, "", ""

    # Gated check 1: score >= 90
    if score < 90:
        return False, 0.5, "GATED-SHORT", f"Short gating: score {score} < 90 required"

    # Gated check 2: STRONG-level confluence
    if not is_strong_level(level_name):
        return (
            False,
            0.5,
            "GATED-SHORT",
            f"Short gating: level '{level_name}' lacks STRONG confluence (PDH/PDL/OI wall/OB/EQ)",
        )

    # All criteria passed for gated short
    return True, 0.5, "GATED-SHORT", ""
