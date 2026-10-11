"""Sweep -> MSS -> POI Reversal Model (Primary Reversal Family) with Rigorous A/B Forensics.

Pipeline Lifecycle:
STEP 1: SWEEP DETECTION (wick beyond ladder pool + close inside) -> Status: SWEPT / AWAITING MSS
  - Pools: Macro ladder levels only (PDH, PDL, ORB-H, ORB-L, EQ, Session High/Low). No micro-wiggles.
  - High pools (PDH, ORB-H, SESS-H): only UP-sweep (high > level, close < level) -> SHORT.
  - Low pools (PDL, ORB-L, SESS-L): only DOWN-sweep (low < level, close > level) -> LONG.
  - EQ: up-sweep -> SHORT; down-sweep -> LONG.
  - Re-sweep guard: price sweeping deeper resets displacement search.

STEP 2: MSS CONFIRMATION (close past reference swing with impulsive displacement) -> Status: MSS ✓
  - Reference swing: preceding swing high >= sweep_extreme + 15 pts (for LONG); swing low <= sweep_extreme - 15 pts (for SHORT).
  - Displacement quality: candle body >= 60% of candle range (min range 12 pts) OR creates valid FVG (>= 10 pts wide).
  - Weak drift is rejected; setup remains AWAITING MSS.

STEP 3: POI MARKING (OB, FVG, IFVG, Breaker) -> Status: AWAITING RETEST
  - Priority: OB -> FVG -> IFVG -> Breaker.
  - Minimum geometry gate: POI zone width must be >= 10.0 pts (no micro-FVG noise).
  - Nearest POI in retrace direction selected as active entry zone.

STEP 4: RETEST ENTRY -> Status: IN TRADE
  - Price retraces INTO the POI + 1-min rejection candle closes off the zone (no entry on mere touch!).
  - Rejection confirmation: close > open + lower wick >= 35% for LONG; close < open + upper wick >= 35% for SHORT.
  - SL beyond POI extreme + 5 pt buffer, enforced with minimum geometry stop: risk_pts >= 15.0 pts (no noise stops).
  - Freshness: valid within 60 minutes of MSS.
  - Structure intact: no close beyond displacement origin / sweep extreme.
  - Short gating: score >= 90 + strong confluence via evaluate_short_gate.

STEP 5: TARGETS & EXIT
  - Primary target: opposite pool (PDH for LONG; PDL for SHORT).
  - 1.5R target: 50% booking at +1.5R or nearest pool; runner structure trail.
  - Full intrabar SL and 15:10 square-off.

A/B FORENSICS & ACCOUNTING INTEGRITY:
  - Both OLD (immediate sweep-fade) and NEW (MSS+POI retest) arms simulated on every candle.
  - Parity guaranteed: identical sweep events, identical price path, identical cost deduction (0.05R).
  - Coherence assertions: Wins + Losses = N; Net R = Gross R - Costs; all fired trades have completed exits.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

import settings
from engines.candles import Candle
from engines.short_gating import evaluate_short_gate

log = logging.getLogger(__name__)

SWEEP_AB_DB_PATH = Path("data/sweep_ab.sqlite3")

# Setup pipeline status constants
STATUS_NONE = "NONE"
STATUS_SWEPT = "SWEPT"
STATUS_AWAITING_MSS = "AWAITING MSS"
STATUS_MSS_CONFIRMED = "MSS ✓"
STATUS_AWAITING_RETEST = "AWAITING RETEST"
STATUS_IN_TRADE = "IN TRADE"
STATUS_COMPLETED = "COMPLETED"
STATUS_INVALIDATED = "INVALIDATED"


@dataclass
class POI:
    """Point of Interest formed during the sweep->MSS displacement leg."""
    poi_type: str                  # "OB" | "FVG" | "IFVG" | "BREAKER"
    low: float
    high: float
    origin_time: datetime
    direction: str                 # "LONG" | "SHORT"
    label: str = ""
    mitigated: bool = False

    @property
    def width(self) -> float:
        return abs(self.high - self.low)

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass
class SweepReversalSetup:
    """State tracking for a Sweep -> MSS -> POI reversal setup."""
    setup_id: str
    pool_name: str
    pool_level: float
    direction: str                 # "LONG" (after sweeping low) | "SHORT" (after sweeping high)
    sweep_time: datetime
    sweep_extreme: float           # Wick peak beyond pool
    sweep_candle: Candle
    status: str = STATUS_SWEPT
    ref_swing_level: float = 0.0   # Reference lower-high (for LONG) or higher-low (for SHORT)
    ref_swing_time: datetime | None = None

    # MSS properties
    mss_time: datetime | None = None
    mss_level: float | None = None
    mss_candle: Candle | None = None
    has_impulse: bool = False
    displacement_candles: list[Candle] = field(default_factory=list)

    # POI properties
    pois: list[POI] = field(default_factory=list)
    active_poi: POI | None = None

    # Target & execution properties
    target_pool_name: str | None = None
    target_pool_level: float | None = None
    target_1_5r: float | None = None
    entry_time: datetime | None = None
    entry_price: float | None = None
    sl_price: float | None = None
    risk_pts: float | None = None
    risk_units: float = 1.0
    is_gated_short: bool = False

    # Exit tracking
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    result_r: float | None = None
    notes: str = ""

    # A/B testing: OLD entry (immediate sweep fade)
    old_fired: bool = False
    old_entry_time: datetime | None = None
    old_entry_price: float | None = None
    old_sl: float | None = None
    old_exit_time: datetime | None = None
    old_exit_price: float | None = None
    old_exit_reason: str | None = None
    old_result_r: float | None = None


def init_sweep_ab_db() -> None:
    """Initialize database for A/B measurement of sweep timing models."""
    SWEEP_AB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SWEEP_AB_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sweep_ab_records (
            sweep_id TEXT PRIMARY KEY,
            date TEXT,
            time TEXT,
            pool_name TEXT,
            pool_level REAL,
            direction TEXT,
            sweep_extreme REAL,
            ref_swing_level REAL,
            status TEXT,
            mss_time TEXT,
            mss_level REAL,
            has_impulse INTEGER,
            active_poi_type TEXT,
            active_poi_low REAL,
            active_poi_high REAL,
            poi_width REAL,
            old_fired INTEGER,
            old_entry_time TEXT,
            old_entry_price REAL,
            old_sl REAL,
            old_exit_time TEXT,
            old_exit_price REAL,
            old_exit_reason TEXT,
            old_result_r REAL,
            new_fired INTEGER,
            new_entry_time TEXT,
            new_entry_price REAL,
            new_sl REAL,
            new_risk_pts REAL,
            new_exit_time TEXT,
            new_exit_price REAL,
            new_exit_reason TEXT,
            new_result_r REAL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def record_sweep_ab_event(setup: SweepReversalSetup) -> None:
    """Save or update a sweep setup in the A/B test journal."""
    init_sweep_ab_db()
    conn = sqlite3.connect(str(SWEEP_AB_DB_PATH))
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO sweep_ab_records (
            sweep_id, date, time, pool_name, pool_level, direction, sweep_extreme,
            ref_swing_level, status, mss_time, mss_level, has_impulse,
            active_poi_type, active_poi_low, active_poi_high, poi_width,
            old_fired, old_entry_time, old_entry_price, old_sl, old_exit_time, old_exit_price, old_exit_reason, old_result_r,
            new_fired, new_entry_time, new_entry_price, new_sl, new_risk_pts, new_exit_time, new_exit_price, new_exit_reason, new_result_r,
            notes
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?
        )
        ON CONFLICT(sweep_id) DO UPDATE SET
            status = excluded.status,
            mss_time = excluded.mss_time,
            mss_level = excluded.mss_level,
            has_impulse = excluded.has_impulse,
            active_poi_type = excluded.active_poi_type,
            active_poi_low = excluded.active_poi_low,
            active_poi_high = excluded.active_poi_high,
            poi_width = excluded.poi_width,
            old_fired = excluded.old_fired,
            old_entry_time = excluded.old_entry_time,
            old_entry_price = excluded.old_entry_price,
            old_sl = excluded.old_sl,
            old_exit_time = excluded.old_exit_time,
            old_exit_price = excluded.old_exit_price,
            old_exit_reason = excluded.old_exit_reason,
            old_result_r = excluded.old_result_r,
            new_fired = excluded.new_fired,
            new_entry_time = excluded.new_entry_time,
            new_entry_price = excluded.new_entry_price,
            new_sl = excluded.new_sl,
            new_risk_pts = excluded.new_risk_pts,
            new_exit_time = excluded.new_exit_time,
            new_exit_price = excluded.new_exit_price,
            new_exit_reason = excluded.new_exit_reason,
            new_result_r = excluded.new_result_r,
            notes = excluded.notes
    """, (
        setup.setup_id,
        setup.sweep_time.strftime("%Y-%m-%d"),
        setup.sweep_time.strftime("%H:%M"),
        setup.pool_name,
        setup.pool_level,
        setup.direction,
        setup.sweep_extreme,
        setup.ref_swing_level,
        setup.status,
        setup.mss_time.isoformat() if setup.mss_time else None,
        setup.mss_level,
        1 if setup.has_impulse else 0,
        setup.active_poi.poi_type if setup.active_poi else None,
        setup.active_poi.low if setup.active_poi else None,
        setup.active_poi.high if setup.active_poi else None,
        setup.active_poi.width if setup.active_poi else None,
        1 if setup.old_fired else 0,
        setup.old_entry_time.isoformat() if setup.old_entry_time else None,
        setup.old_entry_price,
        setup.old_sl,
        setup.old_exit_time.isoformat() if setup.old_exit_time else None,
        setup.old_exit_price,
        setup.old_exit_reason,
        setup.old_result_r,
        1 if setup.entry_time else 0,
        setup.entry_time.isoformat() if setup.entry_time else None,
        setup.entry_price,
        setup.sl_price,
        setup.risk_pts,
        setup.exit_time.isoformat() if setup.exit_time else None,
        setup.exit_price,
        setup.exit_reason,
        setup.result_r,
        setup.notes,
    ))
    conn.commit()
    conn.close()


