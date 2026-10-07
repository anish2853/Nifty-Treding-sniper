"""Hold-or-break decisions, approach alerts, and RSI divergence traps."""
from dataclasses import dataclass

import settings
from engines.mtf_scalp import MTFZone, detect_pullback_zone
from utils import ist_now


@dataclass
class LevelEvent:
    kind: str
    level_id: str
    label: str
    price: float
    direction: str | None = None
    message: str = ""
    zone: MTFZone | None = None


@dataclass
class _PendingBreak:
    level_id: str
    label: str
    price: float
    direction: str
    started_at: object
    closes: int = 0


def _zone_sources(levels):
    parts = []
    for level in levels:
        if "OI" in level.sources:
            continue
        if level.label == "VWAP":
            text = f"VWAP {level.price:,.0f}"
        elif level.label == "SWING-L":
            text = "swing lows"
        elif level.label == "SWING-H":
            text = "swing highs"
        else:
            text = level.label
        if text not in parts:
            parts.append(text)
    parts.sort(key=lambda text: (not text.startswith("VWAP"), text))
    return " + ".join(parts) or "liquidity levels"


def _watch_oi_bias(oi, side):
    if oi is None or oi.change_oi is None:
        return "OI unavailable → neutral bias"
    writer = "put" if side == "S" else "call"
    location = "below" if side == "S" else "above"
    amount = abs(oi.change_oi) / 1000
    if oi.change_oi > 0:
        return f"{writer} writers +{amount:g}k {location} → HOLD bias"
    if oi.change_oi < 0:
        return f"{writer} writers covering {amount:g}k {location} → BREAK bias"
    return f"{writer} writers flat {location} → neutral bias"


def rsi_values(closes, period: int = settings.DIVERGENCE_RSI_PERIOD):
    """Wilder RSI series aligned with closes; early, undefined values are None."""
    values = [None] * len(closes)
    if len(closes) <= period:
        return values
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def value(gain, loss):
        if loss == 0:
            return 100.0
        relative = gain / loss
        return 100 - 100 / (1 + relative)

    values[period] = value(average_gain, average_loss)
    for index in range(period + 1, len(closes)):
        change_index = index - 1
        average_gain = ((average_gain * (period - 1)) + gains[change_index]) / period
        average_loss = ((average_loss * (period - 1)) + losses[change_index]) / period
        values[index] = value(average_gain, average_loss)
    return values


def find_rsi_divergences(candles, levels, period: int = settings.DIVERGENCE_RSI_PERIOD):
    """Find two confirmed 2-bar pivots with price/RSI disagreement at a ladder level."""
    if len(candles) < period + 5:
        return []
    rsi = rsi_values([candle.close for candle in candles], period)
    pivots = {"high": [], "low": []}
    for index in range(2, len(candles) - 2):
        neighbors = candles[index - 2:index] + candles[index + 1:index + 3]
        if all(candles[index].high > other.high for other in neighbors):
            pivots["high"].append(index)
        if all(candles[index].low < other.low for other in neighbors):
            pivots["low"].append(index)
    if not levels:
        return []
    divergences = []
    for pivot_kind, side, divergence_kind, direction in (
            ("high", "R", "BEARISH", "LONG"),
            ("low", "S", "BULLISH", "SHORT")):
        indexes = pivots[pivot_kind]
        if len(indexes) < 2:
            continue
        previous_index, current_index = indexes[-2:]
        previous_rsi, current_rsi = rsi[previous_index], rsi[current_index]
        if previous_rsi is None or current_rsi is None:
            continue
        previous_price = (candles[previous_index].high if pivot_kind == "high"
                          else candles[previous_index].low)
        current_price = (candles[current_index].high if pivot_kind == "high"
                         else candles[current_index].low)
        diverged = (current_price > previous_price and current_rsi < previous_rsi
                    if pivot_kind == "high" else
                    current_price < previous_price and current_rsi > previous_rsi)
        if not diverged:
            continue
        candidates = [level for level in levels if level.side == side
                      and abs(level.price - current_price) <= settings.LEVEL_APPROACH_PTS]
        if candidates:
            level = min(candidates, key=lambda item: abs(item.price - current_price))
            divergences.append({"kind": divergence_kind, "direction": direction,
                                "level_id": level.level_id, "label": level.label,
                                "price": level.price,
                                "pivot_time": candles[current_index].start})
    return divergences


