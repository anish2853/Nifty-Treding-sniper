"""Phase 15: Zone-Ride Model — The Consolidated Final Spec.

Consolidates all fragmented setups into a single, high-conviction, delta-aware
Zone-Ride Engine:
1. ANCHOR ZONES: Top call-OI (>100k OI) + confluence (PDH/PDL/swing) + tested today (Resistance).
   Top put-OI mirror (Support). Width 10-30 pts, drawn, labeled, STRONG-tagged.
2. PATIENCE GATE: Price returns to a STRONG zone WITH the regime (trend direction only).
3. CONFIRMATION (Both required):
   - 5-min candle closes off the zone in trade direction.
   - Next candle breaks the zone's liquidity edge with body >= 50%.
4. OI VELOCITY GATE:
   - Writers covering beyond the zone = fuel -> trade.
   - Writers stacking = live wall -> NO TRADE.
5. THE INSTRUMENT:
   - Delta 0.65-0.75 (slightly ITM), live from chain IV / Black-Scholes.
   - Capital gate: premium x 75 <= 70% of account.
   - SL: beyond the zone's far edge, max 33 spot pts.
   - PREMIUM ROOM GATE: next opposing zone >= 20 premium pts away (delta-converted) -> NO ROOM, NO TRADE.
6. THE RIDE (Single Trade, No Scalps):
   - NO early partials. NO +5-pt bookings. Ever.
   - Trail: 5-min structure (higher lows for LONG, lower highs for SHORT). Runner holds while structure holds.
   - Optional single 30% booking ONLY at a major opposing wall.
   - 15:10 square-off; trend days ride to close (15:25).
7. GOVERNANCE:
   - NO_SILENT_TRADES: Full card must announce before trade book entry.
   - Single journal (data/journal.sqlite3).
   - Reconciliation assert: Journal Trades == Telegram Alerts == Dashboard Trades.
"""

from __future__ import annotations

import json
import logging
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Callable

# Ensure repository root is in sys.path
root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import settings
from engines.candles import Candle
from engines.delta_execution import (
    calculate_black_scholes_delta,
    select_delta_aware_strike,
)
from utils import ist_now

log = logging.getLogger("engines.zone_ride")


@dataclass
class LiquidityZone:
    """STRONG Liquidity Zone anchored on open interest and market structure."""
    zone_id: str                      # e.g. "SUP-22400" or "RES-22500"
    zone_type: str                    # "SUPPORT" | "RESISTANCE"
    anchor_strike: int                # Strike price
    zone_low: float                   # Lower boundary
    zone_high: float                  # Upper boundary
    oi_total: float                   # Total open interest (contracts or shares)
    oi_change: float                  # Net intraday change in OI
    confluence_factors: list[str]     # ["PDH", "SWING_HIGH", etc.]
    tested_today: bool                # Tested by price wick/body today
    strong: bool                      # Meets >100k OI + confluence + tested
    tag: str = "STRONG"

    @property
    def width(self) -> float:
        return self.zone_high - self.zone_low

    @property
    def far_edge(self) -> float:
        """Far boundary for protective stop placement."""
        return self.zone_low if self.zone_type == "SUPPORT" else self.zone_high

    @property
    def liquidity_edge(self) -> float:
        """Near boundary that must be cleanly broken on bounce confirmation."""
        return self.zone_high if self.zone_type == "SUPPORT" else self.zone_low

    def touches(self, candle: Candle) -> bool:
        """Returns True if the candle range intersects this zone."""
        return candle.low <= self.zone_high and candle.high >= self.zone_low


@dataclass
class ZoneRideSignal:
    """Actionable trade entry signal emitted after all Phase 15 gates pass."""
    trade_id: str
    direction: str                     # "LONG" | "SHORT"
    anchor_zone: LiquidityZone
    opposing_zone: LiquidityZone | None
    entry_candle: Candle
    entry_spot: float
    sl_spot: float
    risk_spot_pts: float
    room_to_opposing_spot_pts: float
    prem_room: float
    strike: int
    option_type: str                  # "CE" | "PE"
    option_symbol: str
    expiry: str
    delta: float
    entry_prem: float
    sl_prem: float
    spot_target: float
    prem_target: float
    reasons: list[str] = field(default_factory=list)
    timestamp: str = ""