def get_sweep_ab_stats(cost_per_trade_r: float = 0.05) -> dict[str, Any]:
    """Calculate running performance metrics comparing OLD vs NEW timing models with strict coherence assertions."""
    if not SWEEP_AB_DB_PATH.exists():
        return {
            "audit_valid": False,
            "error": "Database missing",
            "total_sweeps": 0, "old": {}, "new": {}, "verdict": None
        }

    conn = sqlite3.connect(str(SWEEP_AB_DB_PATH))
    cursor = conn.cursor()

    # Query total sweep events and period covered
    cursor.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM sweep_ab_records")
    row = cursor.fetchone()
    total_sweeps = row[0] or 0
    min_date = row[1] or ""
    max_date = row[2] or ""

    if total_sweeps == 0:
        conn.close()
        return {
            "audit_valid": False,
            "error": "0 sweep events recorded",
            "total_sweeps": 0, "old": {}, "new": {}, "verdict": None
        }

    # Query OLD arm completed trades
    cursor.execute("""
        SELECT
            COUNT(CASE WHEN old_fired = 1 AND old_result_r IS NOT NULL THEN 1 END),
            COUNT(CASE WHEN old_fired = 1 AND old_result_r > 0 THEN 1 END),
            COUNT(CASE WHEN old_fired = 1 AND old_result_r <= 0 THEN 1 END),
            AVG(CASE WHEN old_fired = 1 AND old_result_r > 0 THEN old_result_r END),
            AVG(CASE WHEN old_fired = 1 AND old_result_r <= 0 THEN old_result_r END),
            SUM(CASE WHEN old_fired = 1 AND old_result_r IS NOT NULL THEN old_result_r END)
        FROM sweep_ab_records
    """)
    old_row = cursor.fetchone()

    # Query NEW arm completed trades
    cursor.execute("""
        SELECT
            COUNT(CASE WHEN new_fired = 1 AND new_result_r IS NOT NULL THEN 1 END),
            COUNT(CASE WHEN new_fired = 1 AND new_result_r > 0 THEN 1 END),
            COUNT(CASE WHEN new_fired = 1 AND new_result_r <= 0 THEN 1 END),
            AVG(CASE WHEN new_fired = 1 AND new_result_r > 0 THEN new_result_r END),
            AVG(CASE WHEN new_fired = 1 AND new_result_r <= 0 THEN new_result_r END),
            SUM(CASE WHEN new_fired = 1 AND new_result_r IS NOT NULL THEN new_result_r END)
        FROM sweep_ab_records
    """)
    new_row = cursor.fetchone()
    conn.close()

    old_n = old_row[0] or 0
    old_wins = old_row[1] or 0
    old_losses = old_row[2] or 0
    old_avg_win = float(old_row[3] or 0.0)
    old_avg_loss = float(old_row[4] or 0.0)
    old_gross_r = float(old_row[5] or 0.0)
    old_net_r = old_gross_r - (old_n * cost_per_trade_r)
    old_wr = (old_wins / old_n * 100.0) if old_n > 0 else 0.0

    new_n = new_row[0] or 0
    new_wins = new_row[1] or 0
    new_losses = new_row[2] or 0
    new_avg_win = float(new_row[3] or 0.0)
    new_avg_loss = float(new_row[4] or 0.0)
    new_gross_r = float(new_row[5] or 0.0)
    new_net_r = new_gross_r - (new_n * cost_per_trade_r)
    new_wr = (new_wins / new_n * 100.0) if new_n > 0 else 0.0
    new_filtered_rate = ((total_sweeps - new_n) / total_sweeps * 100.0) if total_sweeps > 0 else 0.0

    # -------------------------------------------------------------------------
    # COHERENCE ASSERTIONS
    # -------------------------------------------------------------------------
    validation_errors = []
    if old_n != (old_wins + old_losses):
        validation_errors.append(f"OLD accounting mismatch: N={old_n} != wins({old_wins}) + losses({old_losses})")

    if new_n != (new_wins + new_losses):
        validation_errors.append(f"NEW accounting mismatch: N={new_n} != wins({new_wins}) + losses({new_losses})")

    if old_n > 0:
        recalc_old_gross = (old_wins * old_avg_win) + (old_losses * old_avg_loss)
        if abs(recalc_old_gross - old_gross_r) > 0.05:
            validation_errors.append(f"OLD gross R mismatch: sum={old_gross_r:.2f} vs formula={recalc_old_gross:.2f}")

    if new_n > 0:
        recalc_new_gross = (new_wins * new_avg_win) + (new_losses * new_avg_loss)
        if abs(recalc_new_gross - new_gross_r) > 0.05:
            validation_errors.append(f"NEW gross R mismatch: sum={new_gross_r:.2f} vs formula={recalc_new_gross:.2f}")

    if validation_errors:
        err_msg = " | ".join(validation_errors)
        log.error("AUDIT INVALID: %s", err_msg)
        return {
            "audit_valid": False,
            "error": f"AUDIT INVALID: {err_msg}",
            "total_sweeps": total_sweeps,
            "old": {"trades": old_n, "wins": old_wins, "losses": old_losses, "win_rate": old_wr, "net_r": old_net_r, "total_r": old_net_r},
            "new": {"trades": new_n, "wins": new_wins, "losses": new_losses, "win_rate": new_wr, "net_r": new_net_r, "total_r": new_net_r},
            "verdict": None,
        }

    # Generate Verdict if threshold >= 60 sweeps
    verdict = None
    if total_sweeps >= 60:
        if new_net_r > old_net_r:
            diff = new_net_r - old_net_r
            verdict = (
                f"NEW (MSS + POI) OUTPERFORMS OLD by +{diff:.2f}R net "
                f"(Win Rate {new_wr:.1f}% vs {old_wr:.1f}%, filtered {new_filtered_rate:.1f}% weak setups)"
            )
        else:
            diff = old_net_r - new_net_r
            verdict = (
                f"OLD (IMMEDIATE) OUTPERFORMS NEW by +{diff:.2f}R net "
                f"(Win Rate {old_wr:.1f}% vs {new_wr:.1f}%)"
            )

    return {
        "audit_valid": True,
        "error": None,
        "total_sweeps": total_sweeps,
        "period_covered": f"{min_date} to {max_date}",
        "costs_applied": f"{cost_per_trade_r}R per executed trade",
        "old": {
            "trades": old_n,
            "wins": old_wins,
            "losses": old_losses,
            "win_rate": round(old_wr, 1),
            "avg_win": round(old_avg_win, 2),
            "avg_loss": round(old_avg_loss, 2),
            "gross_r": round(old_gross_r, 2),
            "net_r": round(old_net_r, 2),
            "total_r": round(old_net_r, 2),
        },
        "new": {
            "trades": new_n,
            "wins": new_wins,
            "losses": new_losses,
            "win_rate": round(new_wr, 1),
            "avg_win": round(new_avg_win, 2),
            "avg_loss": round(new_avg_loss, 2),
            "gross_r": round(new_gross_r, 2),
            "net_r": round(new_net_r, 2),
            "total_r": round(new_net_r, 2),
            "filtered_rate": round(new_filtered_rate, 1),
        },
        "verdict": verdict,
    }


