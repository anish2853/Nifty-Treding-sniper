"""Shadow-Compliant Comparison Engine (Phase 13.4).

Quantifies the exact economic cost of execution non-compliance.
Whenever a signal is evaluated or fired, this engine runs a parallel
shadow simulation of the strict FROZEN-v1 compliant version:
- Retest entry: Pullback to broken level / POI
- Tight SL: Retest structure (<= 33 pts, ~20-25 pts)
- Exit mechanics: Partial 50% at +1.0R, runner structural trail

Outputs EOD comparison:
"FIRED: X R | COMPLIANT shadow: Y R | cost of violation: X - Y"
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import settings
from utils import ist_now

log = logging.getLogger("shadow_compliant")


@dataclass
class ShadowTrade:
    trade_id: str
    strategy_id: str
    direction: str
    broken_level: float
    retest_entry_price: float
    retest_sl: float
    risk_pts: float
    entry_time: str
    fired_r: float = 0.0
    shadow_r: float = 0.0
    status: str = "OPEN"  # OPEN, TGT1_HIT, CLOSED
    half_booked: bool = False
    runner_sl: float = 0.0
    closed_at: str | None = None
    notes: str = ""
    live_trade_id: str | None = None

    def update_candle(self, high: float, low: float, close: float, candle_time: str) -> None:
        if self.status == "CLOSED":
            return

        if self.direction == "LONG":
            # 1. Check stop loss
            effective_sl = self.runner_sl if self.half_booked else self.retest_sl
            if low <= effective_sl:
                # Stopped out
                if self.half_booked:
                    # Half booked at +1.0R, other half stopped at BE (0R) -> net +0.50R
                    self.shadow_r = 0.50
                    self.status = "CLOSED"
                    self.notes = "TGT1 + BE Stop"
                else:
                    self.shadow_r = -1.0
                    self.status = "CLOSED"
                    self.notes = "Full SL hit"
                self.closed_at = candle_time
                return

            # 2. Check TGT1 (+1.0R = entry + risk_pts)
            tgt1 = self.retest_entry_price + self.risk_pts
            if not self.half_booked and high >= tgt1:
                self.half_booked = True
                self.runner_sl = self.retest_entry_price  # Move SL to entry
                self.status = "TGT1_HIT"
                self.notes = "Partial 50% booked at +1.0R, SL to BE"

            # 3. Check TGT2 (+2.0R)
            tgt2 = self.retest_entry_price + (2.0 * self.risk_pts)
            if self.half_booked and high >= tgt2:
                # 50% at +1.0R, 50% at +2.0R -> net +1.50R
                self.shadow_r = 1.50
                self.status = "CLOSED"
                self.closed_at = candle_time
                self.notes = "Full Target (+1.5R blended)"
                return

        else:  # SHORT
            effective_sl = self.runner_sl if self.half_booked else self.retest_sl
            if high >= effective_sl:
                if self.half_booked:
                    self.shadow_r = 0.50
                    self.status = "CLOSED"
                    self.notes = "TGT1 + BE Stop"
                else:
                    self.shadow_r = -1.0
                    self.status = "CLOSED"
                    self.notes = "Full SL hit"
                self.closed_at = candle_time
                return

            tgt1 = self.retest_entry_price - self.risk_pts
            if not self.half_booked and low <= tgt1:
                self.half_booked = True
                self.runner_sl = self.retest_entry_price
                self.status = "TGT1_HIT"
                self.notes = "Partial 50% booked at +1.0R, SL to BE"

            tgt2 = self.retest_entry_price - (2.0 * self.risk_pts)
            if self.half_booked and low <= tgt2:
                self.shadow_r = 1.50
                self.status = "CLOSED"
                self.closed_at = candle_time
                self.notes = "Full Target (+1.5R blended)"
                return


class ShadowCompliantLedger:
    """Manages parallel shadow execution of FROZEN-v1 compliant trades."""

    def __init__(self, data_file: Path | None = None):
        self.data_file = data_file or (settings.DATA_DIR / "shadow_compliant_ledger.json")
        self.trades: list[ShadowTrade] = []
        self._load()

    def _load(self) -> None:
        if not self.data_file.exists():
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.trades = [ShadowTrade(**t) for t in data.get("trades", [])]
        except Exception as exc:
            log.warning("Could not load shadow ledger: %s", exc)

    def save(self) -> None:
        try:
            payload = {
                "updated_at": ist_now().isoformat(),
                "trades": [t.__dict__ for t in self.trades],
            }
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            log.warning("Could not save shadow ledger: %s", exc)

    def record_signal_shadow(
        self,
        strategy_id: str,
        direction: str,
        broken_level: float,
        spot_close: float,
        initial_sl: float,
        fired_r: float = 0.0,
        retest_confirmed: bool = False,
        live_trade_id: str | None = None,
        entry_time: str | None = None,
    ) -> ShadowTrade:
        """Create a shadow compliant trade alongside any fired or evaluated signal."""
        # Compliant retest price is at broken level pullback
        retest_price = broken_level if broken_level > 0 else spot_close

        # Compliant SL is capped at 25-33 pts
        if direction == "LONG":
            raw_risk = retest_price - initial_sl
            risk_pts = min(25.0, max(12.0, raw_risk))
            compliant_sl = retest_price - risk_pts
        else:
            raw_risk = initial_sl - retest_price
            risk_pts = min(25.0, max(12.0, raw_risk))
            compliant_sl = retest_price + risk_pts

        trade_id = f"SHADOW-{len(self.trades) + 1:03d}"
        trade = ShadowTrade(
            trade_id=trade_id,
            strategy_id=strategy_id,
            direction=direction,
            broken_level=broken_level,
            retest_entry_price=retest_price,
            retest_sl=compliant_sl,
            risk_pts=risk_pts,
            entry_time=entry_time or ist_now().isoformat(),
            fired_r=fired_r,
            shadow_r=0.0,
            status="OPEN",
            notes="FROZEN-v1 strict shadow simulation",
            live_trade_id=live_trade_id,
        )
        self.trades.append(trade)
        self.save()
        log.info("Opened Shadow Compliant Trade %s [%s %s @ %.2f, SL %.2f (%.1f pts risk)]",
                 trade_id, strategy_id, direction, retest_price, compliant_sl, risk_pts)
        return trade

    def update_candles(self, high: float, low: float, close: float, candle_time: str) -> None:
        for t in self.trades:
            if t.status in ("OPEN", "TGT1_HIT"):
                t.update_candle(high, low, close, candle_time)
        self.save()

    def update_fired_r(self, trade_id: str, final_r: float) -> None:
        for t in self.trades:
            if t.trade_id == trade_id or t.live_trade_id == trade_id:
                t.fired_r = final_r
        self.save()

    def get_eod_summary(self, today_iso: str | None = None) -> dict[str, Any]:
        """Compute running totals: FIRED R vs COMPLIANT SHADOW R vs Cost of Violation."""
        iso = today_iso or ist_now().date().isoformat()
        day_trades = [t for t in self.trades if t.entry_time.startswith(iso)]

        fired_sum = sum(t.fired_r for t in day_trades)
        shadow_sum = sum(t.shadow_r for t in day_trades)
        cost_of_violation = fired_sum - shadow_sum

        # Cumulative all-time totals
        cum_fired = sum(t.fired_r for t in self.trades)
        cum_shadow = sum(t.shadow_r for t in self.trades)
        cum_cost = cum_fired - cum_shadow

        report_txt = (
            f"FIRED: {fired_sum:+.2f}R | COMPLIANT shadow: {shadow_sum:+.2f}R | "
            f"cost of violation: {cost_of_violation:+.2f}R"
        )
        if len(self.trades) > len(day_trades):
            report_txt += f" (Cumulative: Fired {cum_fired:+.2f}R vs Shadow {cum_shadow:+.2f}R | Delta: {cum_cost:+.2f}R)"

        return {
            "day_fired_r": round(fired_sum, 2),
            "day_shadow_r": round(shadow_sum, 2),
            "day_cost_of_violation": round(cost_of_violation, 2),
            "cum_fired_r": round(cum_fired, 2),
            "cum_shadow_r": round(cum_shadow, 2),
            "cum_cost_of_violation": round(cum_cost, 2),
            "report_text": report_txt,
            "trade_count": len(day_trades),
        }
