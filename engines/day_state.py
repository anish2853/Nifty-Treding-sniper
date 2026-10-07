"""Day-State Engine (Trend-Day v2, module A) - replaces rigid clock windows.

Computed from the store's closed 5-min candles on every cycle:
  VWAP        - volume-weighted when candle volume exists; otherwise the
                cumulative typical-price average, tagged VWAP-proxy.
  EMA-20      - on 5-min closes.
  Liquidity break - the FIRST 5-min close beyond PDH/PDL (one per side per day).
  Structure   - ratcheting higher lows (LONG side) / lower highs (SHORT side);
                a 5-min close beyond the structure level breaks it and the state
                re-evaluates.
  Day state   - TREND-UP: PDH broken + closes holding above it + spot > VWAP.
                TREND-DOWN: mirror. RANGE: everything else (whipsawing VWAP).

Entry permissions (enforced in main): TREND - entries 09:30-14:30 while structure
holds; RANGE - sweep-fade at extremes only, no breakout entries; the clock
governs only the FIRST attempt at PDH/PDL - after a liquidity break STRUCTURE
governs. No new entries after 14:30. Square-off 15:10 unchanged.
"""
import logging
from dataclasses import dataclass, field

import settings

log = logging.getLogger(__name__)

TREND_UP = "TREND-UP"
TREND_DOWN = "TREND-DOWN"
RANGE = "RANGE"


@dataclass
class DayState:
    state: str = RANGE
    vwap: float | None = None
    vwap_proxy: bool = False
    ema20: float | None = None
    broken_level: float | None = None   # PDH/PDL whose break opened the trend
    broken_side: str | None = None      # 'UP' | 'DOWN'
    structure_intact: bool = True
    last_higher_low: float | None = None   # LONG structure trail anchor
    last_lower_high: float | None = None   # SHORT structure trail anchor
    notes: list = field(default_factory=list)

    def as_ctx(self) -> dict:
        return {"state": self.state, "structure_intact": self.structure_intact,
                "vwap": self.vwap, "broken_level": self.broken_level,
                "broken_side": self.broken_side}

    def summary(self) -> str:
        vwap = f"{self.vwap:.2f}" if self.vwap is not None else "MISSING"
        proxy = " (proxy)" if self.vwap_proxy else ""
        ema = f"{self.ema20:.2f}" if self.ema20 is not None else "MISSING"
        broken = f"{self.broken_level:.2f}" if self.broken_level is not None else "-"
        return (f"{self.state} | VWAP {vwap}{proxy} | EMA20 {ema} | "
                f"liq-break {broken} | structure "
                f"{'intact' if self.structure_intact else 'BROKEN'}")


class DayStateEngine:
    def __init__(self):
        self.state = DayState()

    def update(self, candles, pdh, pdl, spot) -> DayState:
        state = self.state
        if candles:
            self._update_vwap_ema(candles)
        # liquidity break: the FIRST 5-min close beyond PDH/PDL
        if candles and state.broken_level is None:
            for c in candles:
                if pdh is not None and c.close > pdh:
                    state.broken_level, state.broken_side = float(pdh), "UP"
                    state.notes.append(f"liquidity break: close {c.close:.2f} "
                                       f"above PDH {pdh:.2f}")
                    log.info("liquidity break UP at %s (close %.2f > PDH %.2f)",
                             c.start.strftime("%H:%M"), c.close, pdh)
                    break
                if pdl is not None and c.close < pdl:
                    state.broken_level, state.broken_side = float(pdl), "DOWN"
                    state.notes.append(f"liquidity break: close {c.close:.2f} "
                                       f"below PDL {pdl:.2f}")
                    log.info("liquidity break DOWN at %s (close %.2f < PDL %.2f)",
                             c.start.strftime("%H:%M"), c.close, pdl)
                    break
        # structure: ratcheting higher lows / lower highs
        if len(candles) >= 2:
            prev, last = candles[-2], candles[-1]
            if last.low > prev.low and (state.last_higher_low is None
                                        or last.low > state.last_higher_low):
                state.last_higher_low = float(last.low)
            if last.high < prev.high and (state.last_lower_high is None
                                          or last.high < state.last_lower_high):
                state.last_lower_high = float(last.high)
        # structure-breaking close -> re-evaluate
        if candles:
            last_close = candles[-1].close
            if state.broken_side == "UP" and state.last_higher_low is not None \
                    and last_close < state.last_higher_low:
                if state.structure_intact:
                    state.structure_intact = False
                    state.notes.append(f"structure-breaking close {last_close:.2f} "
                                       f"< last higher low "
                                       f"{state.last_higher_low:.2f} - re-evaluating")
                    log.info("structure break (UP): close %.2f < higher low %.2f",
                             last_close, state.last_higher_low)
            elif state.broken_side == "DOWN" and state.last_lower_high is not None \
                    and last_close > state.last_lower_high:
                if state.structure_intact:
                    state.structure_intact = False
                    state.notes.append(f"structure-breaking close {last_close:.2f} "
                                       f"> last lower high "
                                       f"{state.last_lower_high:.2f} - re-evaluating")
                    log.info("structure break (DOWN): close %.2f > lower high %.2f",
                             last_close, state.last_lower_high)
            elif not state.structure_intact:
                # closes recovered beyond the structure level -> intact again
                state.structure_intact = True
                state.notes.append("structure recovered - state re-evaluated intact")
        state.state = self._classify(candles, spot)
        return state

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

    def _classify(self, candles, spot) -> str:
        state = self.state
        if not candles or state.broken_level is None or state.vwap is None:
            return RANGE
        last_close = candles[-1].close
        if state.broken_side == "UP":
            holding = last_close >= state.broken_level
            above_vwap = spot is not None and spot > state.vwap
            if holding and above_vwap and state.structure_intact:
                return TREND_UP
        elif state.broken_side == "DOWN":
            holding = last_close <= state.broken_level
            below_vwap = spot is not None and spot < state.vwap
            if holding and below_vwap and state.structure_intact:
                return TREND_DOWN
        return RANGE
