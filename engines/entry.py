"""Module 3 - entry engine (Phase B). Fires ONLY on 5-min candle CLOSE, inside the
09:30-11:00 window. The confluence score is FROZEN (settings W_*); nothing here
tunes anything. LONG and SHORT are mirror evaluations on the same candle.

OI rule (spec): over the last OI_CONFIRM_SNAPSHOTS snapshots, FALLING change-in-OI
at strikes just beyond spot = +20 (writers covering); RISING = fresh wall = the
signal is REJECTED outright regardless of score.
"""
import logging
from dataclasses import dataclass, field

import settings
from engines.candles import Candle
from engines.oi_engine import classify_oi_window, recent_oi_net, spot_adjacent

log = logging.getLogger(__name__)


@dataclass
class Evaluation:
    direction: str
    candle: Candle
    level_name: str | None = None
    level: float | None = None
    broke: bool = False
    confirmed: bool = False
    oi_state: str = "UNKNOWN"          # PASS / REJECT / UNKNOWN
    oi_detail: str = ""
    volume_state: str = "MISSING"      # PASS / FAIL / MISSING
    volume_detail: str = ""
    regime_ok: bool = False
    regime_note: str = ""
    window_ok: bool = False
    score: int = 0
    rejected: bool = False
    reject_reason: str = ""
    fired: bool = False
    near_miss: bool = False
    blocked_note: str = ""
    caps_note: str = ""
    reasons: list = field(default_factory=list)  # [(name, ok, points, detail)]


def volume_state_for(candle: Candle, engine):
    """Shared volume component: breakout candle volume vs the 10-candle average.
    MISSING (0 pts) when index volume is unavailable - the usual case."""
    volumes = engine.recent_volumes(candle, settings.VOLUME_AVG_WINDOW) if engine else []
    if candle.volume is None or len(volumes) < 5:
        return "MISSING", f"volume unavailable ({len(volumes)} prior pts)"
    avg = sum(volumes) / len(volumes)
    if candle.volume > avg:
        return "PASS", f"vol {candle.volume:.0f} > avg {avg:.0f}"
    return "FAIL", f"vol {candle.volume:.0f} <= avg {avg:.0f}"


