from datetime import datetime, timedelta
from types import SimpleNamespace

import settings
from engines.candles import Candle
from engines.coil_snipe import (COILING, PENDING_BREAK, RESOLVED,
                                Coil, CoilSnipeEngine, detect_coil)


NOW = datetime(2026, 10, 7, 10, 0, tzinfo=settings.IST)


def bars(count=8, width=60, volumes=None, start=NOW):
    volumes = volumes or [100 - 8 * index for index in range(count)]
    center = 22000.0
    low, high = center - width / 2, center + width / 2
    result = []
    for index in range(count):
        up = index % 2 == 0
        close = high - 10 if up else low + 10
        open_ = close - 3 if up else close + 3
        candle_high = high if up else close + 4
        candle_low = close - 4 if up else low
        result.append(Candle(start + timedelta(minutes=5 * index), open_,
                             candle_high, candle_low, close,
                             volume=volumes[index]))
    return result


def test_coil_qualifies_with_range_touches_volume_and_ema9_crosses():
    coil = detect_coil(bars(), spot=22000, morning_state="TREND-UP", vwap=21960)

    assert coil is not None and coil.status == COILING
    assert coil.width == 60
    assert coil.touches_high >= 2 and coil.touches_low >= 2
    assert coil.ema9_crosses >= 2
    assert coil.bias == "UPSIDE"


def test_nested_coil_keeps_qualifying_outer_width():
    sequence = bars(12, width=60)
    sequence[-8:] = bars(8, width=30, start=sequence[-8].start)
    coil = detect_coil(sequence, spot=22000)

    assert coil is not None
    assert coil.width == 60
    assert coil.nested_width == 30


def test_coil_requires_declining_activity_and_caps_width():
    assert detect_coil(bars(volumes=[10] * 8), spot=22000) is None
    assert detect_coil(bars(width=80), spot=22000) is None


def test_fake_purge_returns_fade_zone_without_resolving_coil():
    engine = CoilSnipeEngine()
    engine.coil = Coil("C1", NOW, NOW + timedelta(minutes=40), 100, 160,
                       100, 160, 60, None, 2, 2, 4, "traded volume",
                       "NEUTRAL", ())
    minute = Candle(NOW, 159, 163, 158, 159, interval_minutes=1)

    event = engine.on_minute(minute)[0]
    assert event.kind == "FAKE-PURGE"
    assert event.direction == "SHORT"
    assert engine.coil.status == COILING


def test_break_uses_retest_and_requires_covering_with_runway():
    engine = CoilSnipeEngine()
    engine.coil = Coil("C1", NOW, NOW + timedelta(minutes=40), 100, 160,
                       100, 160, 60, 30, 2, 2, 4, "traded volume",
                       "UPSIDE", ())
    event = engine.on_minute(Candle(NOW, 159, 163, 158, 162,
                                    interval_minutes=1))[0]
    assert event.kind == "BREAK-PENDING"
    assert engine.on_minute(Candle(NOW + timedelta(minutes=1), 162, 164, 159, 163,
                                   interval_minutes=1)) == []
    wall = SimpleNamespace(side="R", price=210.0, label="R1", change_oi=-5000)
    confirm = Candle(NOW, 162, 172, 161, 170)
    entry = engine.on_five_minute(confirm, [wall])[0]
    assert entry.kind == "ENTRY" and entry.method == "RETEST"
    assert entry.entry == 163 and entry.stop == 130 and entry.target == 220
    assert engine.coil.status == RESOLVED


def test_break_into_stacked_wall_is_rejected():
    engine = CoilSnipeEngine()
    engine.coil = Coil("C1", NOW, NOW + timedelta(minutes=40), 100, 160,
                       100, 160, 60, None, 2, 2, 4, "traded volume",
                       "NEUTRAL", ())
    engine.on_minute(Candle(NOW, 159, 163, 158, 162, interval_minutes=1))
    wall = SimpleNamespace(side="R", price=190.0, label="R1", change_oi=5000)

    event = engine.on_five_minute(Candle(NOW, 162, 172, 161, 170), [wall])[0]
    assert event.kind == "BREAK-INTO-WALL"
    assert engine.coil.status == RESOLVED