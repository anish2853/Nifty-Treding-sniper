"""Trend Pullback setup (Trend-Day v2, module C) - the follow-up entry on trend
days. TREND-UP: price pulls back to VWAP or retests the broken PDH and prints a
5-min bullish close off the level -> CE entry. Mirror for PE.

Frozen scoring (max 100, fire >= 80 - same discipline as the other families):
  +20 level confluence (pullback touches VWAP or the broken level)
  +20 rejection candle (5-min bullish/bearish close off the level)
  +20 OI (no fresh writing beyond spot on the trade side)
  +15 VWAP/structure hold
  +15 trend intact
  +10 volume spike (MISSING ok, 0 pts)
SL spot = pullback level -/+ SWEEP_SL_BUFFER_PTS (the frozen 5-pt buffer).
"""
import logging
from dataclasses import dataclass, field

import settings
from engines.candles import Candle
from engines.entry import volume_state_for
from engines.oi_engine import classify_oi_window, recent_oi_net, spot_adjacent

log = logging.getLogger(__name__)


@dataclass
class PullbackEvaluation:
    direction: str
    candle: Candle
    level: float
    level_label: str
    sl_spot: float
    touched: bool = False
    rejection: bool = False
    rejection_detail: str = ""
    oi_state: str = "UNKNOWN"
    oi_detail: str = ""
    volume_state: str = "MISSING"
    volume_detail: str = ""
    vwap_hold: bool = False
    trend_intact: bool = False
    window_ok: bool = False
    score: int = 0
    rejected: bool = False
    reject_reason: str = ""
    fired: bool = False
    near_miss: bool = False
    blocked_note: str = ""
    caps_note: str = ""
    reasons: list = field(default_factory=list)


class PullbackEngine:
    def __init__(self, store):
        self._store = store

    @staticmethod
    def _levels(ctx) -> list:
        """Pullback candidates: the broken liquidity level first, then VWAP."""
        day_state = ctx.get("day_state") or {}
        levels = []
        if day_state.get("broken_level") is not None:
            name = "broken PDH" if day_state.get("broken_side") == "UP" \
                else "broken PDL"
            levels.append((day_state["broken_level"], name))
        if ctx.get("vwap") is not None:
            levels.append((ctx["vwap"], "VWAP" + (" (proxy)" if ctx.get("vwap_proxy") else "")))
        return levels

    @staticmethod
    def window_ok(candle: Candle) -> bool:
        """TREND entries run 09:30-14:30 while structure holds (structure-
        governed, not clock-governed)."""
        return settings.ENTRY_EVAL_START <= candle.end.time() <= settings.LAST_ENTRY_TIME

    def _oi_state(self, ctx, direction: str, spot: float):
        snapshot = ctx.get("snapshot")
        if not snapshot or not snapshot.rows:
            return "UNKNOWN", "chain unavailable"
        near = spot_adjacent(snapshot.rows, spot,
                             count=settings.OI_SPOT_ADJACENT_COUNT,
                             side="above" if direction == "LONG" else "below",
                             expiry=snapshot.nearest_expiry)
        net = recent_oi_net(self._store.conn, snapshot.nearest_expiry,
                            [r.strike for r in near],
                            "ce" if direction == "LONG" else "pe")
        state = classify_oi_window(net)
        detail = ", ".join(f"{s}:{v:+.0f}" for s, v in sorted(net.items())) or "no data"
        return state, detail

    def evaluate(self, candle: Candle, prev: Candle | None, ctx: dict) -> list:
        day_state = ctx.get("day_state") or {}
        if not self.window_ok(candle) or not day_state:
            return []
        state = day_state.get("state")
        if state not in ("TREND-UP", "TREND-DOWN"):
            return []                       # pullbacks exist only on trend days
        spot = ctx.get("spot") or candle.close
        vwap = ctx.get("vwap")
        out = []
        for level, label in self._levels(ctx):
            if state == "TREND-UP":
                direction = "LONG"
                touched = candle.low <= level <= candle.high or \
                    (candle.low <= level < candle.close)
                rejection = candle.close > level and candle.close > candle.open
                depth = candle.close - level
                vwap_hold = vwap is not None and spot > vwap
            else:
                direction = "SHORT"
                touched = candle.high >= level >= candle.low or \
                    (candle.high >= level > candle.close)
                rejection = candle.close < level and candle.close < candle.open
                depth = level - candle.close
                vwap_hold = vwap is not None and spot < vwap
            if not touched:
                continue
            rng = candle.high - candle.low
            rejection_detail = (f"close {candle.close:.2f} off {label} {level:.2f} "
                                f"({(depth / rng):.0%} of range)" if rng > 0 else "")
            ev = PullbackEvaluation(direction=direction, candle=candle,
                                    level=level, level_label=label,
                                    sl_spot=level - settings.SWEEP_SL_BUFFER_PTS
                                    if direction == "LONG"
                                    else level + settings.SWEEP_SL_BUFFER_PTS,
                                    touched=True, rejection=rejection,
                                    rejection_detail=rejection_detail,
                                    window_ok=True)
            ev.reasons.append((f"pullback to {label} {level:.2f}", touched,
                               settings.W_LEVEL_BREAK if touched else 0,
                               f"low {candle.low:.2f} high {candle.high:.2f}"))
            ev.reasons.append(("rejection candle off the level", rejection,
                               settings.W_CONFIRMATION if rejection else 0,
                               rejection_detail))
            ev.oi_state, ev.oi_detail = self._oi_state(ctx, direction, spot)
            oi_ok = ev.oi_state == "PASS"
            ev.reasons.append(("OI: no fresh writing beyond spot", oi_ok,
                               settings.W_OI_CONFIRMATION if oi_ok else 0,
                               f"{ev.oi_state}: {ev.oi_detail}"))
            structure_intact = bool(day_state.get("structure_intact", False))
            ev.vwap_hold = vwap_hold
            ev.trend_intact = structure_intact
            vwap_ok = vwap_hold and structure_intact
            ev.reasons.append(("VWAP/structure hold", vwap_ok,
                               settings.W_REGIME_VIX if vwap_ok else 0,
                               f"vwap hold {vwap_hold}, structure {structure_intact}"))
            trend_ok = state in ("TREND-UP", "TREND-DOWN") and structure_intact
            ev.reasons.append(("trend intact", trend_ok,
                               settings.W_REGIME_VIX if trend_ok else 0,
                               f"day state {state}"))
            ev.volume_state, ev.volume_detail = volume_state_for(
                candle, ctx.get("candle_engine"))
            ev.reasons.append(("volume on the pullback candle",
                               ev.volume_state == "PASS",
                               settings.W_VOLUME if ev.volume_state == "PASS" else 0,
                               ev.volume_detail))
            ev.score = sum(points for _, ok, points, _ in ev.reasons if ok)

            caps_ok, caps_note = ctx["caps_ok"](direction)
            ev.caps_note = caps_note
            ltp = ctx["option_ltp"](direction, candle.close)
            ev.fired = (ev.rejection and ev.score >= settings.ENTRY_SCORE_FIRE
                        and caps_ok and ltp is not None and trend_ok)
            if not ev.fired and ev.score >= settings.ENTRY_SCORE_FIRE:
                ev.blocked_note = caps_note or ("option LTP unavailable"
                                                if ltp is None else "trend not intact")
            ev.near_miss = (not ev.fired
                            and settings.ENTRY_SCORE_JOURNAL_MIN <= ev.score
                            < settings.ENTRY_SCORE_FIRE)
            out.append(ev)
        return out
