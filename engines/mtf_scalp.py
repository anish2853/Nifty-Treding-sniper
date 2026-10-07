"""Shared 1m-zone / 5m-confirm transitions for live MTF scalp and replay."""
from dataclasses import dataclass
from datetime import datetime, time

import settings


WAITING = "WAITING"
CONFIRMED = "CONFIRMED"
EXPIRED = "EXPIRED"
STALE = "STALE"


@dataclass
class MTFZone:
    zone_id: str
    direction: str
    formed_at: datetime
    trigger: float
    stop: float
    broken_level: float
    vwap: float
    status: str = WAITING
    confirmation_count: int = 0
    confirm_time: datetime | None = None
    entry_spot: float | None = None

    @property
    def risk_points(self) -> float | None:
        if self.entry_spot is None:
            return None
        return (self.entry_spot - self.stop if self.direction == "LONG"
                else self.stop - self.entry_spot)

    def as_reasons(self) -> dict:
        return {"zone_id": self.zone_id, "direction": self.direction,
                "formed_at": self.formed_at.isoformat(timespec="seconds"),
                "trigger": self.trigger, "stop": self.stop,
                "broken_level": self.broken_level, "vwap": self.vwap,
                "status": self.status,
                "confirmation_count": self.confirmation_count,
                "confirm_time": self.confirm_time.isoformat(timespec="seconds")
                if self.confirm_time else None,
                "entry_spot": self.entry_spot}


def detect_pullback_zone(minute, previous, day_state, broken_level, vwap):
    """Detect a one-minute pullback in a trend and return its WAITING zone."""
    if day_state == "TREND-UP":
        direction = "LONG"
    elif day_state == "TREND-DOWN":
        direction = "SHORT"
    else:
        return None
    if previous is None or broken_level is None or vwap is None:
        return None
    if not (settings.ENTRY_EVAL_START <= minute.start.time() < settings.LAST_ENTRY_TIME):
        return None
    if minute.high <= minute.low:
        return None

    if direction == "LONG":
        valid = (minute.close < minute.open and minute.close < previous.low
                 and minute.low > broken_level and minute.low > vwap)
        trigger, stop = minute.high, minute.low
    else:
        valid = (minute.close > minute.open and minute.close > previous.high
                 and minute.high < broken_level and minute.high < vwap)
        trigger, stop = minute.low, minute.high
    if not valid:
        return None
    zone_id = f"{direction}-{minute.start.isoformat(timespec='seconds')}"
    return MTFZone(zone_id, direction, minute.start, float(trigger), float(stop),
                   float(broken_level), float(vwap))


def confirm_zone(zone: MTFZone, candle) -> str:
    """Advance a WAITING zone on each eligible five-minute close.

    A close through the trigger confirms. A close inside the zone expires it;
    otherwise a zone with no confirmation after two closes becomes stale.
    """
    if zone.status != WAITING or candle.end <= zone.formed_at:
        return zone.status
    zone.confirmation_count += 1
    if zone.direction == "LONG":
        if candle.close > zone.trigger:
            zone.status = CONFIRMED
        elif zone.stop <= candle.close <= zone.trigger:
            zone.status = EXPIRED
    else:
        if candle.close < zone.trigger:
            zone.status = CONFIRMED
        elif zone.trigger <= candle.close <= zone.stop:
            zone.status = EXPIRED
    if zone.status == CONFIRMED:
        zone.confirm_time = candle.end
        zone.entry_spot = float(candle.close)
    elif zone.confirmation_count >= 2:
        zone.status = STALE
    return zone.status


def confirmation_is_fresh(zone: MTFZone, now: datetime) -> bool:
    return (zone.status == CONFIRMED and zone.confirm_time is not None
            and 0 <= (now - zone.confirm_time).total_seconds()
            <= settings.FIRE_SLO_SECONDS)


def geometry_ok(zone: MTFZone, max_risk_points: float | None = None) -> bool:
    limit = max_risk_points or settings.MTF_MAX_RISK_POINTS
    risk = zone.risk_points
    return risk is not None and 0 < risk <= limit