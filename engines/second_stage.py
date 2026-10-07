"""SECOND-STAGE entry engine (broken wall = fuel).

Watches STRONG liquidity walls: when price breaks INTO a wall and TWO
consecutive 5-min closes HOLD beyond it, the wall FLIPS - trapped writers must
cover = fuel. Entry: price retests the broken wall from the far side and prints
a rejection close; FUEL check: change-in-OI at the wall strikes turning NEGATIVE
(writers covering). Family: SECOND-STAGE. Only fires after a wall is already
broken - never on the initial break. ("1-min rejection" is interpreted as the
5-min candle's rejection of the level - the store has no 1-min candles.)
"""
import logging
from dataclasses import dataclass, field

import settings
from engines.candles import Candle
from engines.oi_engine import recent_oi_net

log = logging.getLogger(__name__)


@dataclass
class SecondStageEvaluation:
    direction: str
    candle: Candle
    level_strike: float
    level_label: str
    sl_spot: float
    score: int = 100
    fired: bool = True
    near_miss: bool = False
    blocked_note: str = ""
    rejected: bool = False
    reject_reason: str = ""
    reasons: list = field(default_factory=list)


class SecondStageEngine:
    def __init__(self, store):
        self._store = store
        self.watches: dict = {}   # strike -> {side, closes, flipped}

    def update(self, candles, levels_map) -> None:
        """Track closes-beyond for each strong wall (call once per cycle)."""
        if levels_map is None:
            return
        for lv in levels_map.levels:
            if not (lv.strong or lv.change_oi > 0):
                continue
            w = self.watches.setdefault(
                lv.strike, {"side": None, "closes": 0, "flipped": False})
            if w["flipped"] or not candles:
                continue
            last = candles[-1]
            if w["side"] is None:
                if last.close > lv.strike:
                    w["side"] = "UP"
                elif last.close < lv.strike:
                    w["side"] = "DOWN"
                else:
                    continue
            beyond = last.close > lv.strike if w["side"] == "UP" \
                else last.close < lv.strike
            if beyond:
                w["closes"] += 1
                if w["closes"] >= 2:
                    w["flipped"] = True
                    log.info("wall FLIPPED: %s held beyond %s for 2 closes - "
                             "trapped writers must cover", lv.label, lv.strike)
            else:
                w["closes"] = 0       # back inside: 2-consecutive count resets

    def has_flipped(self) -> bool:
        return any(w["flipped"] for w in self.watches.values())

    def _fuel(self, strike: float, direction: str, nearest_expiry) -> bool:
        """Fuel = change-in-OI at the wall turning NEGATIVE (writers covering)."""
        side = "ce" if direction == "LONG" else "pe"
        net = recent_oi_net(self._store.conn, nearest_expiry, [int(strike)], side)
        return bool(net) and all(v < 0 for v in net.values())

    def evaluate(self, candle: Candle, ctx: dict) -> list:
        """Retest of a flipped wall from the far side + rejection + fuel."""
        snapshot = ctx.get("snapshot")
        out = []
        for strike, w in self.watches.items():
            if not w["flipped"]:
                continue
            if w["side"] == "UP":
                direction = "LONG"
                retest = candle.low <= strike and candle.close > strike
                rejection = candle.close > candle.open
            else:
                direction = "SHORT"
                retest = candle.high >= strike and candle.close < strike
                rejection = candle.close < candle.open
            if not retest or not rejection:
                continue
            fuel = self._fuel(strike, direction,
                              snapshot.nearest_expiry if snapshot else None)
            if not fuel:
                log.info("SECOND-STAGE retest at %s rejected: no covering fuel",
                         strike)
                continue
            sl = strike - settings.SWEEP_SL_BUFFER_PTS if direction == "LONG" \
                else strike + settings.SWEEP_SL_BUFFER_PTS
            ev = SecondStageEvaluation(
                direction=direction, candle=candle, level_strike=strike,
                level_label=f"flipped wall {strike:,.0f}", sl_spot=sl,
                reasons=[
                    ("wall flipped: 2 consecutive closes held beyond", True, 0, ""),
                    ("retest from the far side + rejection close", True, 0, ""),
                    ("fuel: change-in-OI at the wall turning negative", True, 0, ""),
                ])
            out.append(ev)
        return out
