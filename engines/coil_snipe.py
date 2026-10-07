"""Consolidation -> liquidity purge -> confirmed-break coil sniper logic."""
from dataclasses import dataclass
from datetime import time

import settings
from engines.mtf_scalp import MTFZone


COILING = "COILING"
PENDING_BREAK = "PENDING_BREAK"
RESOLVED = "RESOLVED"


@dataclass
class Coil:
    coil_id: str
    start: object
    end: object
    low: float
    high: float
    outer_low: float
    outer_high: float
    width: float
    nested_width: float | None
    touches_low: int
    touches_high: int
    ema9_crosses: int
    activity_source: str
    bias: str
    bias_notes: tuple[str, ...]
    status: str = COILING

    @property
    def midpoint(self):
        return (self.outer_low + self.outer_high) / 2

    def as_reasons(self):
        return {"coil_id": self.coil_id, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "low": self.outer_low,
                "high": self.outer_high, "width": self.width,
                "nested_width": self.nested_width,
                "touches_low": self.touches_low,
                "touches_high": self.touches_high,
                "ema9_crosses": self.ema9_crosses,
                "activity_source": self.activity_source,
                "bias": self.bias, "bias_notes": list(self.bias_notes),
                "status": self.status}


@dataclass
class CoilEvent:
    kind: str
    coil: Coil
    message: str
    direction: str | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    runway: float | None = None
    checkpoint: float | None = None
    method: str | None = None
    fade_zone: MTFZone | None = None


def _ema(values, period):
    if not values:
        return []
    alpha = 2 / (period + 1)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1 - alpha) * out[-1])
    return out


def _ema_crosses(candles):
    ema9 = _ema([c.close for c in candles], 9)
    signs = [1 if c.close > average else -1 if c.close < average else 0
             for c, average in zip(candles, ema9)]
    crosses = 0
    previous = 0
    for sign in signs:
        if sign and previous and sign != previous:
            crosses += 1
        if sign:
            previous = sign
    return crosses


def _activity(candles):
    if all(c.volume is not None and c.volume > 0 for c in candles):
        return [float(c.volume) for c in candles], "traded volume"
    if all(getattr(c, "tick_count", None) is not None for c in candles):
        return [float(c.tick_count) for c in candles], "tick-count proxy"
    return None, None


def _bias(low, high, morning_state, vwap, levels):
    notes = []
    if morning_state == "TREND-UP" and vwap is not None and low >= vwap:
        notes.append("morning TREND-UP holding above VWAP")
    elif morning_state == "TREND-DOWN" and vwap is not None and high <= vwap:
        notes.append("morning TREND-DOWN holding below VWAP")
    for level in levels or []:
        if level.side == "S" and level.price < low and level.change_oi is not None:
            if level.change_oi > 0:
                notes.append(f"put writers ADDING ({level.change_oi:+,.0f}) below low")
            elif level.change_oi < 0:
                notes.append(f"put writers COVERING ({level.change_oi:+,.0f}) below low")
        elif level.side == "R" and level.price > high and level.change_oi is not None:
            if level.change_oi > 0:
                notes.append(f"call writers ADDING ({level.change_oi:+,.0f}) above high")
            elif level.change_oi < 0:
                notes.append(f"call writers COVERING ({level.change_oi:+,.0f}) above high")
    up = any("TREND-UP" in note or "put writers ADDING" in note
             or "call writers COVERING" in note for note in notes)
    down = any("TREND-DOWN" in note or "call writers ADDING" in note
               or "put writers COVERING" in note for note in notes)
    bias = "NEUTRAL" if up == down else "UPSIDE" if up else "DOWNSIDE"
    return bias, tuple(notes)


