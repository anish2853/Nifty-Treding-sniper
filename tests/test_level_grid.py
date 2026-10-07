from datetime import datetime, timedelta
from types import SimpleNamespace

import settings
from engines.candles import Candle
from engines.level_grid import build_level_grid
from engines.liquidity import OILevel, LiquidityMap


def bar(index, high, low, close):
    return Candle(datetime(2026, 10, 7, 9, 15, tzinfo=settings.IST)
                  + timedelta(minutes=5 * index), close, high, low, close)


def test_grid_merges_all_price_sources_and_confirmed_swings():
    candles = [bar(0, 100, 95, 98), bar(1, 103, 97, 101),
               bar(2, 110, 90, 105), bar(3, 104, 96, 99),
               bar(4, 102, 94, 98)]
    grid = build_level_grid(
        spot=100, pdh=108, pdl=92, pdc=98,
        orb={"high": 106, "low": 94}, candles=candles,
        vwap=101, liquidity=None)
    labels = {level.label for level in grid}

    assert {"PDH", "PDL", "PDC", "ORB-H", "ORB-L", "SWING-H", "SWING-L",
            "VWAP"} <= labels
    assert [level.price for level in grid] == sorted(level.price for level in grid)


def test_strong_oi_wall_requires_structure_confluence_and_a_held_test():
    wall = OILevel("R", 1, 22700, 465000, 8000, 50, 0.2, False)
    liquidity = LiquidityMap(None, 22650, [wall])
    candles = [bar(0, 22705, 22680, 22690)]

    grid = build_level_grid(spot=22650, pdh=22710, candles=candles,
                            liquidity=liquidity)
    oi = next(level for level in grid if level.level_id.startswith("OI-"))
    assert oi.tested and oi.held and oi.strong
    assert oi.bias == "HOLD"

    untested = build_level_grid(spot=22650, pdh=22710, liquidity=liquidity)
    assert not next(level for level in untested if level.level_id.startswith("OI-")).strong