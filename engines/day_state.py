"""Day-State Engine v2 (symmetric rebuild - BUG-1 fix).

A full symmetric state machine over the FIRST-HOUR RANGE (09:15-10:15 high/low),
re-evaluated on EVERY 5-min close, every transition logged with timestamp and
boundary:
  RANGE -> TREND-UP     2 consecutive closes above the first-hour range high
  RANGE -> TREND-DOWN   2 consecutive closes below the first-hour range low
  TREND-* -> RANGE      2 consecutive closes back inside the range
  TREND-UP <-> TREND-DOWN  2 consecutive closes beyond the OPPOSITE boundary

Also computes: VWAP (proxy when index volume missing), EMA-20, ORB-mid bias
(below mid after the first hour -> PE-preferred; above -> CE-preferred), and
ratcheting structure (higher lows / lower highs) for the OI-whispers trail.
PDH/PDL remain as liquidity-map context, never the state gate.

Entry permissions (enforced in main): TREND - entries 09:30-14:30 while structure
holds; RANGE - sweep-fade at extremes only, no breakout entries; the clock
governs only the FIRST attempt - after a break STRUCTURE governs. No new entries
after 14:30. Square-off 15:10 unchanged.
"""
import logging
from dataclasses import dataclass, field
from datetime import time as dt_time

import settings

log = logging.getLogger(__name__)

TREND_UP = "TREND-UP"
TREND_DOWN = "TREND-DOWN"
RANGE = "RANGE"


@dataclass
class DayState:
    state: str = RANGE
    first_hour_high: float | None = None
    first_hour_low: float | None = None
    vwap: float | None = None
    vwap_proxy: bool = False
    ema20: float | None = None
    orb_mid: float | None = None        # session bias pivot (ORB mid)
    bias: str | None = None             # CE-preferred / PE-preferred (after hour 1)
    broken_level: float | None = None   # boundary whose break opened the trend
    broken_side: str | None = None      # 'UP' | 'DOWN'
    structure_intact: bool = True
    last_higher_low: float | None = None
    last_lower_high: float | None = None
    eq: float | None = None             # Equilibrium midline = (PDH + PDL) / 2
    eq_bias: str | None = None          # "long-preferred" | "short-preferred"
    notes: list = field(default_factory=list)
    transitions: list = field(default_factory=list)   # [(ts, "A->B", boundary)]

    def as_ctx(self) -> dict:
        return {"state": self.state, "structure_intact": self.structure_intact,
                "vwap": self.vwap, "broken_level": self.broken_level,
                "broken_side": self.broken_side,
                "eq": self.eq, "eq_bias": self.eq_bias}

    def summary(self) -> str:
        vwap = f"{self.vwap:.2f}" if self.vwap is not None else "MISSING"
        ema = f"{self.ema20:.2f}" if self.ema20 is not None else "MISSING"
        rng = (f"{self.first_hour_low:.2f}-{self.first_hour_high:.2f}"
               if self.first_hour_low is not None else "unlocked")
        bias = self.bias or "-"
        eq_txt = f" | EQ {self.eq:.2f} ({self.eq_bias})" if self.eq is not None else ""
        return (f"{self.state} | 1h-range {rng} | VWAP {vwap}"
                f"{' (proxy)' if self.vwap_proxy else ''} | EMA20 {ema} | "
                f"bias {bias}{eq_txt} | structure "
                f"{'intact' if self.structure_intact else 'BROKEN'}")