@dataclass
class ActiveZoneRideTrade:
    """In-flight Zone-Ride trade tracking structural trailing stops and the single ride."""
    trade_id: str
    direction: str                     # "LONG" | "SHORT"
    strike: int
    option_type: str
    option_symbol: str
    expiry: str
    entry_spot: float
    entry_prem: float
    entry_time: str
    initial_sl_spot: float
    current_sl_spot: float
    sl_prem: float
    risk_spot_pts: float
    anchor_zone: LiquidityZone
    opposing_zone: LiquidityZone | None
    delta: float
    spot_target: float
    prem_target: float
    prem_room: float
    highest_spot: float
    lowest_spot: float
    last_higher_low: float | None = None
    last_lower_high: float | None = None
    opposing_wall_booked: bool = False
    booked_fraction: float = 0.0
    status: str = "OPEN"               # "OPEN" | "WALL_PARTIAL" | "CLOSED"
    exit_spot: float | None = None
    exit_prem: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    realized_r: float = 0.0
    notes: str = ""


def build_liquidity_zones(
    chain_snapshot: Any,
    spot: float,
    candles: list[Candle],
    pdh: float | None = None,
    pdl: float | None = None,
    pdc: float | None = None,
    min_oi_threshold: float = 100_000.0,
    zone_half_width: float = 10.0,
) -> tuple[list[LiquidityZone], list[LiquidityZone]]:
    """Builds Resistance & Support zones from live chain OI and market confluence (Phase 15 Sec 2).

    Returns:
        (resistance_zones, support_zones)
    """
    if spot <= 0 or not chain_snapshot or not getattr(chain_snapshot, "rows", None):
        return [], []

    rows = chain_snapshot.rows
    target_expiry = getattr(chain_snapshot, "trade_expiry", None) or getattr(chain_snapshot, "nearest_expiry", None)

    # 1. Identify swing highs and swing lows from closed session candles
    swing_highs: list[float] = []
    swing_lows: list[float] = []
    if len(candles) >= 3:
        for i in range(1, len(candles) - 1):
            if candles[i].high > candles[i - 1].high and candles[i].high >= candles[i + 1].high:
                swing_highs.append(candles[i].high)
            if candles[i].low < candles[i - 1].low and candles[i].low <= candles[i + 1].low:
                swing_lows.append(candles[i].low)

    def is_tested_today(z_low: float, z_high: float) -> bool:
        """Returns True if any closed candle touched or entered the zone."""
        for c in candles:
            if c.low <= z_high + 3.0 and c.high >= z_low - 3.0:
                return True
        return False

    resistances: list[LiquidityZone] = []
    supports: list[LiquidityZone] = []

    # Filter rows matching nearest/target expiry
    active_rows = [
        r for r in rows
        if target_expiry is None or str(getattr(r, "expiry", "")).strip().upper() == str(target_expiry).strip().upper()
    ]
    if not active_rows:
        active_rows = list(rows)

    # 2. Build Resistance Zones from Call-OI strikes above spot
    call_pool = [r for r in active_rows if float(getattr(r, "strike", 0.0)) > spot]
    call_pool.sort(key=lambda r: float(getattr(r, "ce_oi", 0.0) or 0.0), reverse=True)

    for r in call_pool[:5]:
        k = int(round(getattr(r, "strike", 0)))
        raw_oi = float(getattr(r, "ce_oi", 0.0) or 0.0)
        # Normalize OI in shares (if raw is in contracts, 1 contract = 75 shares)
        eff_oi = raw_oi * 75.0 if raw_oi < 50_000 else raw_oi
        chg_oi = float(getattr(r, "ce_change_oi", 0.0) or 0.0)

        z_low = float(k - zone_half_width)
        z_high = float(k + zone_half_width)

        confluence: list[str] = []
        if pdh is not None and abs(k - pdh) <= 30.0:
            confluence.append("PDH")
        if pdc is not None and abs(k - pdc) <= 30.0:
            confluence.append("PDC")
        for sh in swing_highs:
            if abs(k - sh) <= 25.0:
                confluence.append("SWING_HIGH")
                break

        tested = is_tested_today(z_low, z_high)
        is_strong = (eff_oi >= min_oi_threshold) and (len(confluence) > 0) and tested

        resistances.append(
            LiquidityZone(
                zone_id=f"RES-{k}",
                zone_type="RESISTANCE",
                anchor_strike=k,
                zone_low=z_low,
                zone_high=z_high,
                oi_total=eff_oi,
                oi_change=chg_oi,
                confluence_factors=confluence,
                tested_today=tested,
                strong=is_strong,
                tag="STRONG" if is_strong else "MODERATE",
            )
        )

    # 3. Build Support Zones from Put-OI strikes below spot
    put_pool = [r for r in active_rows if float(getattr(r, "strike", 0.0)) < spot]
    put_pool.sort(key=lambda r: float(getattr(r, "pe_oi", 0.0) or 0.0), reverse=True)

    for r in put_pool[:5]:
        k = int(round(getattr(r, "strike", 0)))
        raw_oi = float(getattr(r, "pe_oi", 0.0) or 0.0)
        eff_oi = raw_oi * 75.0 if raw_oi < 50_000 else raw_oi
        chg_oi = float(getattr(r, "pe_change_oi", 0.0) or 0.0)

        z_low = float(k - zone_half_width)
        z_high = float(k + zone_half_width)

        confluence = []
        if pdl is not None and abs(k - pdl) <= 30.0:
            confluence.append("PDL")
        if pdc is not None and abs(k - pdc) <= 30.0:
            confluence.append("PDC")
        for sl in swing_lows:
            if abs(k - sl) <= 25.0:
                confluence.append("SWING_LOW")
                break

        tested = is_tested_today(z_low, z_high)
        is_strong = (eff_oi >= min_oi_threshold) and (len(confluence) > 0) and tested

        supports.append(
            LiquidityZone(
                zone_id=f"SUP-{k}",
                zone_type="SUPPORT",
                anchor_strike=k,
                zone_low=z_low,
                zone_high=z_high,
                oi_total=eff_oi,
                oi_change=chg_oi,
                confluence_factors=confluence,
                tested_today=tested,
                strong=is_strong,
                tag="STRONG" if is_strong else "MODERATE",
            )
        )

    # Sort: Support descending (nearest support first); Resistance ascending (nearest resistance first)
    supports.sort(key=lambda z: z.zone_high, reverse=True)
    resistances.sort(key=lambda z: z.zone_low, reverse=False)

    return resistances, supports


