"""Order Block Engine (OB-RETEST family).

Identifies Demand and Supply Order Blocks on confirmed level breaks:
- Bullish break: origin zone = last opposite (red) candle before the impulse (DEMAND OB).
- Bearish break: origin zone = last opposite (green) candle before the impulse (SUPPLY OB).
- Entry: retest of the OB zone + 1-min rejection candle off the zone.
- SL: beyond the OB extreme (tight geometry).
- Target: 1.5R + next ladder level, then structure trail.
- Tag: OB-RETEST.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

import settings
from engines.candles import Candle

log = logging.getLogger(__name__)

DEMAND = "DEMAND"
SUPPLY = "SUPPLY"


@dataclass
class OrderBlock:
    ob_id: str
    side: str                     # DEMAND | SUPPLY
    direction: str                # LONG | SHORT
    zone_low: float
    zone_high: float
    extreme: float                # zone_low for DEMAND (SL just below), zone_high for SUPPLY
    broken_level: float
    level_name: str
    origin_time: datetime
    created_time: datetime
    status: str = "ACTIVE"        # ACTIVE | FIRED | INVALIDATED
    retest_time: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "ob_id": self.ob_id,
            "side": self.side,
            "direction": self.direction,
            "zone_low": self.zone_low,
            "zone_high": self.zone_high,
            "extreme": self.extreme,
            "broken_level": self.broken_level,
            "level_name": self.level_name,
            "origin_time": self.origin_time.isoformat() if self.origin_time else None,
            "created_time": self.created_time.isoformat() if self.created_time else None,
            "status": self.status,
        }


@dataclass
class OrderBlockSignal:
    ob: OrderBlock
    candle: Candle
    direction: str
    entry_spot: float
    sl_spot: float
    tgt1_spot: float
    risk_pts: float
    family: str = "OB-RETEST"
    reasons: list = field(default_factory=list)


class OrderBlockEngine:
    """Manages order blocks across 5m breaks and 1m retests."""

    def __init__(self):
        self.zones: dict[str, OrderBlock] = {}
        self._broken_levels_seen: set[str] = set()

    def detect_order_blocks(
        self,
        candle: Candle,
        prior_candles: Sequence[Candle],
        levels: list[tuple[float, str]],
    ) -> list[OrderBlock]:
        """Detect new Order Blocks when a 5m bar breaks a key level."""
        new_obs: list[OrderBlock] = []
        if len(prior_candles) < 2:
            return new_obs

        for lvl_price, lvl_name in levels:
            if lvl_price is None:
                continue

            # Bullish break
            if candle.close > lvl_price and prior_candles[-2].close <= lvl_price:
                key = f"BULL-{lvl_name}-{candle.start.isoformat(timespec='minutes')}"
                if key not in self._broken_levels_seen:
                    self._broken_levels_seen.add(key)
                    # Origin zone = last opposite (red) candle before impulse
                    origin = None
                    for c in reversed(prior_candles[:-1]):
                        if c.close < c.open:  # Red / Bearish candle
                            origin = c
                            break
                    if origin is not None:
                        ob_id = f"OB-DEMAND-{lvl_name}-{origin.start.strftime('%H%M')}"
                        ob = OrderBlock(
                            ob_id=ob_id,
                            side=DEMAND,
                            direction="LONG",
                            zone_low=float(origin.low),
                            zone_high=float(origin.high),
                            extreme=float(origin.low),
                            broken_level=float(lvl_price),
                            level_name=lvl_name,
                            origin_time=origin.start,
                            created_time=candle.end,
                        )
                        self.zones[ob_id] = ob
                        new_obs.append(ob)
                        log.info("Detected DEMAND OB %s @ [%.2f - %.2f] on break of %s",
                                 ob_id, ob.zone_low, ob.zone_high, lvl_name)

            # Bearish break
            elif candle.close < lvl_price and prior_candles[-2].close >= lvl_price:
                key = f"BEAR-{lvl_name}-{candle.start.isoformat(timespec='minutes')}"
                if key not in self._broken_levels_seen:
                    self._broken_levels_seen.add(key)
                    # Origin zone = last opposite (green) candle before impulse
                    origin = None
                    for c in reversed(prior_candles[:-1]):
                        if c.close > c.open:  # Green / Bullish candle
                            origin = c
                            break
                    if origin is not None:
                        ob_id = f"OB-SUPPLY-{lvl_name}-{origin.start.strftime('%H%M')}"
                        ob = OrderBlock(
                            ob_id=ob_id,
                            side=SUPPLY,
                            direction="SHORT",
                            zone_low=float(origin.low),
                            zone_high=float(origin.high),
                            extreme=float(origin.high),
                            broken_level=float(lvl_price),
                            level_name=lvl_name,
                            origin_time=origin.start,
                            created_time=candle.end,
                        )
                        self.zones[ob_id] = ob
                        new_obs.append(ob)
                        log.info("Detected SUPPLY OB %s @ [%.2f - %.2f] on break of %s",
                                 ob_id, ob.zone_low, ob.zone_high, lvl_name)

        return new_obs

    def on_minute(
        self,
        minute_bar: Candle,
        ladder_levels: list[float] | None = None,
    ) -> list[OrderBlockSignal]:
        """Evaluate 1-minute bars for OB retest + rejection candle entry."""
        signals: list[OrderBlockSignal] = []

        for ob in list(self.zones.values()):
            if ob.status != "ACTIVE":
                continue

            # Invalidation: close beyond the OB extreme kills the zone
            if ob.side == DEMAND and minute_bar.close < ob.extreme:
                ob.status = "INVALIDATED"
                log.info("DEMAND OB %s invalidated on close %.2f < extreme %.2f",
                         ob.ob_id, minute_bar.close, ob.extreme)
                continue
            if ob.side == SUPPLY and minute_bar.close > ob.extreme:
                ob.status = "INVALIDATED"
                log.info("SUPPLY OB %s invalidated on close %.2f > extreme %.2f",
                         ob.ob_id, minute_bar.close, ob.extreme)
                continue

            # Retest & Rejection Check
            if ob.side == DEMAND:
                # Touched / entered the zone
                touched = minute_bar.low <= ob.zone_high and minute_bar.high >= ob.zone_low
                # 1-min rejection: bullish close off the zone
                rejection = minute_bar.close > minute_bar.open and minute_bar.close >= ob.zone_low
                if touched and rejection:
                    ob.status = "FIRED"
                    ob.retest_time = minute_bar.end
                    entry_spot = float(minute_bar.close)
                    sl_spot = round(ob.extreme - 2.0, 2)  # Tight geometry: 2 pts beyond OB extreme
                    risk_pts = max(1.0, abs(entry_spot - sl_spot))
                    tgt1 = round(entry_spot + 1.5 * risk_pts, 2)

                    sig = OrderBlockSignal(
                        ob=ob,
                        candle=minute_bar,
                        direction="LONG",
                        entry_spot=entry_spot,
                        sl_spot=sl_spot,
                        tgt1_spot=tgt1,
                        risk_pts=risk_pts,
                        reasons=[
                            ("DEMAND OB Retest", True, 40, f"Zone [{ob.zone_low:.2f} - {ob.zone_high:.2f}]"),
                            ("1m Bullish Rejection", True, 30, f"Close {minute_bar.close:.2f} > Open {minute_bar.open:.2f}"),
                            ("Tight OB Extreme SL", True, 20, f"SL {sl_spot:.2f} (Extreme {ob.extreme:.2f})"),
                        ],
                    )
                    signals.append(sig)
                    log.info("OB-RETEST SIGNAL LONG @ %.2f, SL %.2f, TGT1 %.2f", entry_spot, sl_spot, tgt1)

            elif ob.side == SUPPLY:
                # Touched / entered the zone
                touched = minute_bar.high >= ob.zone_low and minute_bar.low <= ob.zone_high
                # 1-min rejection: bearish close off the zone
                rejection = minute_bar.close < minute_bar.open and minute_bar.close <= ob.zone_high
                if touched and rejection:
                    ob.status = "FIRED"
                    ob.retest_time = minute_bar.end
                    entry_spot = float(minute_bar.close)
                    sl_spot = round(ob.extreme + 2.0, 2)  # Tight geometry: 2 pts beyond OB extreme
                    risk_pts = max(1.0, abs(entry_spot - sl_spot))
                    tgt1 = round(entry_spot - 1.5 * risk_pts, 2)

                    sig = OrderBlockSignal(
                        ob=ob,
                        candle=minute_bar,
                        direction="SHORT",
                        entry_spot=entry_spot,
                        sl_spot=sl_spot,
                        tgt1_spot=tgt1,
                        risk_pts=risk_pts,
                        reasons=[
                            ("SUPPLY OB Retest", True, 40, f"Zone [{ob.zone_low:.2f} - {ob.zone_high:.2f}]"),
                            ("1m Bearish Rejection", True, 30, f"Close {minute_bar.close:.2f} < Open {minute_bar.open:.2f}"),
                            ("Tight OB Extreme SL", True, 20, f"SL {sl_spot:.2f} (Extreme {ob.extreme:.2f})"),
                        ],
                    )
                    signals.append(sig)
                    log.info("OB-RETEST SIGNAL SHORT @ %.2f, SL %.2f, TGT1 %.2f", entry_spot, sl_spot, tgt1)

        return signals