class EntryEngine:
    def __init__(self, store):
        self._store = store

    # -- components -----------------------------------------------------------
    def _level_break(self, candle: Candle, direction: str, orb, pdh, pdl):
        """ORB first (primary intraday level), then the previous-day level."""
        if direction == "LONG":
            if orb and candle.close > orb["high"]:
                return "ORB high", orb["high"]
            if pdh is not None and candle.close > pdh:
                return "PDH", pdh
            return None, None
        if orb and candle.close < orb["low"]:
            return "ORB low", orb["low"]
        if pdl is not None and candle.close < pdl:
            return "PDL", pdl
        return None, None

    def _oi_state(self, ctx, direction: str, spot: float):
        snapshot = ctx.get("snapshot")
        if not snapshot or snapshot.underlying is None or not snapshot.rows:
            return "UNKNOWN", "chain unavailable"
        expiry = snapshot.nearest_expiry
        near = spot_adjacent(snapshot.rows, spot,
                             count=settings.OI_SPOT_ADJACENT_COUNT,
                             side="above" if direction == "LONG" else "below",
                             expiry=expiry)
        net = recent_oi_net(self._store.conn, expiry, [r.strike for r in near],
                            "ce" if direction == "LONG" else "pe")
        state = classify_oi_window(net)
        detail = ", ".join(f"{s}:{v:+.0f}" for s, v in sorted(net.items())) or "no data"
        return state, detail

    def _volume_state(self, candle: Candle, ctx):
        return volume_state_for(candle, ctx.get("candle_engine"))

    # -- main evaluation --------------------------------------------------------
    def evaluate(self, candle: Candle, prev: Candle | None, ctx: dict) -> list:
        """One closed candle -> [Evaluation(LONG), Evaluation(SHORT)]. Empty list
        when the candle closes outside the 09:30-11:00 window."""
        if not (settings.ENTRY_EVAL_START <= candle.end.time() <= settings.ENTRY_WINDOW_END):
            return []
        orb, pdh, pdl = ctx.get("orb"), ctx.get("pdh"), ctx.get("pdl")
        spot = ctx.get("spot") or candle.close
        out = []
        for direction in ("LONG", "SHORT"):
            ev = Evaluation(direction=direction, candle=candle, window_ok=True)
            ev.reasons.append(("time window 09:30-11:00", True, settings.W_TIME_WINDOW,
                               f"close {candle.end.strftime('%H:%M')} IST"))

            level_name, level = self._level_break(candle, direction, orb, pdh, pdl)
            ev.level_name, ev.level, ev.broke = level_name, level, level_name is not None
            ev.reasons.append((
                f"level break ({level_name} {level:.2f})" if level_name else "level break",
                ev.broke, settings.W_LEVEL_BREAK if ev.broke else 0,
                f"5-min close {candle.close:.2f}"))

            confirmed = False
            if ev.broke and prev is not None:
                confirmed = prev.close > level if direction == "LONG" \
                    else prev.close < level
            ev.confirmed = confirmed
            ev.reasons.append(("confirmation: prior candle also beyond level", confirmed,
                               settings.W_CONFIRMATION if confirmed else 0,
                               f"prior close {prev.close:.2f}" if prev else "no prior candle"))

            if ev.broke:
                ev.oi_state, ev.oi_detail = self._oi_state(ctx, direction, spot)
                oi_ok = ev.oi_state == "PASS"
                ev.reasons.append(("OI: writers covering beyond spot", oi_ok,
                                   settings.W_OI_CONFIRMATION if oi_ok else 0,
                                   f"{ev.oi_state}: {ev.oi_detail}"))
            else:
                ev.reasons.append(("OI: writers covering beyond spot", False, 0,
                                   "no level break"))

            if ev.broke:
                vol_state, vol_detail = self._volume_state(candle, ctx)
            else:
                vol_state, vol_detail = "FAIL", "no level break"
            ev.volume_state, ev.volume_detail = vol_state, vol_detail
            ev.reasons.append(("breakout volume > 10-candle avg", vol_state == "PASS",
                               settings.W_VOLUME if vol_state == "PASS" else 0,
                               vol_detail))

            regime_ok, regime_note = ctx["regime_allows"](direction, level_name)
            ev.regime_ok, ev.regime_note = regime_ok, regime_note
            vix_stable = not ctx.get("vix_spike", False)
            component_ok = regime_ok and vix_stable and not ctx.get("event_day", False)
            ev.reasons.append(("regime allows direction + VIX stable + not event day",
                               component_ok,
                               settings.W_REGIME_VIX if component_ok else 0,
                               regime_note or ("" if vix_stable else "VIX spike")))

            ev.score = sum(points for _, ok, points, _ in ev.reasons if ok)

            # Trend-Day v2 BONUS points (existing components unchanged, +20 max):
            # +10 level confluence: ORB high within 0.15% of PDH / ORB low of PDL
            confluence = False
            orb, pdh, pdl = ctx.get("orb"), ctx.get("pdh"), ctx.get("pdl")
            if orb:
                if pdh is not None and orb["high"] is not None and \
                        abs(orb["high"] - pdh) <= abs(pdh) * settings.STRONG_LEVEL_BAND_PCT:
                    confluence = True
                if pdl is not None and orb["low"] is not None and \
                        abs(orb["low"] - pdl) <= abs(pdl) * settings.STRONG_LEVEL_BAND_PCT:
                    confluence = True
            ev.reasons.append(("BONUS level confluence (ORB at PDH/PDL)", confluence,
                               settings.W_TIME_WINDOW if confluence else 0,
                               f"ORB {orb['high']:.2f}/{orb['low']:.2f}" if orb else "no ORB"))
            # +10 VWAP alignment: long only above VWAP, short only below
            vwap = ctx.get("vwap")
            aligned = vwap is not None and spot is not None and \
                ((direction == "LONG" and spot > vwap)
                 or (direction == "SHORT" and spot < vwap))
            ev.reasons.append(("BONUS VWAP alignment", aligned,
                               settings.W_TIME_WINDOW if aligned else 0,
                               f"spot {spot:.2f} vs VWAP {vwap:.2f}"
                               if vwap is not None else "VWAP unavailable"))
            ev.score = sum(points for _, ok, points, _ in ev.reasons if ok)

            if ev.broke and ev.oi_state == "REJECT":
                ev.rejected = True
                ev.reject_reason = f"fresh writing wall beyond spot ({ev.oi_detail})"

            caps_ok, caps_note = ctx["caps_ok"](direction)
            ev.caps_note = caps_note
            ltp = ctx["option_ltp"](direction, spot)
            ev.fired = (ev.broke and not ev.rejected
                        and ev.score >= settings.ENTRY_SCORE_FIRE
                        and regime_ok and caps_ok and ltp is not None)
            if ev.broke and not ev.rejected and not ev.fired \
                    and ev.score >= settings.ENTRY_SCORE_FIRE:
                ev.blocked_note = caps_note or ("option LTP unavailable" if ltp is None
                                                else "regime blocked")
            ev.near_miss = (ev.broke and not ev.rejected and not ev.fired
                            and settings.ENTRY_SCORE_JOURNAL_MIN <= ev.score
                            < settings.ENTRY_SCORE_FIRE)
            out.append(ev)
        return out