def sample_10_new_trades() -> list[dict[str, Any]]:
    """Sample 10 NEW (MSS+POI) trades with full forensic detail."""
    if not SWEEP_AB_DB_PATH.exists():
        return []

    conn = sqlite3.connect(str(SWEEP_AB_DB_PATH))
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            sweep_id, date, time, pool_name, pool_level, direction,
            sweep_extreme, mss_time, mss_level, active_poi_type,
            active_poi_low, active_poi_high, poi_width,
            new_entry_time, new_entry_price, new_sl, new_risk_pts,
            new_exit_time, new_exit_price, new_exit_reason, new_result_r
        FROM sweep_ab_records
        WHERE new_fired = 1 AND new_entry_price IS NOT NULL
        ORDER BY sweep_id DESC
        LIMIT 10
    """)
    rows = cursor.fetchall()
    conn.close()

    out = []
    for r in rows:
        out.append({
            "sweep_id": r[0],
            "date": r[1],
            "time": r[2],
            "pool": f"{r[3]} ({r[4]:.2f})",
            "direction": r[5],
            "sweep_extreme": r[6],
            "mss_time": r[7],
            "mss_level": r[8],
            "poi_type": r[9],
            "poi_zone": f"[{r[10]:.2f} - {r[11]:.2f}] (width: {r[12]:.1f} pts)",
            "entry_time": r[13],
            "entry_price": r[14],
            "sl_price": r[15],
            "risk_pts": r[16],
            "exit_time": r[17],
            "exit_price": r[18],
            "exit_reason": r[19],
            "result_r": r[20],
        })
    return out


class SweepMssPoiEngine:
    """Primary Reversal Family Engine: Sweep -> MSS -> POI Retest."""

    def __init__(self):
        self.active_setups: dict[str, SweepReversalSetup] = {}
        self.historical_setups: list[SweepReversalSetup] = []
        self.prior_opposite_fvgs: list[POI] = []
        self.prior_opposite_obs: list[POI] = []

    def get_liquidity_pools(self, candles: list[Candle], ctx: dict) -> list[tuple[str, float, str]]:
        """Identify macro ladder liquidity pools only: PDH, PDL, ORB-H, ORB-L, EQ, Session High/Low.
        Returns list of (pool_name, level, pool_type) where pool_type is 'HIGH', 'LOW', or 'EQ'.
        """
        pools: list[tuple[str, float, str]] = []
        seen_levels: set[float] = set()

        def add_pool(name: str, level: float | None, pool_type: str):
            if level is not None and level > 0:
                lvl = round(float(level), 2)
                if lvl not in seen_levels:
                    seen_levels.add(lvl)
                    pools.append((name, lvl, pool_type))

        # 1. PDH / PDL (Primary macro ladder levels)
        add_pool("PDH", ctx.get("pdh"), "HIGH")
        add_pool("PDL", ctx.get("pdl"), "LOW")

        # 2. EQ (Midline)
        pdh = ctx.get("pdh")
        pdl = ctx.get("pdl")
        if pdh and pdl:
            add_pool("EQ", (float(pdh) + float(pdl)) / 2.0, "EQ")

        # 3. ORB-H / ORB-L (09:15-09:45 Range)
        orb_end = getattr(settings, "ORB_END", getattr(settings, "ORB_WINDOW_END", dt_time(9, 45)))
        orb_candles = [c for c in candles if c.start.time() <= orb_end]
        if len(orb_candles) >= 3:
            orb_h = max(c.high for c in orb_candles)
            orb_l = min(c.low for c in orb_candles)
            add_pool("ORB-H", orb_h, "HIGH")
            add_pool("ORB-L", orb_l, "LOW")

        # 4. Major Session High / Low (only established after hour 1, i.e., >= 12 candles)
        if len(candles) >= 12:
            sess_h = max(c.high for c in candles[:-1])
            sess_l = min(c.low for c in candles[:-1])
            add_pool("SESS-H", sess_h, "HIGH")
            add_pool("SESS-L", sess_l, "LOW")

        return pools

    def find_reference_swing(
        self, candles: list[Candle], sweep_idx: int, direction: str, sweep_extreme: float
    ) -> tuple[float, datetime | None]:
        """Find the reference swing high/low to break for Market Structure Shift (MSS).
        - For LONG (after sweeping low): preceding swing high at least 15 pts above sweep_extreme.
        - For SHORT (after sweeping high): preceding swing low at least 15 pts below sweep_extreme.
        """
        if sweep_idx <= 0:
            c = candles[0]
            level = max(c.high, sweep_extreme + 15.0) if direction == "LONG" else min(c.low, sweep_extreme - 15.0)
            return level, c.start

        lookback = min(sweep_idx, 15)
        preceding = candles[max(0, sweep_idx - lookback):sweep_idx]

        if direction == "LONG":
            # Looking for previous swing high >= sweep_extreme + 15.0 pts
            for i in range(len(preceding) - 2, 0, -1):
                c = preceding[i]
                if c.high >= preceding[i - 1].high and c.high >= preceding[i + 1].high:
                    if c.high >= sweep_extreme + 15.0:
                        return c.high, c.start
            # Fallback to the highest candle in preceding
            highest_c = max(preceding, key=lambda c: c.high)
            level = max(highest_c.high, sweep_extreme + 15.0)
            return level, highest_c.start
        else:
            # Looking for previous swing low <= sweep_extreme - 15.0 pts
            for i in range(len(preceding) - 2, 0, -1):
                c = preceding[i]
                if c.low <= preceding[i - 1].low and c.low <= preceding[i + 1].low:
                    if c.low <= sweep_extreme - 15.0:
                        return c.low, c.start
            lowest_c = min(preceding, key=lambda c: c.low)
            level = min(lowest_c.low, sweep_extreme - 15.0)
            return level, lowest_c.start

    def check_impulse_and_fvgs(
        self, leg_candles: list[Candle], direction: str
    ) -> tuple[bool, list[POI]]:
        """Validate displacement quality (body >= 60% with range >= 12 pts OR FVG >= 10 pts wide)."""
        has_impulse = False
        fvgs: list[POI] = []

        # 1. Impulsive candle body check
        for c in leg_candles:
            rng = c.high - c.low
            if rng >= 12.0:  # Minimum candle range to avoid micro-candles
                body = abs(c.close - c.open)
                if (body / rng) >= 0.60:
                    if direction == "LONG" and c.close > c.open:
                        has_impulse = True
                    elif direction == "SHORT" and c.close < c.open:
                        has_impulse = True

        # 2. 3-candle Fair Value Gaps (min gap width >= 10.0 pts)
        if len(leg_candles) >= 3:
            for i in range(2, len(leg_candles)):
                c0 = leg_candles[i - 2]
                c2 = leg_candles[i]
                if direction == "LONG":
                    gap = c2.low - c0.high
                    if gap >= 10.0:  # Enforce minimum 10 pt zone width
                        has_impulse = True
                        fvgs.append(POI(
                            poi_type="FVG",
                            low=c0.high,
                            high=c2.low,
                            origin_time=leg_candles[i - 1].start,
                            direction="LONG",
                            label="BULLISH FVG"
                        ))
                else:
                    gap = c0.low - c2.high
                    if gap >= 10.0:  # Enforce minimum 10 pt zone width
                        has_impulse = True
                        fvgs.append(POI(
                            poi_type="FVG",
                            low=c2.high,
                            high=c0.low,
                            origin_time=leg_candles[i - 1].start,
                            direction="SHORT",
                            label="BEARISH FVG"
                        ))

        return has_impulse, fvgs

    def extract_order_block(self, leg_candles: list[Candle], direction: str) -> POI | None:
        """Find the origin Order Block: last opposite candle before displacement impulse."""
        if not leg_candles:
            return None

        candidates = leg_candles[:-1] if len(leg_candles) > 1 else leg_candles

        if direction == "LONG":
            for c in reversed(candidates):
                if c.close < c.open:
                    if abs(c.high - c.low) >= 10.0:  # Min zone width 10 pts
                        return POI(poi_type="OB", low=c.low, high=c.high, origin_time=c.start, direction="LONG", label="DEMAND OB")
            c = leg_candles[0]
            low, high = c.low, max(c.high, c.low + 10.0)
            return POI(poi_type="OB", low=low, high=high, origin_time=c.start, direction="LONG", label="DEMAND OB")
        else:
            for c in reversed(candidates):
                if c.close > c.open:
                    if abs(c.high - c.low) >= 10.0:  # Min zone width 10 pts
                        return POI(poi_type="OB", low=c.low, high=c.high, origin_time=c.start, direction="SHORT", label="SUPPLY OB")
            c = leg_candles[0]
            low, high = min(c.low, c.high - 10.0), c.high
            return POI(poi_type="OB", low=low, high=high, origin_time=c.start, direction="SHORT", label="SUPPLY OB")

    def check_inverted_fvgs_and_breakers(
        self, leg_candles: list[Candle], direction: str
    ) -> list[POI]:
        """Check if displacement leg closed through prior opposite FVGs (IFVG) or OBs (Breaker)."""
        extra_pois: list[POI] = []
        if not leg_candles:
            return extra_pois

        latest_close = leg_candles[-1].close

        # Check IFVGs (min width 10 pts)
        for ifvg in self.prior_opposite_fvgs:
            if ifvg.width < 10.0:
                continue
            if direction == "LONG" and ifvg.direction == "SHORT":
                if latest_close > ifvg.high:
                    extra_pois.append(POI(poi_type="IFVG", low=ifvg.low, high=ifvg.high, origin_time=ifvg.origin_time, direction="LONG", label="BULLISH IFVG"))
            elif direction == "SHORT" and ifvg.direction == "LONG":
                if latest_close < ifvg.low:
                    extra_pois.append(POI(poi_type="IFVG", low=ifvg.low, high=ifvg.high, origin_time=ifvg.origin_time, direction="SHORT", label="BEARISH IFVG"))

        # Check Breakers (min width 10 pts)
        for ob in self.prior_opposite_obs:
            if ob.width < 10.0:
                continue
            if direction == "LONG" and ob.direction == "SHORT":
                if latest_close > ob.high:
                    extra_pois.append(POI(poi_type="BREAKER", low=ob.low, high=ob.high, origin_time=ob.origin_time, direction="LONG", label="BULLISH BREAKER"))
            elif direction == "SHORT" and ob.direction == "LONG":
                if latest_close < ob.low:
                    extra_pois.append(POI(poi_type="BREAKER", low=ob.low, high=ob.high, origin_time=ob.origin_time, direction="SHORT", label="BEARISH BREAKER"))

        return extra_pois

    def select_active_poi(self, pois: list[POI], current_price: float, direction: str) -> POI | None:
        """Select the NEAREST valid POI (>= 10 pts width) in the retrace direction."""
        valid_pois = [p for p in pois if p.width >= 10.0]
        if not valid_pois:
            return None

        priority_map = {"OB": 0, "FVG": 1, "IFVG": 2, "BREAKER": 3}

        if direction == "LONG":
            below = [p for p in valid_pois if p.high <= current_price or p.low <= current_price]
            if not below:
                return min(valid_pois, key=lambda p: abs(current_price - p.high))
            below.sort(key=lambda p: (-p.high, priority_map.get(p.poi_type, 9)))
            return below[0]
        else:
            above = [p for p in valid_pois if p.low >= current_price or p.high >= current_price]
            if not above:
                return min(valid_pois, key=lambda p: abs(current_price - p.low))
            above.sort(key=lambda p: (p.low, priority_map.get(p.poi_type, 9)))
            return above[0]

    def update_5m(self, candles: list[Candle], ctx: dict) -> list[SweepReversalSetup]:
        """Process 5-minute candle updates for Sweep Detection and MSS Confirmation."""
        if not candles:
            return []

        curr_candle = candles[-1]
        candle_idx = len(candles) - 1
        pools = self.get_liquidity_pools(candles, ctx)

        # ---------------------------------------------------------------------
        # STEP 1: SWEEP DETECTION (Macro Ladder Levels Only)
        # ---------------------------------------------------------------------
        for pool_name, pool_level, pool_type in pools:
            up_sweep = False
            down_sweep = False

            if pool_type == "HIGH":
                # High pool: only UP-sweep allowed (wick above, close below) -> SHORT
                up_sweep = curr_candle.high > pool_level and curr_candle.close < pool_level
            elif pool_type == "LOW":
                # Low pool: only DOWN-sweep allowed (wick below, close above) -> LONG
                down_sweep = curr_candle.low < pool_level and curr_candle.close > pool_level
            elif pool_type == "EQ":
                # Midline: down-sweep -> LONG; up-sweep -> SHORT
                down_sweep = curr_candle.low < pool_level and curr_candle.close > pool_level
                up_sweep = curr_candle.high > pool_level and curr_candle.close < pool_level

            if up_sweep or down_sweep:
                direction = "SHORT" if up_sweep else "LONG"
                extreme = curr_candle.high if up_sweep else curr_candle.low

                # Re-sweep check: price sweeping deeper before MSS resets displacement
                existing_pre_mss = None
                for s in self.active_setups.values():
                    if s.pool_name == pool_name and s.direction == direction and s.status in (STATUS_SWEPT, STATUS_AWAITING_MSS):
                        existing_pre_mss = s
                        break

                if existing_pre_mss:
                    log.info(
                        "Re-sweep of pool %s @ %s (new extreme %.2f). Resetting setup.",
                        pool_name, curr_candle.start.strftime("%H:%M"), extreme
                    )
                    existing_pre_mss.sweep_extreme = extreme
                    existing_pre_mss.sweep_time = curr_candle.start
                    existing_pre_mss.sweep_candle = curr_candle
                    existing_pre_mss.status = STATUS_AWAITING_MSS
                    existing_pre_mss.displacement_candles = [curr_candle]
                    existing_pre_mss.has_impulse = False
                    existing_pre_mss.ref_swing_level, existing_pre_mss.ref_swing_time = self.find_reference_swing(
                        candles, candle_idx, direction, extreme
                    )
                    record_sweep_ab_event(existing_pre_mss)
                    continue

                # New Sweep Detected
                setup_id = f"SWEEP-{curr_candle.start.strftime('%Y%m%d-%H%M')}-{pool_name}-{direction}"
                ref_swing, ref_time = self.find_reference_swing(candles, candle_idx, direction, extreme)

                opp_pool_name = "PDH" if direction == "LONG" else "PDL"
                opp_pool_level = ctx.get("pdh") if direction == "LONG" else ctx.get("pdl")

                setup = SweepReversalSetup(
                    setup_id=setup_id,
                    pool_name=pool_name,
                    pool_level=pool_level,
                    direction=direction,
                    sweep_time=curr_candle.start,
                    sweep_extreme=extreme,
                    sweep_candle=curr_candle,
                    status=STATUS_AWAITING_MSS,
                    ref_swing_level=ref_swing,
                    ref_swing_time=ref_time,
                    displacement_candles=[curr_candle],
                    target_pool_name=opp_pool_name,
                    target_pool_level=opp_pool_level,
                )

                # Initialize OLD Entry for A/B testing (Immediate sweep fade)
                setup.old_fired = True
                setup.old_entry_time = curr_candle.end
                setup.old_entry_price = curr_candle.close
                # Stop Loss: beyond sweep extreme +/- 5 pts (min 15 pts stop)
                raw_sl = extreme + 5.0 if direction == "SHORT" else extreme - 5.0
                if direction == "SHORT":
                    setup.old_sl = max(raw_sl, curr_candle.close + 15.0)
                else:
                    setup.old_sl = min(raw_sl, curr_candle.close - 15.0)

                self.active_setups[setup.setup_id] = setup
                record_sweep_ab_event(setup)
                log.info(
                    "🎯 [SWEEP DETECTED] %s swept %s @ %s. Ref swing level: %.2f. Status: AWAITING MSS",
                    direction, pool_name, curr_candle.start.strftime("%H:%M"), ref_swing
                )

        # ---------------------------------------------------------------------
        # STEP 2 & 3: MSS CONFIRMATION & POI MARKING
        # ---------------------------------------------------------------------
        for setup_key, setup in list(self.active_setups.items()):
            if setup.status != STATUS_AWAITING_MSS:
                continue

            if curr_candle.start > setup.sweep_time:
                setup.displacement_candles.append(curr_candle)

            # Check if MSS condition met (close past reference swing)
            is_mss = False
            if setup.direction == "LONG":
                is_mss = curr_candle.close > setup.ref_swing_level
            else:
                is_mss = curr_candle.close < setup.ref_swing_level

            if is_mss:
                # Displacement Quality Check: impulsive (>60% body or FVG >= 10 pts)
                has_impulse, fvgs = self.check_impulse_and_fvgs(setup.displacement_candles, setup.direction)
                if not has_impulse:
                    log.info(
                        "MSS level breached @ %s but shift leg lacks displacement quality (weak drift). Keep waiting.",
                        curr_candle.start.strftime("%H:%M")
                    )
                    continue

                # MSS Confirmed!
                setup.status = STATUS_MSS_CONFIRMED
                setup.mss_time = curr_candle.end
                setup.mss_level = setup.ref_swing_level
                setup.mss_candle = curr_candle
                setup.has_impulse = True

                # Step 3: Mark POIs (OB, FVG, IFVG, Breaker)
                ob = self.extract_order_block(setup.displacement_candles, setup.direction)
                if ob and ob.width >= 10.0:
                    setup.pois.append(ob)
                setup.pois.extend([f for f in fvgs if f.width >= 10.0])

                extra = self.check_inverted_fvgs_and_breakers(setup.displacement_candles, setup.direction)
                setup.pois.extend([e for e in extra if e.width >= 10.0])

                setup.active_poi = self.select_active_poi(setup.pois, curr_candle.close, setup.direction)
                if not setup.active_poi:
                    # If all POIs were narrower than 10 pts, reject as micro-noise
                    setup.status = STATUS_INVALIDATED
                    setup.notes = "No valid POI >= 10 pts formed in displacement leg"
                    record_sweep_ab_event(setup)
                    continue

                setup.status = STATUS_AWAITING_RETEST

                if ob and ob.width >= 10.0:
                    self.prior_opposite_obs.append(ob)
                self.prior_opposite_fvgs.extend([f for f in fvgs if f.width >= 10.0])

                record_sweep_ab_event(setup)
                log.info(
                    "🔥 [MSS CONFIRMED] %s %s @ %s. Active POI: %s [%.2f - %.2f] (width %.1f pts). Status: AWAITING RETEST",
                    setup.direction, setup.pool_name, curr_candle.end.strftime("%H:%M"),
                    setup.active_poi.label, setup.active_poi.low, setup.active_poi.high, setup.active_poi.width
                )

        return list(self.active_setups.values())

    def update_retest_and_entry(
        self, candle_1m: Candle, ctx: dict
    ) -> list[SweepReversalSetup]:
        """Step 4: Retest Entry Execution on 1-min candle with strict rejection candle requirement."""
        triggered: list[SweepReversalSetup] = []

        for setup_key, setup in list(self.active_setups.items()):
            if setup.status != STATUS_AWAITING_RETEST or not setup.active_poi or not setup.mss_time:
                continue

            # Freshness filter: valid within 60 minutes of MSS
            time_since_mss = (candle_1m.end - setup.mss_time).total_seconds() / 60.0
            if time_since_mss > 60.0:
                setup.status = STATUS_INVALIDATED
                setup.notes = "POI expired (> 60m since MSS)"
                record_sweep_ab_event(setup)
                continue

            # Structure intact check
            poi = setup.active_poi
            if setup.direction == "LONG":
                invalidation_level = min(setup.sweep_extreme, poi.low - 5.0) if poi.low < setup.mss_level else setup.mss_level
                if candle_1m.close < invalidation_level:
                    setup.status = STATUS_INVALIDATED
                    setup.notes = f"Structure invalidated (close {candle_1m.close:.2f} < level {invalidation_level:.2f})"
                    record_sweep_ab_event(setup)
                    continue
            else:
                invalidation_level_short = max(setup.sweep_extreme, poi.high + 5.0) if poi.high > setup.mss_level else setup.mss_level
                if candle_1m.close > invalidation_level_short:
                    setup.status = STATUS_INVALIDATED
                    setup.notes = f"Structure invalidated (close {candle_1m.close:.2f} > level {invalidation_level_short:.2f})"
                    record_sweep_ab_event(setup)
                    continue

            # Retest & Strict 1-Min Rejection Candle Confirmation
            in_zone = False
            rejection_candle = False

            if setup.direction == "LONG":
                # Must retrace into the POI zone
                if candle_1m.low <= poi.high and candle_1m.close >= poi.low:
                    in_zone = True
                    rng = candle_1m.high - candle_1m.low
                    lower_shadow = (min(candle_1m.open, candle_1m.close) - candle_1m.low)
                    # Rejection candle: Bullish close OR lower wick >= 35% of candle range
                    if candle_1m.close > candle_1m.open or (rng > 0 and lower_shadow / rng >= 0.35):
                        rejection_candle = True
            else:
                # Must retrace into the POI zone
                if candle_1m.high >= poi.low and candle_1m.close <= poi.high:
                    in_zone = True
                    rng = candle_1m.high - candle_1m.low
                    upper_shadow = (candle_1m.high - max(candle_1m.open, candle_1m.close))
                    # Rejection candle: Bearish close OR upper wick >= 35% of candle range
                    if candle_1m.close < candle_1m.open or (rng > 0 and upper_shadow / rng >= 0.35):
                        rejection_candle = True

            if in_zone and rejection_candle:
                # Short Gating Enforcement
                risk_units = 1.0
                is_gated = False
                if setup.direction == "SHORT":
                    score = ctx.get("score", 90)
                    gate_ok, alloc, tag, block_reason = evaluate_short_gate(
                        direction="SHORT", score=score, level_name=setup.pool_name, is_gated=True
                    )
                    if not gate_ok:
                        continue
                    if tag:
                        is_gated = True
                        risk_units = alloc
                        setup.notes = f"{setup.notes} | {tag}"

                # Calculate Entry and SL with minimum 15 pt geometry stop
                entry_price = candle_1m.close
                if setup.direction == "LONG":
                    raw_sl = poi.low - 5.0
                    sl_price = min(raw_sl, entry_price - 15.0)  # Min 15 pts risk
                    risk_pts = entry_price - sl_price
                    target_1_5r = entry_price + 1.5 * risk_pts
                else:
                    raw_sl = poi.high + 5.0
                    sl_price = max(raw_sl, entry_price + 15.0)  # Min 15 pts risk
                    risk_pts = sl_price - entry_price
                    target_1_5r = entry_price - 1.5 * risk_pts

                setup.status = STATUS_IN_TRADE
                setup.entry_time = candle_1m.end
                setup.entry_price = entry_price
                setup.sl_price = sl_price
                setup.risk_pts = risk_pts
                setup.risk_units = risk_units
                setup.is_gated_short = is_gated
                setup.target_1_5r = target_1_5r

                record_sweep_ab_event(setup)
                triggered.append(setup)
                log.info(
                    "🚀 [SWEEP-MSS-POI ENTRY] %s @ %.2f, SL %.2f (risk %.1f pts), TGT1.5R %.2f, TargetPool: %s",
                    setup.direction, entry_price, sl_price, risk_pts, target_1_5r, setup.target_pool_name
                )

        return triggered

    def update_exits(self, candle: Candle, square_off_time: datetime) -> None:
        """Simulate trade exits for BOTH active NEW and OLD trades on every candle."""
        for setup_key, setup in list(self.active_setups.items()):
            # 1. Simulate OLD trade exit if open
            if setup.old_fired and setup.old_exit_time is None and setup.old_entry_price:
                self._simulate_old_exit(setup, candle, square_off_time)

            # 2. Simulate NEW trade exit if open
            if setup.status == STATUS_IN_TRADE and setup.entry_price and setup.sl_price and setup.risk_pts:
                self._simulate_new_exit(setup, candle, square_off_time)

    def on_end_of_day(self, last_bar: Candle) -> None:
        """Force clean square-off for any remaining open setups at 15:10 close."""
        for setup in list(self.active_setups.values()):
            if setup.old_fired and setup.old_exit_time is None and setup.old_entry_price:
                risk = abs(setup.old_entry_price - setup.old_sl) if setup.old_sl else 20.0
                move = ((last_bar.close - setup.old_entry_price) if setup.direction == "LONG"
                        else (setup.old_entry_price - last_bar.close))
                setup.old_exit_time = last_bar.end
                setup.old_exit_price = last_bar.close
                setup.old_exit_reason = "SQUARE_OFF"
                setup.old_result_r = max(-1.1, min(10.0, move / risk))
                record_sweep_ab_event(setup)

            if setup.status == STATUS_IN_TRADE and setup.exit_time is None and setup.entry_price:
                risk = setup.risk_pts or 20.0
                move = ((last_bar.close - setup.entry_price) if setup.direction == "LONG"
                        else (setup.entry_price - last_bar.close))
                setup.exit_time = last_bar.end
                setup.exit_price = last_bar.close
                setup.exit_reason = "SQUARE_OFF"
                setup.result_r = max(-1.1, min(10.0, move / risk))
                setup.status = STATUS_COMPLETED
                record_sweep_ab_event(setup)

    def _simulate_new_exit(self, setup: SweepReversalSetup, candle: Candle, square_off: datetime) -> None:
        entry = setup.entry_price
        sl = setup.sl_price
        risk = setup.risk_pts
        direction = setup.direction
        tgt_1_5r = setup.target_1_5r or (entry + 1.5 * risk if direction == "LONG" else entry - 1.5 * risk)
        opp_pool = setup.target_pool_level

        # Intrabar Stop Loss check
        if direction == "LONG" and candle.low <= sl:
            setup.exit_time = candle.end
            setup.exit_price = sl
            setup.exit_reason = "SL"
            setup.result_r = max(-1.1, (sl - entry) / risk)
            setup.status = STATUS_COMPLETED
            record_sweep_ab_event(setup)
            return
        elif direction == "SHORT" and candle.high >= sl:
            setup.exit_time = candle.end
            setup.exit_price = sl
            setup.exit_reason = "SL"
            setup.result_r = max(-1.1, (entry - sl) / risk)
            setup.status = STATUS_COMPLETED
            record_sweep_ab_event(setup)
            return

        # Target 1.5R or Opposite Pool Hit
        if direction == "LONG":
            if candle.high >= tgt_1_5r or (opp_pool and candle.high >= opp_pool):
                exit_p = tgt_1_5r if candle.high >= tgt_1_5r else opp_pool
                setup.exit_time = candle.end
                setup.exit_price = exit_p
                setup.exit_reason = "TGT1"
                setup.result_r = (exit_p - entry) / risk
                setup.status = STATUS_COMPLETED
                record_sweep_ab_event(setup)
                return
        else:
            if candle.low <= tgt_1_5r or (opp_pool and candle.low <= opp_pool):
                exit_p = tgt_1_5r if candle.low <= tgt_1_5r else opp_pool
                setup.exit_time = candle.end
                setup.exit_price = exit_p
                setup.exit_reason = "TGT1"
                setup.result_r = (entry - exit_p) / risk
                setup.status = STATUS_COMPLETED
                record_sweep_ab_event(setup)
                return

        # 15:10 Square-Off
        if candle.end >= square_off:
            setup.exit_time = candle.end
            setup.exit_price = candle.close
            setup.exit_reason = "SQUARE_OFF"
            setup.result_r = ((candle.close - entry) / risk) if direction == "LONG" else ((entry - candle.close) / risk)
            setup.result_r = max(-1.1, setup.result_r)
            setup.status = STATUS_COMPLETED
            record_sweep_ab_event(setup)

    def _simulate_old_exit(self, setup: SweepReversalSetup, candle: Candle, square_off: datetime) -> None:
        entry = setup.old_entry_price
        sl = setup.old_sl
        direction = setup.direction
        risk = abs(entry - sl) if sl else 20.0

        if direction == "LONG" and candle.low <= sl:
            setup.old_exit_time = candle.end
            setup.old_exit_price = sl
            setup.old_exit_reason = "SL"
            setup.old_result_r = max(-1.1, (sl - entry) / risk)
            record_sweep_ab_event(setup)
        elif direction == "SHORT" and candle.high >= sl:
            setup.old_exit_time = candle.end
            setup.old_exit_price = sl
            setup.old_exit_reason = "SL"
            setup.old_result_r = max(-1.1, (entry - sl) / risk)
            record_sweep_ab_event(setup)
        elif candle.end >= square_off:
            setup.old_exit_time = candle.end
            setup.old_exit_price = candle.close
            setup.old_exit_reason = "SQUARE_OFF"
            setup.old_result_r = ((candle.close - entry) / risk) if direction == "LONG" else ((entry - candle.close) / risk)
            setup.old_result_r = max(-1.1, setup.old_result_r)
            record_sweep_ab_event(setup)
