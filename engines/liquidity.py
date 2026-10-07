"""Liquidity map (Phase B+ module 1).

Recomputed on every chain poll from the nearest-expiry rows: S1-S3 = top-3 PUT-OI
strikes below spot (supports), R1-R3 = top-3 CALL-OI strikes above spot
(resistances), each with a change-in-OI arrow (writing ↑ / unwinding ↓ / flat →)
and distance from spot. A level is STRONG when it sits within
STRONG_LEVEL_BAND_PCT (0.15%) of PDH/PDL. PDH/PDL themselves are shown as
liquidity pools. The sweep-fade engine sweeps these STRONG levels + pools.
"""
import logging
from dataclasses import dataclass, field

import settings

log = logging.getLogger(__name__)


@dataclass
class OILevel:
    side: str                  # 'S' put wall below spot | 'R' call wall above
    rank: int                  # 1..LIQUIDITY_TOP_N
    strike: int
    oi: float
    change_oi: float
    distance_pts: float
    distance_pct: float
    strong: bool = False

    @property
    def arrow(self) -> str:
        if self.change_oi > 0:
            return "↑"         # fresh writing
        if self.change_oi < 0:
            return "↓"         # unwinding
        return "→"

    @property
    def label(self) -> str:
        return f"{self.side}{self.rank}"


@dataclass
class LiquidityMap:
    ts: str | None
    spot: float
    levels: list = field(default_factory=list)   # OILevel: S1..S3 then R1..R3
    pools: list = field(default_factory=list)    # [(name, level, pct_from_spot)]
    pcr_total: float | None = None

    def ladder_lines(self) -> list:
        lines = ["LIQUIDITY MAP"]
        for name, level, pct in self.pools:
            lines.append(f"POOL {name} {level:,.0f} ({pct:+.2f}% from spot)")
        for lv in self.levels:
            strong = " STRONG" if lv.strong else ""
            lines.append(f"{lv.label} {lv.strike:,.0f} OI {lv.oi:,.0f} {lv.arrow} "
                         f"{lv.change_oi:+,.0f} | {lv.distance_pts:+,.0f} pts "
                         f"({lv.distance_pct:+.2f}%){strong}")
        return lines

    def strong_strikes(self) -> list:
        return [lv.strike for lv in self.levels if lv.strong]


def room_to_run(levels, spot, direction, pdh=None, pdl=None,
                broken_level=None, max_oi_strike=None):
    """Room-to-run gate (Trend-Day v2.1): distance to the nearest SIGNIFICANT
    OI wall beyond the entry (R-levels for LONG / S-levels for SHORT with
    OI > WALL_OI_MIN, plus the max-OI strike), and the measured move
    (yesterday's PDH-PDL range projected from the break). The nearest wall or
    measured-move target defines the available runway.
    Returns (runway_pts | None, wall_label | None)."""
    if spot is None:
        return None, None
    walls = []
    if direction == "LONG":
        for lv in levels or []:
            if lv.side == "R" and lv.strike > spot and lv.oi > settings.WALL_OI_MIN:
                walls.append((lv.strike - spot,
                              f"{lv.strike:,.0f} (OI {lv.oi / 1000:.0f}k)"))
        if max_oi_strike is not None and max_oi_strike > spot:
            walls.append((max_oi_strike - spot, f"{max_oi_strike:,.0f} (max OI)"))
        if pdh is not None and pdl is not None and pdh > spot:
            rng = pdh - pdl
            base = broken_level if (broken_level is not None
                                    and broken_level > spot) else spot
            walls.append((base + rng - spot,
                          f"measured move {base + rng:,.0f} (PDH-PDL range)"))
    else:
        for lv in levels or []:
            if lv.side == "S" and lv.strike < spot and lv.oi > settings.WALL_OI_MIN:
                walls.append((spot - lv.strike,
                              f"{lv.strike:,.0f} (OI {lv.oi / 1000:.0f}k)"))
        if max_oi_strike is not None and max_oi_strike < spot:
            walls.append((spot - max_oi_strike, f"{max_oi_strike:,.0f} (max OI)"))
        if pdh is not None and pdl is not None and pdl < spot:
            rng = pdh - pdl
            base = broken_level if (broken_level is not None
                                    and broken_level < spot) else spot
            walls.append((spot - (base - rng),
                          f"measured move {base - rng:,.0f} (PDH-PDL range)"))
    if not walls:
        return None, None
    return min(walls)


def _strong(strike: float, pdh, pdl) -> bool:
    band = settings.STRONG_LEVEL_BAND_PCT
    if pdh is not None and abs(strike - pdh) <= abs(pdh) * band:
        return True
    if pdl is not None and abs(strike - pdl) <= abs(pdl) * band:
        return True
    return False


def compute_liquidity_map(rows, spot, nearest_expiry, pdh=None, pdl=None,
                          ts: str | None = None, pcr_total=None) -> LiquidityMap | None:
    """rows: duck-typed StrikeRow-like objects (strike, ce_oi, pe_oi,
    ce_change_oi, pe_change_oi, expiry) - the parser's rows or dashboard stand-ins."""
    if spot is None:
        return None
    pool_rows = [r for r in rows
                 if getattr(r, "expiry", None) == nearest_expiry or nearest_expiry is None]

    def top(side: str):
        if side == "S":
            pool = [r for r in pool_rows
                    if r.pe_oi is not None and r.strike < spot]
            key = lambda r: r.pe_oi
            change = lambda r: r.pe_change_oi or 0.0
        else:
            pool = [r for r in pool_rows
                    if r.ce_oi is not None and r.strike > spot]
            key = lambda r: r.ce_oi
            change = lambda r: r.ce_change_oi or 0.0
        ranked = sorted(pool, key=key, reverse=True)[:settings.LIQUIDITY_TOP_N]
        levels = []
        for rank, r in enumerate(ranked, 1):
            oi = float(key(r))
            chg = float(change(r))
            distance = (spot - r.strike) if side == "S" else (r.strike - spot)
            levels.append(OILevel(side=side, rank=rank, strike=int(r.strike),
                                  oi=oi, change_oi=chg,
                                  distance_pts=distance,
                                  distance_pct=distance / spot * 100 if spot else 0.0,
                                  strong=_strong(r.strike, pdh, pdl)))
        return levels

    levels = top("S") + top("R")
    pools = []
    for name, level in (("PDH", pdh), ("PDL", pdl)):
        if level is not None:
            pools.append((name, float(level), (level - spot) / spot * 100))
    return LiquidityMap(ts=ts, spot=float(spot), levels=levels, pools=pools,
                        pcr_total=pcr_total)
