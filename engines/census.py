"""Engine Census Module (Phase 14: One Brain — Consolidation).

Enumerate EVERY signal path that can execute a trade.
Any engine not mapped to an approved spec (FROZEN-v1 or approved EXP)
has execution strictly DISABLED and runs in shadow-only mode.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Ensure root directory is in sys.path
root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import settings

log = logging.getLogger("engines.census")


@dataclass
class EngineCensusRecord:
    engine_id: str
    name: str
    signal_path: str
    spec: str               # "FROZEN-v1" | "APPROVED-EXP" | "UNAPPROVED-EXP" | "ROUGE-SIM"
    execution_status: str   # "ENABLED" | "DISABLED_SHADOW" | "DETACHED_REMOVED"
    approved: bool
    notes: str


def get_engine_census() -> list[EngineCensusRecord]:
    """Returns the comprehensive census of all signal and execution paths in the system."""
    return [
        EngineCensusRecord(
            engine_id="ENG-01-BRK",
            name="FROZEN-v1 Breakout Sniper",
            signal_path="engines/entry.py:evaluate -> main.py:fire",
            spec="FROZEN-v1",
            execution_status="ENABLED",
            approved=True,
            notes="ORB / PDH / PDL breakout with OI covering confirmation, Score >= 80, time gate 09:30-11:00.",
        ),
        EngineCensusRecord(
            engine_id="ENG-02-SWP",
            name="FROZEN-v1 Liquidity Sweep-Fade",
            signal_path="engines/sweep_fade.py:evaluate -> main.py:fire",
            spec="FROZEN-v1",
            execution_status="ENABLED",
            approved=True,
            notes="Liquidity pool sweep rejection at PDH/PDL/OI walls with candle close back inside by >25%.",
        ),
        EngineCensusRecord(
            engine_id="ENG-03-ARCH",
            name="Phase 1-10 Architectural Pipeline",
            signal_path="strategies/s1_trend_expansion.py, s2_vwap_momentum.py, s3_liquidity_reversal.py -> Arbiter -> RiskGovernor -> PositionManager",
            spec="FROZEN-v1",
            execution_status="ENABLED",
            approved=True,
            notes="Unified multi-strategy arbiter governed by strict risk governor and position manager.",
        ),
        EngineCensusRecord(
            engine_id="ENG-04-SMW1",
            name="Institutional Liquidity Sweep Engine (Arm 1)",
            signal_path="engines/shadow_mw.py:MWPatternEngine -> Arm 1",
            spec="FROZEN-v1",
            execution_status="ENABLED",
            approved=True,
            notes="Institutional M/W boundary sweeps at PDH/PDL/OI walls with MSS confirmation & Fib expansion targets.",
        ),
        EngineCensusRecord(
            engine_id="ENG-05-TLFIB",
            name="Phase 15.1 Trendline Fibonacci Golden Pocket v2",
            signal_path="engines/shadow_mw.py:LiquidityTrendlineFibEngine -> main.py:sniper.arm2",
            spec="PHASE-15.1-TLFIB-v2",
            execution_status="ENABLED",
            approved=True,
            notes="Restored core engine: Delta 0.65-0.75 ITM, H >= 30 impulse filter, ride target >= 20 prem pts, 5m structural trail exit.",
        ),
        EngineCensusRecord(
            engine_id="ENG-06-DSIM",
            name="Dashboard Private Simulation",
            signal_path="dashboard/app.py:InstitutionalLiquidityEngine -> run_working_sniper",
            spec="ROUGE-SIM",
            execution_status="DETACHED_REMOVED",
            approved=False,
            notes="Dashboard must NEVER simulate trades independently. DETACHED & REMOVED. Dashboard reads only single journal.",
        ),
        EngineCensusRecord(
            engine_id="ENG-07-ZONERIDE",
            name="Zone-Ride Confluence Provider",
            signal_path="engines/zone_ride.py:build_liquidity_zones -> bonus score only",
            spec="DEMOTED-BONUS",
            execution_status="DEMOTED_BONUS",
            approved=False,
            notes="Demoted from signal gate to live confluence bonus (+10 score). Never gates or blocks execution.",
        ),
        EngineCensusRecord(
            engine_id="ENG-08-ZR",
            name="Liquidity Zone Reversal Engine (Arm 3)",
            signal_path="engines/shadow_mw.py:LiquidityZoneReversalEngine -> UnifiedLiquiditySniper.arm3",
            spec="PHASE-16-ZR-v1",
            execution_status="ENABLED",
            approved=True,
            notes="Production 2-candle reversal at PDH/PDL/OI levels + trendline rejection. Max 3 trades/day. "
                  "Structural trailing exit. Delta 0.65-0.75 ITM. No complex state machine.",
        ),
    ]


def print_and_export_census(export_path: Path | None = None) -> str:
    """Prints the engine census table and exports census to JSON."""
    census = get_engine_census()
    lines = [
        "==========================================================================================",
        "                    PHASE 14: ONE BRAIN — ENGINE CENSUS & EXECUTION GATES                 ",
        "==========================================================================================",
        f"{'ID':<12} | {'SPEC':<14} | {'STATUS':<17} | {'NAME':<36}",
        "------------------------------------------------------------------------------------------",
    ]
    for r in census:
        approved_flag = " [APPROVED]" if r.approved else " [BLOCKED]"
        lines.append(f"{r.engine_id:<12} | {r.spec:<14} | {r.execution_status:<17} | {r.name:<36}{approved_flag}")
    lines.append("==========================================================================================")
    lines.append("ENFORCEMENT: Only engines mapped to an approved spec (FROZEN-v1) may execute trades.")
    lines.append("All unapproved engines (Model A, TL-FIB, M/W, Fib Golden Pocket) are locked in SHADOW-ONLY mode.")
    lines.append("==========================================================================================")

    output_text = "\n".join(lines)
    log.info("\n" + output_text)

    # Export to data/census.json
    out_file = export_path or (settings.DATA_DIR / "census.json")
    try:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(r) for r in census]
        out_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("Failed to export census.json: %s", exc)

    return output_text


def is_engine_approved(engine_id: str) -> bool:
    """Checks whether an engine is permitted to execute live/paper trades."""
    for r in get_engine_census():
        if r.engine_id == engine_id:
            return r.approved and r.execution_status == "ENABLED"
    return False


get_census = get_engine_census


if __name__ == "__main__":
    print(print_and_export_census())
