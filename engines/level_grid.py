"""Unified price-structure and OI level ladder for live decisions/dashboard."""
from dataclasses import dataclass

import settings


@dataclass
class GridLevel:
    level_id: str
    label: str
    price: float
    side: str
    sources: tuple[str, ...]
    oi: float | None = None
    change_oi: float | None = None
    rank: int | None = None
    tested: bool = False
    held: bool = False
    strong: bool = False

    @property
    def bias(self) -> str:
        if self.change_oi is None or self.change_oi == 0:
            return "NEUTRAL"
        return "HOLD" if self.change_oi > 0 else "BREAK"


def _pivot_levels(candles):
    """Confirmed 5m pivots use two completed bars on each side."""
    levels = []
    for index in range(2, len(candles) - 2):
        candidate = candles[index]
        neighbors = candles[index - 2:index] + candles[index + 1:index + 3]
        if all(candidate.high > candle.high for candle in neighbors):
            levels.append((f"SWING-H-{candidate.start.isoformat()}",
                           "SWING-H", float(candidate.high), "R"))
        if all(candidate.low < candle.low for candle in neighbors):
            levels.append((f"SWING-L-{candidate.start.isoformat()}",
                           "SWING-L", float(candidate.low), "S"))
    return levels


def build_level_grid(*, spot, pdh=None, pdl=None, pdc=None, orb=None,
                     candles=(), vwap=None, liquidity=None):
    """Return one price-sorted ladder; OI walls are STRONG only after a held test
    and 0.1% confluence with a price-structure reference."""
    if spot is None:
        return []
    structures = []
    for label, price, side in (
            ("PDH", pdh, "R"), ("PDL", pdl, "S"),
            ("PDC", pdc, "R" if pdc is not None and pdc >= spot else "S")):
        if price is not None:
            structures.append((label, float(price), side))
    if orb:
        structures.extend((label, float(orb[key]), side) for label, key, side in
                          (("ORB-H", "high", "R"), ("ORB-L", "low", "S")))
    structures.extend((label, price, side)
                       for _level_id, label, price, side in _pivot_levels(candles))

    result = [GridLevel(label, label, price, side, (label,))
              for label, price, side in structures]
    if vwap is not None:
        result.append(GridLevel("VWAP", "VWAP", float(vwap),
                                "S" if spot >= vwap else "R", ("VWAP",)))

    for wall in getattr(liquidity, "levels", ()) if liquidity else ():
        structure_confluence = any(
            abs(wall.strike - price) <= abs(price) * settings.LEVEL_CONFLUENCE_PCT
            for _label, price, _side in structures)
        touches = [candle for candle in candles
                   if candle.low <= wall.strike + settings.LEVEL_TEST_TOLERANCE_PTS
                   and candle.high >= wall.strike - settings.LEVEL_TEST_TOLERANCE_PTS]
        held = any(candle.close <= wall.strike if wall.side == "R"
                   else candle.close >= wall.strike for candle in touches)
        significant = (wall.oi > settings.WALL_OI_MIN or wall.rank <= 2)
        strong = significant and structure_confluence and bool(touches) and held
        result.append(GridLevel(
            f"OI-{wall.label}-{wall.strike}", wall.label, float(wall.strike),
            wall.side, ("OI",), oi=float(wall.oi),
            change_oi=float(wall.change_oi), rank=wall.rank,
            tested=bool(touches), held=held, strong=strong))
    return sorted(result, key=lambda level: (level.price, level.label))