class LevelDecisionEngine:
    def __init__(self):
        self._watch_state: dict[str, dict] = {}
        self.pending_breaks: list[_PendingBreak] = []
        self.bar_index = 0
        self.suppressed_until: dict[str, int] = {}
        self._seen_divergences: set[tuple] = set()

    def approach_events(self, spot, levels, now=None) -> list[LevelEvent]:
        if spot is None:
            return []
        now = now or ist_now()
        by_side = {"S": [], "R": []}
        for level in levels:
            # Location relative to spot is authoritative; swing naming is provenance only.
            side = "S" if level.price <= spot else "R"
            by_side[side].append(level)

        candidates = []
        for side, side_levels in by_side.items():
            side_levels.sort(key=lambda item: item.price)
            groups = []
            for level in side_levels:
                if not groups or level.price - groups[-1][-1].price > \
                        settings.LEVEL_ZONE_MERGE_PTS:
                    groups.append([level])
                else:
                    groups[-1].append(level)
            for group in groups:
                low = min(level.price for level in group)
                high = max(level.price for level in group)
                distance = max(low - spot, spot - high, 0)
                key = ",".join(sorted(level.level_id for level in group))
                state = self._watch_state.setdefault(
                    key, {"last_alert": None, "left_far": False, "returned": False})
                if state["last_alert"] is not None:
                    if distance > settings.LEVEL_WATCH_RETURN_PTS:
                        state["left_far"] = True
                        state["returned"] = False
                    elif (state["left_far"]
                          and distance <= settings.LEVEL_APPROACH_PTS):
                        state["returned"] = True
                if distance > settings.LEVEL_APPROACH_PTS:
                    continue
                last_alert = state["last_alert"]
                cooled = (last_alert is None or
                          (now - last_alert).total_seconds()
                          >= settings.LEVEL_WATCH_COOLDOWN_SEC)
                eligible = last_alert is None or (cooled and state["returned"])
                if not eligible:
                    continue
                oi_levels = [level for level in side_levels
                             if "OI" in level.sources]
                oi = min(oi_levels, key=lambda item: abs(item.price - spot)) \
                    if oi_levels else None
                oi_read = _watch_oi_bias(oi, side)
                names = _zone_sources(group)
                label = "SUPPORT ZONE" if side == "S" else "RESISTANCE ZONE"
                message = (f"{label} {low:,.0f}-{high:,.0f} ({names}) | "
                           f"price {spot:,.0f} | {oi_read}")
                event_id = f"{label}:{low:.2f}-{high:.2f}@{now.isoformat(timespec='seconds')}"
                candidates.append((distance, key, event_id, label, low, message, state))

        if not candidates:
            return []
        _distance, key, level_id, label, price, message, state = min(
            candidates, key=lambda item: item[0])
        state["last_alert"] = now
        state["left_far"] = False
        state["returned"] = False
        return [LevelEvent("LEVEL-WATCH", level_id, label, price, message=message)]

    def on_minute(self, candle, levels, day_state: str, previous=None,
                  broken_level=None, vwap=None) -> list[LevelEvent]:
        if day_state not in ("TREND-UP", "TREND-DOWN", "RANGE"):
            return []
        if not (settings.ENTRY_EVAL_START <= candle.end.time() < settings.LAST_ENTRY_TIME):
            return []
        events = []
        for level in levels:
            swept_up = level.side == "R" and candle.high > level.price \
                and candle.close < level.price
            swept_down = level.side == "S" and candle.low < level.price \
                and candle.close > level.price
            if swept_up or swept_down:
                direction = "SHORT" if swept_up else "LONG"
                zone = MTFZone(
                    zone_id=f"FADE-{level.level_id}-{candle.start.isoformat()}",
                    direction=direction, formed_at=candle.start,
                    trigger=float(candle.low if swept_up else candle.high),
                    stop=float(candle.high if swept_up else candle.low),
                    broken_level=float(level.price), vwap=float(level.price))
                events.append(LevelEvent("FADE-ZONE", level.level_id, level.label,
                                         level.price, direction,
                                         "1m sweep rejected; awaiting 5m confirmation",
                                         zone))
                continue
            broke_up = level.side == "R" and candle.close > level.price
            broke_down = level.side == "S" and candle.close < level.price
            if (broke_up or broke_down) and not any(
                    pending.level_id == level.level_id for pending in self.pending_breaks):
                direction = "LONG" if broke_up else "SHORT"
                self.pending_breaks.append(_PendingBreak(
                    level.level_id, level.label, level.price, direction, candle.end))
                events.append(LevelEvent("BREAK-PENDING", level.level_id, level.label,
                                         level.price, direction,
                                         "1m close through level; awaiting 5m close"))
        pullback = detect_pullback_zone(candle, previous, day_state, broken_level, vwap)
        if pullback is not None:
            events.append(LevelEvent("MTF-ZONE", pullback.zone_id, "MTF pullback",
                                     pullback.trigger, pullback.direction,
                                     "1m trend pullback; awaiting 5m confirmation",
                                     pullback))
        return events

    def on_five_minute(self, candle, levels, history) -> list[LevelEvent]:
        self.bar_index += 1
        events = []
        for divergence in find_rsi_divergences(history, levels):
            key = (divergence["kind"], divergence["level_id"],
                   divergence["pivot_time"])
            if key in self._seen_divergences:
                continue
            self._seen_divergences.add(key)
            self.suppressed_until[divergence["level_id"]] = (
                self.bar_index + settings.DIVERGENCE_SUPPRESS_CANDLES)
            events.append(LevelEvent(
                f"{divergence['kind']}-DIVERGENCE", divergence["level_id"],
                divergence["label"], divergence["price"], divergence["direction"],
                f"{divergence['kind']} RSI divergence at {divergence['label']}"))

        remaining = []
        for pending in self.pending_breaks:
            if candle.end <= pending.started_at:
                remaining.append(pending)
                continue
            pending.closes += 1
            beyond = candle.close > pending.price if pending.direction == "LONG" \
                else candle.close < pending.price
            if beyond:
                trap = self.bar_index <= self.suppressed_until.get(pending.level_id, -1)
                if trap:
                    events.append(LevelEvent(
                        "DIVERGENCE-TRAP-SKIPPED", pending.level_id, pending.label,
                        pending.price, pending.direction,
                        "DIVERGENCE-TRAP-SKIPPED: continuation suppressed"))
                else:
                    far_side = [level for level in levels
                                if level.side == ("R" if pending.direction == "LONG" else "S")
                                and (level.price > pending.price if pending.direction == "LONG"
                                     else level.price < pending.price)
                                and level.change_oi is not None]
                    wall = min(far_side, key=lambda level: abs(level.price - pending.price)) \
                        if far_side else None
                    if wall is not None and wall.change_oi < 0:
                        events.append(LevelEvent(
                            "CONTINUATION", pending.level_id, pending.label,
                            pending.price, pending.direction,
                            f"5m confirmed; writers covering beyond level at "
                            f"{wall.price:,.0f}"))
                    else:
                        events.append(LevelEvent(
                            "BREAK-INTO-WALL", pending.level_id, pending.label,
                            pending.price, pending.direction,
                            "BREAK INTO WALL: no far-side OI covering"))
            elif pending.closes >= 2:
                events.append(LevelEvent("BREAK-STALE", pending.level_id,
                                         pending.label, pending.price,
                                         pending.direction,
                                         "break attempt expired without 5m confirmation"))
            else:
                remaining.append(pending)
        self.pending_breaks = remaining
        return events