from datetime import datetime, timedelta, timezone

import settings
from engines.candles import Candle, parse_chart_payload
from engines.mtf_scalp import (CONFIRMED, EXPIRED, STALE, WAITING,
                               confirm_zone, confirmation_is_fresh,
                               detect_pullback_zone, geometry_ok)


NOW = datetime(2026, 10, 7, 10, 0, tzinfo=settings.IST)


def bar(start, open_, high, low, close):
    return Candle(start=start, open=open_, high=high, low=low, close=close)


def test_long_pullback_zone_and_confirmation():
    previous = bar(NOW - timedelta(minutes=1), 101, 105, 100, 103)
    pullback = bar(NOW, 103, 104, 102, 99)
    zone = detect_pullback_zone(pullback, previous, "TREND-UP", 101, 101.5)

    assert zone is not None
    assert (zone.direction, zone.trigger, zone.stop, zone.status) == (
        "LONG", 104, 102, WAITING)
    confirming = bar(NOW, 103, 106, 103, 105)
    assert confirm_zone(zone, confirming) == CONFIRMED
    assert zone.entry_spot == 105
    assert geometry_ok(zone)
    assert confirmation_is_fresh(zone, zone.confirm_time)


def test_short_pullback_is_mirrored():
    previous = bar(NOW - timedelta(minutes=1), 99, 100, 95, 97)
    pullback = bar(NOW, 97, 101, 96, 102)
    zone = detect_pullback_zone(pullback, previous, "TREND-DOWN", 103, 102.5)

    assert zone is not None
    assert (zone.direction, zone.trigger, zone.stop) == ("SHORT", 96, 101)
    confirming = bar(NOW, 98, 99, 72, 74)
    assert confirm_zone(zone, confirming) == CONFIRMED
    assert zone.risk_points == 27
    assert not geometry_ok(zone)


def test_zone_expires_inside_and_goes_stale_after_two_closes():
    previous = bar(NOW - timedelta(minutes=1), 101, 105, 100, 103)
    zone = detect_pullback_zone(bar(NOW, 103, 104, 102, 99), previous,
                                "TREND-UP", 101, 101.5)
    assert confirm_zone(zone, bar(NOW, 103, 104, 102, 103)) == EXPIRED

    stale_zone = detect_pullback_zone(bar(NOW, 103, 104, 102, 99), previous,
                                      "TREND-UP", 101, 101.5)
    assert confirm_zone(stale_zone, bar(NOW, 101, 101.5, 100, 100.5)) == WAITING
    assert confirm_zone(stale_zone, bar(NOW + timedelta(minutes=5), 100, 100.5,
                                        99, 99.5)) == STALE


def test_zone_requires_trend_location_and_session_window():
    previous = bar(NOW - timedelta(minutes=1), 101, 105, 100, 103)
    pullback = bar(NOW, 103, 104, 102, 99)
    assert detect_pullback_zone(pullback, previous, "RANGE", 101, 101.5) is None
    assert detect_pullback_zone(pullback, previous, "TREND-UP", 103, 101.5) is None
    late = bar(NOW.replace(hour=14, minute=30), 103, 104, 102, 99)
    assert detect_pullback_zone(late, previous, "TREND-UP", 101, 101.5) is None


def test_chart_ticks_support_one_minute_and_five_minute_buckets():
    def tick(minute, second, price):
        stamp = datetime(2026, 10, 7, 10, minute, second, tzinfo=timezone.utc)
        return [stamp.timestamp() * 1000, price, ""]

    payload = {"grapthData": [tick(0, 2, 100), tick(0, 40, 102),
                              tick(1, 5, 99), tick(1, 55, 101)]}
    one_minute = parse_chart_payload(payload, interval_minutes=1)
    five_minute = parse_chart_payload(payload)

    assert len(one_minute) == 2 and one_minute[0].interval_minutes == 1
    assert (one_minute[0].open, one_minute[0].high, one_minute[0].low,
            one_minute[0].close) == (100, 102, 100, 102)
    assert one_minute[0].tick_count == 2
    assert one_minute[0].end == one_minute[0].start + timedelta(minutes=1)
    assert len(five_minute) == 1 and five_minute[0].interval_minutes == 5