def detect_coil(candles, spot, morning_state="RANGE", vwap=None, levels=()):
    """Return the widest qualifying suffix coil; a tighter nested window is noted."""
    if spot is None or len(candles) < settings.COIL_MIN_CANDLES:
        return None
    maximum_width = min(settings.COIL_MAX_POINTS,
                        abs(float(spot)) * settings.COIL_MAX_SPOT_FRACTION)
    valid = []
    earliest = max(0, len(candles) - settings.COIL_SEARCH_CANDLES)
    for start_index in range(earliest, len(candles) - settings.COIL_MIN_CANDLES + 1):
        window = candles[start_index:]
        high = max(candle.high for candle in window)
        low = min(candle.low for candle in window)
        width = high - low
        if width <= 0 or width > maximum_width:
            continue
        tolerance = max(settings.COIL_TOUCH_TOLERANCE_PTS,
                        width * settings.COIL_TOUCH_TOLERANCE_FRACTION)
        touches_high = sum(candle.high >= high - tolerance for candle in window)
        touches_low = sum(candle.low <= low + tolerance for candle in window)
        activity, activity_source = _activity(window)
        split = len(window) // 2
        declining = (activity is not None and split > 0
                     and sum(activity[split:]) / (len(activity) - split)
                     < sum(activity[:split]) / split)
        crosses = _ema_crosses(window)
        if (touches_high < settings.COIL_TOUCHES_PER_BOUNDARY
                or touches_low < settings.COIL_TOUCHES_PER_BOUNDARY
                or not declining or crosses < settings.COIL_EMA9_MIN_CROSSES):
            continue
        valid.append((window, high, low, width, touches_high, touches_low,
                      crosses, activity_source))
    if not valid:
        return None
    outer = max(valid, key=lambda item: len(item[0]))
    nested = [item[3] for item in valid if item[0][0].start > outer[0][0].start]
    window, high, low, width, touches_high, touches_low, crosses, activity_source = outer
    bias, notes = _bias(low, high, morning_state, vwap, levels)
    return Coil(coil_id=f"COIL-{window[0].start.isoformat(timespec='seconds')}",
                start=window[0].start, end=window[-1].end,
                low=low, high=high, outer_low=low, outer_high=high,
                width=width, nested_width=min(nested) if nested else None,
                touches_low=touches_low, touches_high=touches_high,
                ema9_crosses=crosses, activity_source=activity_source,
                bias=bias, bias_notes=notes)


