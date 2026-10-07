"""Sweep-fade engine (Phase B+ module 2) - second setup family, same score
discipline as the breakout engine.

Setup: price TRADES through a STRONG liquidity level (an OI wall tagged STRONG, or
the PDH/PDL pools) but the 5-min candle CLOSES back inside = failed breakout /
stop hunt. Frozen score (settings W_*): +20 sweep, +20 rejection (close back
inside by >25% of the candle range), +20 OI accelerating into the sweep (writers
defending the level), +15 volume spike, +15 regime allows counter-direction, +10
time window 09:30-11:00 or 13:30-14:45. FIRE >= 80: BUY PE after a failed UP-side
sweep, BUY CE after a failed DOWN-side sweep. SL spot = sweep extreme +/- 5 pts.
Same exit engine and the SAME daily 2-trade cap as breakout signals.

NOTE (documented interpretation): mean-reversion regime does NOT block sweep-fades
- Module 2 suppresses *breakout* entries there, and a fade IS the mean-reversion
trade. LOW-CONFIDENCE, VIX spike and event days still block everything.
"""
import logging
from dataclasses import dataclass, field

import settings
from engines.candles import Candle
from engines.entry import volume_state_for
from engines.oi_engine import recent_oi_net

log = logging.getLogger(__name__)


@dataclass
class SweepEvaluation:
    direction: str                       # trade direction (SHORT after UP sweep)
    candle: Candle
    level_strike: float
    level_label: str
    sweep_side: str                      # 'UP' | 'DOWN'
    sweep_extreme: float
    sl_spot: float
    swept: bool = False
    rejection: bool = False
    rejection_detail: str = ""
    oi_state: str = "UNKNOWN"            # PASS (accelerating) / FLAT / UNKNOWN
    oi_detail: str = ""
    volume_state: str = "MISSING"
    volume_detail: str = ""
    regime_ok: bool = False
    regime_note: str = ""
    window_ok: bool = False
    score: int = 0
    rejected: bool = False               # this family has no outright-reject rule
    reject_reason: str = ""
    fired: bool = False
    near_miss: bool = False
    blocked_note: str = ""
    caps_note: str = ""
    reasons: list = field(default_factory=list)