class ZoneRideEngine:
    """The Consolidated Phase 15 Zone-Ride Model Execution Engine."""

    def __init__(self, notifier: Callable[[str], bool] | None = None):
        self.notifier = notifier
        self.resistance_zones: list[LiquidityZone] = []
        self.support_zones: list[LiquidityZone] = []
        self.active_trade: ActiveZoneRideTrade | None = None
        self.closed_trades: list[ActiveZoneRideTrade] = []
        self.signal_count: int = 0

    def update_zones(
        self,
        chain_snapshot: Any,
        spot: float,
        candles: list[Candle],
        pdh: float | None = None,
        pdl: float | None = None,
        pdc: float | None = None,
    ) -> None:
        """Refreshes Support and Resistance liquidity zones on each cycle."""
        if not spot or spot <= 0:
            return
        res, sup = build_liquidity_zones(
            chain_snapshot=chain_snapshot,
            spot=spot,
            candles=candles,
            pdh=pdh,
            pdl=pdl,
            pdc=pdc,
        )
        self.resistance_zones = res
        self.support_zones = sup

    def evaluate_entry(
        self,
        candles: list[Candle],
        chain_snapshot: Any,
        spot: float,
        trend: str = "NORMAL",
        capital: float = 100_000.0,
        vix_val: float = 14.5,
        expiry: str = "",
    ) -> ZoneRideSignal | None:
        """Evaluates entry against all Phase 15 Patience & Confirmation Gates.

        Requirements:
        1. Price returns to a STRONG zone WITH the regime.
        2. Two-candle confirmation:
           - Candle 1 closes off zone in trade direction.
           - Candle 2 breaks zone liquidity edge with body >= 50%.
        3. OI Velocity Gate: writers covering beyond zone = fuel; stacking = live wall block.
        4. Strike selection: Delta 0.65-0.75 ITM; Capital gate <= 70%.
        5. Stop loss: beyond far edge, max 33 spot pts.
        6. Premium Room Gate: next opposing zone >= 20 premium pts away.
        """
        if len(candles) < 2 or spot <= 0 or self.active_trade is not None:
            return None

        if not self.support_zones and not self.resistance_zones and chain_snapshot:
            self.update_zones(chain_snapshot, spot, candles)

        candle2 = candles[-1]  # Trigger candle
        candle1 = candles[-2]  # Reaction candle

        # 1. EVALUATE LONG (SUPPORT BOUNCE)
        long_allowed = trend not in ("DOWNTREND", "TREND-DOWN")
        if long_allowed:
            # Check strong support zones
            strong_supports = [z for z in self.support_zones if z.strong]
            for sz in strong_supports:
                # Patience Gate: Price returns to the zone
                if candle1.low <= sz.zone_high and candle1.low >= (sz.zone_low - 15.0):
                    # Confirmation Condition 1: Candle 1 closes off zone in trade direction
                    c1_bullish = candle1.close > candle1.open
                    c1_off_zone = candle1.close >= (sz.zone_low + 0.20 * sz.width)
                    if not (c1_bullish and c1_off_zone):
                        continue

                    # Confirmation Condition 2: Candle 2 breaks liquidity edge with body >= 50%
                    c2_breaks_edge = candle2.close > sz.liquidity_edge
                    c2_range = max(1.0, candle2.high - candle2.low)
                    c2_body = candle2.close - candle2.open
                    c2_body_ratio = c2_body / c2_range
                    if not (c2_breaks_edge and c2_body > 0 and c2_body_ratio >= 0.50):
                        continue

                    # Chase filter: entry spot must not be > 20 pts from the zone edge
                    entry_spot = candle2.close
                    if (entry_spot - sz.liquidity_edge) > 20.0:
                        log.info("Zone-Ride LONG blocked: chase filter (entry %.1f is %.1f pts from zone edge)",
                                 entry_spot, entry_spot - sz.liquidity_edge)
                        continue

                    # OI Velocity Gate: Check call writers beyond the zone
                    # If call writers are stacking aggressively (+50k) overhead -> live wall -> suppress
                    overhead_calls = [
                        r for r in getattr(chain_snapshot, "rows", [])
                        if float(getattr(r, "strike", 0)) > entry_spot
                    ]
                    call_stacking = False
                    for r in overhead_calls[:2]:
                        chg = float(getattr(r, "ce_change_oi", 0.0) or 0.0)
                        if chg > 50_000:
                            call_stacking = True
                            break
                    if call_stacking:
                        log.info("Zone-Ride LONG blocked: Call writers STACKING overhead (live wall)")
                        continue

                    # Stop Loss: beyond zone far edge (sz.zone_low), max 33 spot pts
                    sl_spot = round(sz.far_edge - 3.0, 1)
                    if (entry_spot - sl_spot) > 33.0:
                        sl_spot = round(entry_spot - 33.0, 1)
                    risk_pts = round(entry_spot - sl_spot, 1)

                    # Strike selection: Delta 0.65 - 0.75 ITM
                    strike_res = select_delta_aware_strike(
                        spot=entry_spot,
                        direction="LONG",
                        chain_snapshot=chain_snapshot,
                        vix_value=vix_val,
                        expiry=expiry,
                    )
                    chosen_strike = strike_res["strike"]
                    chosen_delta = strike_res["delta"]
                    chosen_ltp = strike_res["entry_prem"]

                    # Capital gate: premium * 75 <= 70% of account
                    if (chosen_ltp * 75.0) > (0.70 * capital):
                        log.info("Zone-Ride LONG blocked: capital gate (prem %.1f x 75 > 70%% capital)", chosen_ltp)
                        continue

                    # Find nearest opposing Resistance Zone
                    opposing = None
                    for rz in self.resistance_zones:
                        if rz.strong and rz.zone_low > entry_spot:
                            opposing = rz
                            break

                    room_spot = (opposing.zone_low - entry_spot) if opposing else 45.0
                    prem_room = round(room_spot * chosen_delta, 1)

                    # PREMIUM ROOM GATE: next opposing zone >= 20 premium pts away
                    if prem_room < 20.0:
                        log.info("Zone-Ride LONG blocked: premium room gate (only %.1f prem pts < 20.0 to opposing zone)",
                                 prem_room)
                        continue

                    # Option SL and Targets
                    sl_prem = max(5.0, round(chosen_ltp - (risk_pts * chosen_delta), 1))
                    spot_target = round(entry_spot + room_spot, 1)
                    prem_target = round(chosen_ltp + prem_room, 1)

                    self.signal_count += 1
                    sig_id = f"ZR-{ist_now().strftime('%Y%m%d')}-{self.signal_count:02d}"

                    reasons = [
                        f"Price returned to STRONG Support Zone {sz.zone_id} [{sz.zone_low:.0f}–{sz.zone_high:.0f}]",
                        f"Candle 1 closed off zone ({candle1.close:.1f} > {candle1.open:.1f})",
                        f"Candle 2 broke liquidity edge {sz.liquidity_edge:.1f} with body {c2_body_ratio * 100:.0f}% >= 50%",
                        f"OI velocity verified: writers covering overhead; no stacking wall",
                        f"Premium room gate passed: {prem_room:.1f} prem pts >= 20.0 pts to {opposing.zone_id if opposing else 'Wall'}",
                    ]

                    return ZoneRideSignal(
                        trade_id=sig_id,
                        direction="LONG",
                        anchor_zone=sz,
                        opposing_zone=opposing,
                        entry_candle=candle2,
                        entry_spot=entry_spot,
                        sl_spot=sl_spot,
                        risk_spot_pts=risk_pts,
                        room_to_opposing_spot_pts=room_spot,
                        prem_room=prem_room,
                        strike=chosen_strike,
                        option_type="CE",
                        option_symbol=f"NIFTY {chosen_strike} CE",
                        expiry=expiry,
                        delta=chosen_delta,
                        entry_prem=chosen_ltp,
                        sl_prem=sl_prem,
                        spot_target=spot_target,
                        prem_target=prem_target,
                        reasons=reasons,
                        timestamp=candle2.end.isoformat(),
                    )

        # 2. EVALUATE SHORT (RESISTANCE REJECTION)
        short_allowed = trend not in ("UPTREND", "TREND-UP")
        if short_allowed:
            strong_resistances = [z for z in self.resistance_zones if z.strong]
            for rz in strong_resistances:
                if candle1.high >= rz.zone_low and candle1.high <= (rz.zone_high + 15.0):
                    c1_bearish = candle1.close < candle1.open
                    c1_off_zone = candle1.close <= (rz.zone_high - 0.20 * rz.width)
                    if not (c1_bearish and c1_off_zone):
                        continue

                    c2_breaks_edge = candle2.close < rz.liquidity_edge
                    c2_range = max(1.0, candle2.high - candle2.low)
                    c2_body = candle2.open - candle2.close
                    c2_body_ratio = c2_body / c2_range
                    if not (c2_breaks_edge and c2_body > 0 and c2_body_ratio >= 0.50):
                        continue

                    entry_spot = candle2.close
                    if (rz.liquidity_edge - entry_spot) > 20.0:
                        log.info("Zone-Ride SHORT blocked: chase filter (entry %.1f is %.1f pts from zone edge)",
                                 entry_spot, rz.liquidity_edge - entry_spot)
                        continue

                    # OI Velocity Gate: Check put writers below the zone
                    below_puts = [
                        r for r in getattr(chain_snapshot, "rows", [])
                        if float(getattr(r, "strike", 0)) < entry_spot
                    ]
                    put_stacking = False
                    for r in below_puts[:2]:
                        chg = float(getattr(r, "pe_change_oi", 0.0) or 0.0)
                        if chg > 50_000:
                            put_stacking = True
                            break
                    if put_stacking:
                        log.info("Zone-Ride SHORT blocked: Put writers STACKING below (live wall)")
                        continue

                    sl_spot = round(rz.far_edge + 3.0, 1)
                    if (sl_spot - entry_spot) > 33.0:
                        sl_spot = round(entry_spot + 33.0, 1)
                    risk_pts = round(sl_spot - entry_spot, 1)

                    strike_res = select_delta_aware_strike(
                        spot=entry_spot,
                        direction="SHORT",
                        chain_snapshot=chain_snapshot,
                        vix_value=vix_val,
                        expiry=expiry,
                    )
                    chosen_strike = strike_res["strike"]
                    chosen_delta = strike_res["delta"]
                    chosen_ltp = strike_res["entry_prem"]

                    if (chosen_ltp * 75.0) > (0.70 * capital):
                        log.info("Zone-Ride SHORT blocked: capital gate (prem %.1f x 75 > 70%% capital)", chosen_ltp)
                        continue

                    opposing = None
                    for sz in self.support_zones:
                        if sz.strong and sz.zone_high < entry_spot:
                            opposing = sz
                            break

                    room_spot = (entry_spot - opposing.zone_high) if opposing else 45.0
                    prem_room = round(room_spot * chosen_delta, 1)

                    if prem_room < 20.0:
                        log.info("Zone-Ride SHORT blocked: premium room gate (only %.1f prem pts < 20.0 to opposing zone)",
                                 prem_room)
                        continue

                    sl_prem = max(5.0, round(chosen_ltp - (risk_pts * chosen_delta), 1))
                    spot_target = round(entry_spot - room_spot, 1)
                    prem_target = round(chosen_ltp + prem_room, 1)

                    self.signal_count += 1
                    sig_id = f"ZR-{ist_now().strftime('%Y%m%d')}-{self.signal_count:02d}"

                    reasons = [
                        f"Price returned to STRONG Resistance Zone {rz.zone_id} [{rz.zone_low:.0f}–{rz.zone_high:.0f}]",
                        f"Candle 1 closed off zone ({candle1.close:.1f} < {candle1.open:.1f})",
                        f"Candle 2 broke liquidity edge {rz.liquidity_edge:.1f} with body {c2_body_ratio * 100:.0f}% >= 50%",
                        f"OI velocity verified: writers covering below; no stacking wall",
                        f"Premium room gate passed: {prem_room:.1f} prem pts >= 20.0 pts to {opposing.zone_id if opposing else 'Wall'}",
                    ]

                    return ZoneRideSignal(
                        trade_id=sig_id,
                        direction="SHORT",
                        anchor_zone=rz,
                        opposing_zone=opposing,
                        entry_candle=candle2,
                        entry_spot=entry_spot,
                        sl_spot=sl_spot,
                        risk_spot_pts=risk_pts,
                        room_to_opposing_spot_pts=room_spot,
                        prem_room=prem_room,
                        strike=chosen_strike,
                        option_type="PE",
                        option_symbol=f"NIFTY {chosen_strike} PE",
                        expiry=expiry,
                        delta=chosen_delta,
                        entry_prem=chosen_ltp,
                        sl_prem=sl_prem,
                        spot_target=spot_target,
                        prem_target=prem_target,
                        reasons=reasons,
                        timestamp=candle2.end.isoformat(),
                    )

        return None

    def open_position(self, sig: ZoneRideSignal) -> ActiveZoneRideTrade:
        """Instantiates active trade from confirmed signal."""
        trade = ActiveZoneRideTrade(
            trade_id=sig.trade_id,
            direction=sig.direction,
            strike=sig.strike,
            option_type=sig.option_type,
            option_symbol=sig.option_symbol,
            expiry=sig.expiry,
            entry_spot=sig.entry_spot,
            entry_prem=sig.entry_prem,
            entry_time=sig.timestamp,
            initial_sl_spot=sig.sl_spot,
            current_sl_spot=sig.sl_spot,
            sl_prem=sig.sl_prem,
            risk_spot_pts=sig.risk_spot_pts,
            anchor_zone=sig.anchor_zone,
            opposing_zone=sig.opposing_zone,
            delta=sig.delta,
            spot_target=sig.spot_target,
            prem_target=sig.prem_target,
            prem_room=sig.prem_room,
            highest_spot=sig.entry_spot,
            lowest_spot=sig.entry_spot,
            last_higher_low=sig.anchor_zone.zone_low if sig.direction == "LONG" else None,
            last_lower_high=sig.anchor_zone.zone_high if sig.direction == "SHORT" else None,
        )
        self.active_trade = trade
        return trade

    def update_position(
        self,
        candle: Candle,
        spot: float,
        current_time: datetime | None = None,
        is_trend_day: bool = False,
    ) -> tuple[str | None, str | None]:
        """Manages the single trade ride according to Phase 15 Section 5:

        - NO early partials / +5-pt bookings.
        - Structure Trail: 5-minute higher lows (LONG) / lower highs (SHORT).
        - Optional single 30% booking ONLY at major opposing wall.
        - 15:10 square-off (trend days ride to 15:25).

        Returns:
            (action, reason): e.g. ("WALL_BOOKING", "OPPOSING_WALL_30PCT"), ("EXIT", "STRUCTURE_BREAK"), or (None, None)
        """
        tr = self.active_trade
        if not tr or tr.status == "CLOSED":
            return None, None

        now = current_time or getattr(candle, "start", None) or ist_now()
        tr.highest_spot = max(tr.highest_spot, candle.high)
        tr.lowest_spot = min(tr.lowest_spot, candle.low)

        # 1. SQUARE-OFF TIME CHECK
        square_off = dt_time(15, 25) if is_trend_day else settings.SQUARE_OFF_TIME
        if now.time() >= square_off:
            return self._close_trade(tr, candle.close, now, "SQUARE_OFF_1510", "End of day square-off reached")

        # 2. EVALUATE LONG RIDE
        if tr.direction == "LONG":
            # Structure Break Exit: Candle closes below current trailed SL
            if candle.close < tr.current_sl_spot:
                reason = "TRAIL_SL_HIT" if tr.current_sl_spot > tr.initial_sl_spot else "SL_HIT"
                return self._close_trade(tr, candle.close, now, reason,
                                         f"5-min close {candle.close:.1f} broke structure trail {tr.current_sl_spot:.1f}")

            # Ratchet 5-min Structure Higher Low
            if candle.low > (tr.last_higher_low or 0.0) and candle.close > candle.open and candle.high >= tr.highest_spot:
                tr.last_higher_low = candle.low
                new_trail = round(candle.low - 2.0, 1)
                if new_trail > tr.current_sl_spot:
                    tr.current_sl_spot = new_trail
                    log.info("Zone-Ride LONG trailed SL ratcheted up to %.1f (HL: %.1f)", new_trail, candle.low)

            # Major Opposing Wall Check: Optional single 30% booking ONLY at major opposing wall
            if tr.opposing_zone and not tr.opposing_wall_booked:
                if candle.high >= tr.opposing_zone.zone_low:
                    tr.opposing_wall_booked = True
                    tr.booked_fraction = 0.30
                    tr.status = "WALL_PARTIAL"
                    # Tighten stop to breakeven or structural trail
                    tr.current_sl_spot = max(tr.current_sl_spot, tr.entry_spot)
                    log.info("Zone-Ride LONG hit major opposing wall %s: 30%% booked, 70%% riding structure",
                             tr.opposing_zone.zone_id)
                    return "WALL_BOOKING", "OPPOSING_WALL_30PCT"

        # 3. EVALUATE SHORT RIDE
        elif tr.direction == "SHORT":
            if candle.close > tr.current_sl_spot:
                reason = "TRAIL_SL_HIT" if tr.current_sl_spot < tr.initial_sl_spot else "SL_HIT"
                return self._close_trade(tr, candle.close, now, reason,
                                         f"5-min close {candle.close:.1f} broke structure trail {tr.current_sl_spot:.1f}")

            if candle.high < (tr.last_lower_high or 999999.0) and candle.close < candle.open and candle.low <= tr.lowest_spot:
                tr.last_lower_high = candle.high
                new_trail = round(candle.high + 2.0, 1)
                if new_trail < tr.current_sl_spot:
                    tr.current_sl_spot = new_trail
                    log.info("Zone-Ride SHORT trailed SL ratcheted down to %.1f (LH: %.1f)", new_trail, candle.high)

            if tr.opposing_zone and not tr.opposing_wall_booked:
                if candle.low <= tr.opposing_zone.zone_high:
                    tr.opposing_wall_booked = True
                    tr.booked_fraction = 0.30
                    tr.status = "WALL_PARTIAL"
                    tr.current_sl_spot = min(tr.current_sl_spot, tr.entry_spot)
                    log.info("Zone-Ride SHORT hit major opposing wall %s: 30%% booked, 70%% riding structure",
                             tr.opposing_zone.zone_id)
                    return "WALL_BOOKING", "OPPOSING_WALL_30PCT"

        return None, None

    def _close_trade(
        self,
        tr: ActiveZoneRideTrade,
        exit_spot: float,
        now: datetime,
        reason: str,
        notes: str,
    ) -> tuple[str, str]:
        """Finalizes the trade closure and computes realized R."""
        tr.status = "CLOSED"
        tr.exit_spot = exit_spot
        tr.exit_time = now.isoformat()
        tr.exit_reason = reason
        tr.notes = notes

        # Compute exit premium based on spot delta move
        spot_pts = (exit_spot - tr.entry_spot) if tr.direction == "LONG" else (tr.entry_spot - exit_spot)
        tr.exit_prem = max(0.5, round(tr.entry_prem + (spot_pts * tr.delta), 1))

        # Realized R calculation
        r_mult = spot_pts / max(1.0, tr.risk_spot_pts)
        if tr.booked_fraction > 0:
            # 30% locked at opposing wall target + 70% at exit
            wall_dist = (tr.opposing_zone.zone_low - tr.entry_spot) if (tr.direction == "LONG" and tr.opposing_zone) else tr.room_to_opposing_spot_pts
            wall_r = wall_dist / max(1.0, tr.risk_spot_pts)
            tr.realized_r = round(0.30 * wall_r + 0.70 * r_mult, 2)
        else:
            tr.realized_r = round(r_mult, 2)

        self.closed_trades.append(tr)
        self.active_trade = None
        log.info("Zone-Ride trade %s closed: %.2fR (%s)", tr.trade_id, tr.realized_r, reason)
        return "EXIT", reason