class CoilSnipeEngine:
    def __init__(self):
        self.coil: Coil | None = None
        self.pending = None
        self.retest = None

    def update_coil(self, candles, spot, morning_state="RANGE", vwap=None, levels=()):
        detected = detect_coil(candles, spot, morning_state, vwap, levels)
        if detected is None:
            return []
        if self.coil is not None and self.coil.status in (COILING, PENDING_BREAK):
            if detected.start == self.coil.start:
                self.coil.end = detected.end
                self.coil.low = min(self.coil.low, detected.low)
                self.coil.high = max(self.coil.high, detected.high)
                self.coil.outer_low = min(self.coil.outer_low, detected.outer_low)
                self.coil.outer_high = max(self.coil.outer_high, detected.outer_high)
                self.coil.width = self.coil.outer_high - self.coil.outer_low
                self.coil.nested_width = detected.nested_width
                self.coil.bias = detected.bias
                self.coil.bias_notes = detected.bias_notes
                return []
        self.coil = detected
        self.pending = None
        self.retest = None
        bias = "; ".join(detected.bias_notes) if detected.bias_notes else "flat-from-open / neutral"
        return [CoilEvent(
            "COIL", detected,
            f"COIL {detected.low:,.0f}-{detected.high:,.0f} | "
            f"{detected.width:.0f} pts | {detected.bias} bias ({bias}) | "
            f"liquidity collecting above/below | {detected.activity_source}")]

    def on_minute(self, candle):
        coil = self.coil
        if coil is None or coil.status == RESOLVED:
            return []
        if self.pending is None:
            sweep_up = candle.high > coil.high and candle.close < coil.high
            sweep_down = candle.low < coil.low and candle.close > coil.low
            if sweep_up or sweep_down:
                direction = "SHORT" if sweep_up else "LONG"
                zone = MTFZone(
                    f"COIL-FADE-{candle.start.isoformat()}", direction, candle.start,
                    float(candle.low if sweep_up else candle.high),
                    float(candle.high if sweep_up else candle.low),
                    float(coil.high if sweep_up else coil.low),
                    float(coil.midpoint))
                return [CoilEvent("FAKE-PURGE", coil,
                                  "liquidity purge returned inside coil; fade awaits "
                                  "5m confirmation", direction, fade_zone=zone)]
            if candle.close > coil.high or candle.close < coil.low:
                direction = "LONG" if candle.close > coil.high else "SHORT"
                boundary = coil.high if direction == "LONG" else coil.low
                self.pending = {"direction": direction, "boundary": boundary,
                                "break_time": candle.end, "retest": None}
                coil.status = PENDING_BREAK
                return [CoilEvent("BREAK-PENDING", coil,
                                  f"1m close beyond {boundary:,.0f}; awaiting 5m "
                                  "confirmation", direction)]
            return []

        if candle.end <= self.pending["break_time"]:
            return []
        direction = self.pending["direction"]
        boundary = self.pending["boundary"]
        if direction == "LONG":
            if candle.close <= boundary:
                coil.status = COILING
                self.pending = None
                return [CoilEvent("FAILED-BREAK", coil,
                                  "1m close returned inside coil", direction)]
            rejection = candle.low <= boundary and candle.close > candle.open
        else:
            if candle.close >= boundary:
                coil.status = COILING
                self.pending = None
                return [CoilEvent("FAILED-BREAK", coil,
                                  "1m close returned inside coil", direction)]
            rejection = candle.high >= boundary and candle.close < candle.open
        if rejection:
            self.pending["retest"] = float(candle.close)
        return []

    def on_five_minute(self, candle, levels, divergence_suppressed=False):
        if self.coil is None or self.pending is None:
            return []
        if candle.end <= self.pending["break_time"]:
            return []
        direction = self.pending["direction"]
        boundary = self.pending["boundary"]
        confirmed = candle.close > boundary if direction == "LONG" else candle.close < boundary
        if not confirmed:
            self.coil.status = COILING
            self.pending = None
            return [CoilEvent("FAILED-BREAK", self.coil,
                              "5m close failed to confirm coil break", direction)]
        if candle.end.time() > settings.COIL_LAST_ENTRY_TIME:
            return self._skip("LESS-THAN-40-MIN", "confirmation is too late for 15:10 exit")
        if divergence_suppressed:
            return self._skip("DIVERGENCE-TRAP-SKIPPED",
                              "continuation suppressed by boundary divergence")

        ahead = [level for level in levels
                 if level.side == ("R" if direction == "LONG" else "S")
                 and (level.price > boundary if direction == "LONG"
                      else level.price < boundary)]
        next_level = (min(ahead, key=lambda level: level.price) if direction == "LONG"
                      else max(ahead, key=lambda level: level.price)) if ahead else None
        if next_level is None:
            return self._skip("BREAK-INTO-WALL", "no next ladder level for runway")
        runway = (next_level.price - candle.close if direction == "LONG"
                  else candle.close - next_level.price)
        if runway < settings.ROOM_TO_RUN_MIN_PTS:
            return self._skip("BREAK-INTO-WALL",
                              f"runway {runway:.0f} pts to {next_level.label} "
                              f"(< {settings.ROOM_TO_RUN_MIN_PTS})")
        if next_level.change_oi is None or next_level.change_oi >= 0:
            return self._skip("BREAK-INTO-WALL",
                              f"writers STACKING or OI unavailable at "
                              f"{next_level.label} {next_level.price:,.0f}")
        retest = self.pending.get("retest")
        entry = retest if retest is not None else float(candle.close)
        risk = (entry - self.coil.midpoint if direction == "LONG"
                else self.coil.midpoint - entry)
        target = (boundary + self.coil.width if direction == "LONG"
                  else boundary - self.coil.width)
        checkpoint = next_level.price
        self.coil.status = RESOLVED
        event = CoilEvent("ENTRY", self.coil,
                          f"{('RETEST' if retest is not None else 'MOMENTUM')} "
                          f"{direction} break confirmed | entry {entry:,.2f} | "
                          f"SL {self.coil.midpoint:,.2f} | target {target:,.2f} | "
                          f"next {next_level.label} {checkpoint:,.2f}",
                          direction, entry, self.coil.midpoint, target, runway,
                          checkpoint, "RETEST" if retest is not None else "MOMENTUM")
        if risk <= 0:
            return [CoilEvent("GEOMETRY-SKIP", self.coil,
                              "coil midpoint invalidates confirmed entry geometry",
                              direction)]
        return [event]

    def _skip(self, kind, message):
        self.coil.status = RESOLVED
        event = CoilEvent(kind, self.coil, message,
                          self.pending.get("direction") if self.pending else None)
        self.pending = None
        return [event]