class SweepEngine:
    def __init__(self, store):
        self._store = store

    # -- sweepable universe -----------------------------------------------------
    @staticmethod
    def sweepable_levels(ctx) -> list:
        """[(strike, label)]: PDH/PDL pools + STRONG OI levels from the latest map.
        Deduped by strike so a pool and a co-located STRONG wall yield ONE level."""
        items = []
        seen = set()
        for name, level, _pct in (ctx.get("liquidity").pools if ctx.get("liquidity") else []):
            key = round(float(level), 2)
            if key not in seen:
                seen.add(key)
                items.append((level, f"{name} pool"))
        liquidity = ctx.get("liquidity")
        if liquidity:
            for lv in liquidity.levels:
                if lv.strong and round(float(lv.strike), 2) not in seen:
                    seen.add(round(float(lv.strike), 2))
                    items.append((lv.strike, f"{lv.label} {lv.strike:,.0f} STRONG"))
        return items

    @staticmethod
    def window_ok(candle: Candle) -> bool:
        t = candle.end.time()
        return (settings.ENTRY_EVAL_START <= t <= settings.ENTRY_WINDOW_END
                or settings.SWEEP_WINDOW_2_START <= t <= settings.SWEEP_WINDOW_2_END)

    # -- OI acceleration at the swept level ---------------------------------------
    def _oi_accel(self, ctx, level_strike: float, spot, up: bool):
        snapshot = ctx.get("snapshot")
        if snapshot is None or not snapshot.rows:
            return "UNKNOWN", "no OI data"
        expiry = snapshot.nearest_expiry
        step = settings.STRIKE_ROUND_TO
        candidates = [r.strike for r in snapshot.rows
                      if r.expiry == expiry
                      and abs(r.strike - level_strike) <= step]
        if not candidates:
            return "UNKNOWN", "no OI strike at the level"
        strike = min(candidates, key=lambda s: abs(s - level_strike))
        side = "ce" if (up or level_strike > (spot if spot is not None else level_strike)) \
            else "pe"
        net = recent_oi_net(self._store.conn, expiry, [strike], side)
        if not net:
            return "UNKNOWN", f"{strike} {side.upper()}: no snapshot history"
        value = net[strike]
        if value > 0:
            return "PASS", f"{strike} {side.upper()} change-in-OI {value:+,.0f} " \
                           f"(accelerating - writers defending)"
        return "FLAT", f"{strike} {side.upper()} change-in-OI {value:+,.0f}"

    # -- main evaluation -------------------------------------------------------------
    def evaluate(self, candle: Candle, ctx: dict) -> list:
        """One closed candle -> one evaluation per SWEPT level (both sides possible)."""
        if not self.window_ok(candle):
            return []
        spot = ctx.get("spot") or candle.close
        out = []
        for level_strike, label in self.sweepable_levels(ctx):
            up = candle.high > level_strike and candle.close < level_strike
            down = candle.low < level_strike and candle.close > level_strike
            if not (up or down):
                continue
            direction = "SHORT" if up else "LONG"
            extreme = candle.high if up else candle.low
            sl_spot = extreme + settings.SWEEP_SL_BUFFER_PTS if up \
                else extreme - settings.SWEEP_SL_BUFFER_PTS
            ev = SweepEvaluation(direction=direction, candle=candle,
                                 level_strike=level_strike, level_label=label,
                                 sweep_side="UP" if up else "DOWN",
                                 sweep_extreme=extreme, sl_spot=sl_spot,
                                 swept=True, window_ok=True)
            ev.reasons.append((f"sweep of {label} (wick {extreme:.2f} beyond, "
                               f"close back inside)", True, settings.W_LEVEL_BREAK,
                               f"{ev.sweep_side}-side sweep"))

            rng = candle.high - candle.low
            depth = (candle.high - candle.close) if up else (candle.close - candle.low)
            frac = (depth / rng) if rng > 0 else 0.0
            ev.rejection = frac > settings.SWEEP_REJECT_FRACTION
            ev.rejection_detail = f"close back inside {frac:.0%} of the range"
            ev.reasons.append(("rejection: close back inside >25% of range",
                               ev.rejection,
                               settings.W_CONFIRMATION if ev.rejection else 0,
                               ev.rejection_detail))

            ev.oi_state, ev.oi_detail = self._oi_accel(ctx, level_strike, spot, up)
            oi_ok = ev.oi_state == "PASS"
            ev.reasons.append(("OI accelerating into the sweep", oi_ok,
                               settings.W_OI_CONFIRMATION if oi_ok else 0,
                               ev.oi_detail))

            ev.volume_state, ev.volume_detail = volume_state_for(
                candle, ctx.get("candle_engine"))
            ev.reasons.append(("volume spike on the sweep candle",
                               ev.volume_state == "PASS",
                               settings.W_VOLUME if ev.volume_state == "PASS" else 0,
                               ev.volume_detail))

            regime_ok, regime_note = ctx["regime_allows_sweep"](direction)
            ev.regime_ok, ev.regime_note = regime_ok, regime_note
            ev.reasons.append(("regime allows counter-direction", regime_ok,
                               settings.W_REGIME_VIX if regime_ok else 0, regime_note))

            ev.reasons.append(("time window 09:30-11:00 / 13:30-14:45", True,
                               settings.W_TIME_WINDOW,
                               f"close {candle.end.strftime('%H:%M')} IST"))
            ev.score = sum(points for _, ok, points, _ in ev.reasons if ok)

            caps_ok, caps_note = ctx["caps_ok"](direction)
            ev.caps_note = caps_note
            ltp = ctx["option_ltp"](direction, candle.close)
            ev.fired = (ev.score >= settings.ENTRY_SCORE_FIRE and ev.regime_ok
                        and caps_ok and ltp is not None)
            if not ev.fired and ev.score >= settings.ENTRY_SCORE_FIRE:
                ev.blocked_note = caps_note or ("option LTP unavailable"
                                                if ltp is None else "regime blocked")
            ev.near_miss = (not ev.fired
                            and settings.ENTRY_SCORE_JOURNAL_MIN <= ev.score
                            < settings.ENTRY_SCORE_FIRE)
            out.append(ev)
        return out
