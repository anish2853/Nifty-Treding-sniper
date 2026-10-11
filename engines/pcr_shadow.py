"""PCR Gate Shadow Tracking Engine (measure, don't delete).

Tracks every setup blocked by PCR < 0.70 (or mean-reversion regime from low PCR).
Hypothetical trades are simulated through their lifecycle:
- SL hit -> Gate SAVED a loss (+ rupees saved)
- Target hit -> Gate COST a win (+ rupees cost)
- EOD Square-Off -> Evaluated at close

After 60 blocked events:
- Net saved > net cost -> Gate proven beneficial
- Net cost > net saved -> Recommendation to lift or disable gate
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import settings
from utils import ist_now

log = logging.getLogger(__name__)

PCR_SHADOW_DB_PATH = Path("data/pcr_shadow.sqlite3")


def init_pcr_shadow_db() -> None:
    PCR_SHADOW_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(PCR_SHADOW_DB_PATH))
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pcr_shadow_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            event_time TEXT NOT NULL,
            family TEXT NOT NULL,
            direction TEXT NOT NULL,
            strike INTEGER,
            entry_spot REAL,
            entry_prem REAL,
            sl_spot REAL,
            sl_prem REAL,
            tgt_prem REAL,
            pcr REAL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            exit_time TEXT,
            exit_prem REAL,
            exit_reason TEXT,
            result_r REAL,
            pnl_rupees REAL,
            gate_effect TEXT,
            notes TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pcr_shadow_date ON pcr_shadow_records(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pcr_shadow_status ON pcr_shadow_records(status)")
    conn.commit()
    conn.close()


@dataclass
class PCRShadowTrade:
    id: int | None
    date: str
    event_time: datetime
    family: str
    direction: str
    strike: int
    entry_spot: float
    entry_prem: float
    sl_spot: float
    sl_prem: float
    tgt_prem: float
    pcr: float
    status: str = "OPEN"
    exit_time: datetime | None = None
    exit_prem: float | None = None
    exit_reason: str | None = None
    result_r: float | None = None
    pnl_rupees: float | None = None
    gate_effect: str | None = None
    notes: str = ""


class PCRShadowTracker:
    """Manages recording and lifecycle simulation of PCR-blocked setups."""

    def __init__(self):
        init_pcr_shadow_db()
        self.active_shadows: list[PCRShadowTrade] = self._load_open_shadows()

    def _load_open_shadows(self) -> list[PCRShadowTrade]:
        if not PCR_SHADOW_DB_PATH.exists():
            return []
        conn = sqlite3.connect(str(PCR_SHADOW_DB_PATH))
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, date, event_time, family, direction, strike, entry_spot,
                   entry_prem, sl_spot, sl_prem, tgt_prem, pcr, status, notes
            FROM pcr_shadow_records
            WHERE status = 'OPEN'
        """)
        rows = cursor.fetchall()
        conn.close()
        shadows = []
        for r in rows:
            try:
                dt = datetime.fromisoformat(r[2])
            except Exception:
                dt = ist_now()
            shadows.append(PCRShadowTrade(
                id=r[0], date=r[1], event_time=dt, family=r[3], direction=r[4],
                strike=r[5] or 0, entry_spot=r[6] or 0.0, entry_prem=r[7] or 100.0,
                sl_spot=r[8] or 0.0, sl_prem=r[9] or 75.0, tgt_prem=r[10] or 150.0,
                pcr=r[11] or 0.0, status=r[12], notes=r[13] or ""
            ))
        return shadows

    def record_blocked(
        self,
        family: str,
        direction: str,
        entry_spot: float,
        sl_spot: float,
        pcr: float,
        strike: int | None = None,
        entry_prem: float | None = None,
        notes: str = "",
    ) -> PCRShadowTrade:
        """Record a setup blocked by the PCR gate."""
        now = ist_now()
        date_str = now.strftime("%Y-%m-%d")

        # Premium heuristics if not explicitly given
        prem = entry_prem if entry_prem and entry_prem > 0 else 100.0
        # 25% premium stop loss rule
        sl_p = prem * getattr(settings, "SL_PREMIUM_FRACTION", 0.75)
        # 1.5R target = prem + 1.5 * (prem - sl_p)
        risk = prem - sl_p
        tgt_p = prem + 1.5 * risk

        trade = PCRShadowTrade(
            id=None,
            date=date_str,
            event_time=now,
            family=family,
            direction=direction,
            strike=strike or 0,
            entry_spot=entry_spot,
            entry_prem=prem,
            sl_spot=sl_spot,
            sl_prem=sl_p,
            tgt_prem=tgt_p,
            pcr=pcr,
            status="OPEN",
            notes=notes,
        )

        conn = sqlite3.connect(str(PCR_SHADOW_DB_PATH))
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO pcr_shadow_records (
                date, event_time, family, direction, strike, entry_spot,
                entry_prem, sl_spot, sl_prem, tgt_prem, pcr, status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.date, trade.event_time.isoformat(), trade.family, trade.direction,
            trade.strike, trade.entry_spot, trade.entry_prem, trade.sl_spot,
            trade.sl_prem, trade.tgt_prem, trade.pcr, trade.status, trade.notes
        ))
        trade.id = cursor.lastrowid
        conn.commit()
        conn.close()

        self.active_shadows.append(trade)
        log.info(
            "🛡️ [PCR SHADOW RECORDED] #%s %s %s blocked by PCR %.2f < 0.70. Entry %.2f, SL %.2f",
            trade.id, trade.direction, trade.family, trade.pcr, trade.entry_spot, trade.sl_spot
        )
        return trade

    def update_exits(
        self,
        spot: float | None,
        chain: Any,
        now: datetime,
        square_off_time: datetime,
    ) -> None:
        """Simulate trade exit for all open shadow trades."""
        if not self.active_shadows or spot is None:
            return

        lot_size = getattr(settings, "NIFTY_LOT_SIZE", 25)

        for trade in list(self.active_shadows):
            # Check option LTP if strike exists in chain
            current_prem = None
            if chain and trade.strike and hasattr(chain, "rows"):
                side = "PE" if trade.direction == "SHORT" else "CE"
                for row in chain.rows:
                    if row.strike == trade.strike:
                        current_prem = getattr(row, f"{side.lower()}_ltp", None)
                        break

            # If option LTP unavailable, approximate from spot movement
            if current_prem is None:
                delta = 0.5
                spot_diff = (spot - trade.entry_spot) if trade.direction == "LONG" else (trade.entry_spot - spot)
                current_prem = max(0.0, trade.entry_prem + delta * spot_diff)

            exit_hit = False
            exit_reason = None
            exit_prem = None

            # 1. Stop Loss check
            hit_spot_sl = (spot <= trade.sl_spot) if trade.direction == "LONG" else (spot >= trade.sl_spot)
            hit_prem_sl = current_prem <= trade.sl_prem

            if hit_spot_sl or hit_prem_sl:
                exit_hit = True
                exit_reason = "SL"
                exit_prem = trade.sl_prem
            elif current_prem >= trade.tgt_prem:
                exit_hit = True
                exit_reason = "TGT1"
                exit_prem = trade.tgt_prem
            elif now >= square_off_time:
                exit_hit = True
                exit_reason = "SQUARE_OFF"
                exit_prem = current_prem

            if exit_hit:
                risk = trade.entry_prem - trade.sl_prem
                if risk <= 0:
                    risk = 25.0
                pnl_per_qty = exit_prem - trade.entry_prem
                result_r = max(-1.1, min(5.0, pnl_per_qty / risk))
                pnl_rupees = pnl_per_qty * lot_size

                # If the hypothetical trade lost -> gate SAVED money.
                # If the hypothetical trade won -> gate COST money.
                gate_effect = "SAVED" if result_r <= 0 else "COST"

                trade.status = "CLOSED"
                trade.exit_time = now
                trade.exit_prem = exit_prem
                trade.exit_reason = exit_reason
                trade.result_r = round(result_r, 2)
                trade.pnl_rupees = round(pnl_rupees, 2)
                trade.gate_effect = gate_effect

                self._sync_trade_to_db(trade)
                self.active_shadows.remove(trade)
                log.info(
                    "📊 [PCR SHADOW CLOSED] #%s %s %s: %s @ %.2f -> %s (result: %+.2fR, ₹%+.0f, Gate Effect: %s)",
                    trade.id, trade.direction, trade.family, exit_reason, exit_prem,
                    gate_effect, result_r, pnl_rupees, gate_effect
                )

    def _sync_trade_to_db(self, trade: PCRShadowTrade) -> None:
        if not trade.id:
            return
        conn = sqlite3.connect(str(PCR_SHADOW_DB_PATH))
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE pcr_shadow_records
            SET status = ?, exit_time = ?, exit_prem = ?, exit_reason = ?,
                result_r = ?, pnl_rupees = ?, gate_effect = ?
            WHERE id = ?
        """, (
            trade.status, trade.exit_time.isoformat() if trade.exit_time else None,
            trade.exit_prem, trade.exit_reason, trade.result_r, trade.pnl_rupees,
            trade.gate_effect, trade.id
        ))
        conn.commit()
        conn.close()


def get_pcr_shadow_stats() -> dict[str, Any]:
    """Calculate running performance totals for the PCR < 0.70 gate."""
    if not PCR_SHADOW_DB_PATH.exists():
        return {
            "total_blocked": 0,
            "closed_count": 0,
            "saved_count": 0,
            "cost_count": 0,
            "saved_rupees": 0.0,
            "cost_rupees": 0.0,
            "net_rupees": 0.0,
            "report_text": "PCR gate — 0 blocked events recorded",
            "verdict": None,
        }

    conn = sqlite3.connect(str(PCR_SHADOW_DB_PATH))
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            COUNT(*),
            COUNT(CASE WHEN status = 'CLOSED' THEN 1 END),
            COUNT(CASE WHEN gate_effect = 'SAVED' THEN 1 END),
            COUNT(CASE WHEN gate_effect = 'COST' THEN 1 END),
            COALESCE(SUM(CASE WHEN gate_effect = 'SAVED' THEN ABS(pnl_rupees) ELSE 0 END), 0.0),
            COALESCE(SUM(CASE WHEN gate_effect = 'COST' THEN pnl_rupees ELSE 0 END), 0.0)
        FROM pcr_shadow_records
    """)
    row = cursor.fetchone()
    conn.close()

    total = row[0] or 0
    closed = row[1] or 0
    saved_cnt = row[2] or 0
    cost_cnt = row[3] or 0
    saved_rs = float(row[4] or 0.0)
    cost_rs = float(row[5] or 0.0)
    net_rs = saved_rs - cost_rs

    verdict = None
    if total >= 60:
        if net_rs > 0:
            verdict = f"PCR GATE PROVEN BENEFICIAL: Net +₹{net_rs:,.0f} saved across {total} events. Gate stays active."
        else:
            verdict = f"PCR GATE HARMFUL: Net -₹{abs(net_rs):,.0f} lost across {total} events. RECOMMENDATION: DISABLE PCR GATE."

    report_text = f"saved ₹{saved_rs:,.0f} | cost ₹{cost_rs:,.0f} (net: ₹{net_rs:+,.0f}, {total}/60 events)"

    return {
        "total_blocked": total,
        "closed_count": closed,
        "saved_count": saved_cnt,
        "cost_count": cost_cnt,
        "saved_rupees": round(saved_rs, 2),
        "cost_rupees": round(cost_rs, 2),
        "net_rupees": round(net_rs, 2),
        "report_text": report_text,
        "verdict": verdict,
    }