class DayStateEngine:
    """Full symmetric state machine (BUG-1 fix): every 5-min close is evaluated;
    2 consecutive closes beyond/inside a boundary flip the state; every
    transition is recorded with timestamp + boundary."""

    def __init__(self):
        self.state = DayState()

    def update(self, candles, spot=None, orb_mid=None, pdh=None, pdl=None) -> DayState:
        state = self.state
        if pdh is not None and pdl is not None:
            state.eq = (float(pdh) + float(pdl)) / 2.0
            if spot is not None:
                state.eq_bias = "long-preferred" if spot >= state.eq else "short-preferred"
        if orb_mid is not None:
            state.orb_mid = float(orb_mid)
        if candles:
            self._update_vwap_ema(candles)
        self._lock_first_hour(candles)
        self._update_structure(candles)

        # ---- symmetric transitions on consecutive closes (BUG-1 fix) ----
        if candles and state.first_hour_high is not None:
            high, low = state.first_hour_high, state.first_hour_low
            last_ts = candles[-1].start
            above = self._consecutive(candles, lambda c: c.close > high)
            below = self._consecutive(candles, lambda c: c.close < low)
            inside = self._consecutive(candles, lambda c: low <= c.close <= high)

            def flip(new_state, boundary):
                old = state.state
                if old == new_state:
                    return
                state.state = new_state
                state.broken_level = boundary
                state.broken_side = "UP" if new_state == TREND_UP else \
                    "DOWN" if new_state == TREND_DOWN else state.broken_side
                state.structure_intact = True
                self._record(f"{old} -> {new_state}", boundary, last_ts)

            if state.state == RANGE:
                if above >= 2:
                    flip(TREND_UP, high)           # <- existed before
                elif below >= 2:
                    flip(TREND_DOWN, low)          # <- BUG-1: this was missing
            elif state.state == TREND_UP:
                if below >= 2:
                    flip(TREND_DOWN, low)          # opposite boundary
                elif inside >= 2:
                    flip(RANGE, high)              # back inside
            elif state.state == TREND_DOWN:
                if above >= 2:
                    flip(TREND_UP, high)           # opposite boundary
                elif inside >= 2:
                    flip(RANGE, low)               # back inside

        # ORB-mid bias after the first hour (feeds permissions + review)
        if candles and state.orb_mid is not None:
            now_t = candles[-1].start.time()
            open_mins = settings.MARKET_OPEN.hour * 60 + settings.MARKET_OPEN.minute
            first_hour_end = dt_time((open_mins + 60) // 60, (open_mins + 60) % 60)
            if now_t >= first_hour_end and spot is not None:
                state.bias = "CE-preferred" if spot > state.orb_mid \
                    else "PE-preferred" if spot < state.orb_mid else state.bias
        return state

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _consecutive(candles, predicate) -> int:
        count = 0
        for candle in reversed(candles):
            if predicate(candle):
                count += 1
            else:
                break
        return count

    def _record(self, transition: str, boundary: float | None, ts) -> None:
        self.state.transitions.append(
            (ts.isoformat(timespec="seconds"), transition,
             round(boundary, 2) if boundary is not None else None))
        log.info("day state %s @ %s (boundary %s)", transition,
                 ts.strftime("%H:%M"), boundary)

    def _lock_first_hour(self, candles) -> None:
        state = self.state
        if state.first_hour_high is not None or not candles:
            return
        open_mins = settings.MARKET_OPEN.hour * 60 + settings.MARKET_OPEN.minute
        end_h, end_m = divmod(open_mins + 60, 60)
        first_hour = [c for c in candles
                      if dt_time(open_mins // 60, open_mins % 60)
                      <= c.start.time() < dt_time(end_h, end_m)]
        hour_done = candles[-1].start.time() >= dt_time(end_h, end_m)
        if len(first_hour) >= 3 or (hour_done and first_hour):
            state.first_hour_high = max(c.high for c in first_hour)
            state.first_hour_low = min(c.low for c in first_hour)
            log.info("first-hour range locked: %.2f-%.2f (%d candles)",
                     state.first_hour_high, state.first_hour_low, len(first_hour))

    def _update_structure(self, candles) -> None:
        state = self.state
        if len(candles) >= 2:
            prev, last = candles[-2], candles[-1]
            if last.low > prev.low and (state.last_higher_low is None
                                        or last.low > state.last_higher_low):
                state.last_higher_low = float(last.low)
            if last.high < prev.high and (state.last_lower_high is None
                                          or last.high < state.last_lower_high):
                state.last_lower_high = float(last.high)

    def _update_vwap_ema(self, candles) -> None:
        any_volume = any(c.volume for c in candles)
        pv = fv = tp_sum = 0.0
        for c in candles:
            typical = (c.high + c.low + c.close) / 3
            tp_sum += typical
            if any_volume:
                volume = c.volume or 0.0
                pv += typical * volume
                fv += volume
        if any_volume and fv > 0:
            self.state.vwap, self.state.vwap_proxy = pv / fv, False
        else:
            self.state.vwap, self.state.vwap_proxy = tp_sum / len(candles), True
        closes = [c.close for c in candles]
        ema = closes[0]
        k = 2 / (settings.EMA_SPAN + 1)
        for close in closes[1:]:
            ema = close * k + ema * (1 - k)
        self.state.ema20 = ema
