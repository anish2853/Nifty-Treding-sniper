from datetime import datetime, timedelta
from types import SimpleNamespace

import settings
from engines.candles import Candle
from engines.level_decision import (LevelDecisionEngine, find_rsi_divergences,
                                    rsi_values)
from engines.level_grid import GridLevel


NOW = datetime(2026, 10, 7, 10, 0, tzinfo=settings.IST)


def bar(start, open_, high, low, close, interval=5):
    return Candle(start, open_, high, low, close, interval_minutes=interval)


def level(level_id, price, side, change_oi=None, oi=None):
    return SimpleNamespace(level_id=level_id, label=level_id, price=price,
                           side=side, change_oi=change_oi, oi=oi)


def test_level_watch_emits_once_with_oi_bias():
    engine = LevelDecisionEngine()
    levels = [
        GridLevel("VWAP", "VWAP", 22661, "R", ("VWAP",)),
        GridLevel("SWING-H", "SWING-H", 22659, "R", ("SWING-H",)),
        GridLevel("OI-S1", "S1", 22668, "R", ("OI",),
                  oi=300000, change_oi=8000, rank=1),
        GridLevel("R2", "R2", 22680, "S", ("SWING-L",)),
    ]
    now = NOW
    events = engine.approach_events(22670, levels, now=now)
    assert len(events) == 1
    assert "SUPPORT ZONE 22,659-22,668" in events[0].message
    assert "VWAP 22,661 + swing highs" in events[0].message
    assert "put writers +8k below → HOLD bias" in events[0].message
    assert engine.approach_events(22670, levels[:3],
                                  now=now + timedelta(minutes=1)) == []


def test_watch_realerts_only_after_15_point_excursion_return_and_30_minutes():
    engine = LevelDecisionEngine()
    resistance = GridLevel("R1", "R1", 100, "S", ("OI",),
                           oi=300000, change_oi=-8000, rank=1)
    assert engine.approach_events(82, [resistance], now=NOW)
    assert engine.approach_events(130, [resistance],
                                  now=NOW + timedelta(minutes=5)) == []
    assert engine.approach_events(90, [resistance],
                                  now=NOW + timedelta(minutes=20)) == []
    event = engine.approach_events(90, [resistance],
                                   now=NOW + timedelta(minutes=31))
    assert len(event) == 1
    assert event[0].message.startswith("RESISTANCE ZONE")
    assert "call writers covering 8k above → BREAK bias" in event[0].message


def test_sweep_generates_mtf_fade_zone():
    engine = LevelDecisionEngine()
    resistance = level("R1", 100, "R", 8000, 300000)
    events = engine.on_minute(bar(NOW, 99, 103, 97, 99, interval=1),
                              [resistance], "RANGE")
    assert events[0].kind == "FADE-ZONE"
    assert events[0].direction == "SHORT"
    assert events[0].zone.trigger == 97
    assert events[0].zone.stop == 103


def test_break_requires_far_side_oi_covering_and_expires_after_two_closes():
    engine = LevelDecisionEngine()
    resistance = level("R1", 100, "R", 9000, 400000)
    far_wall_covering = level("R2", 125, "R", -5000, 200000)
    pending = engine.on_minute(bar(NOW, 99, 103, 99, 102, interval=1),
                               [resistance, far_wall_covering], "TREND-UP")
    assert pending[0].kind == "BREAK-PENDING"
    confirm = engine.on_five_minute(bar(NOW, 102, 130, 101, 126),
                                    [resistance, far_wall_covering], [])
    assert confirm[0].kind == "CONTINUATION"

    engine.on_minute(bar(NOW, 99, 103, 99, 102, interval=1),
                     [resistance], "TREND-UP")
    events = engine.on_five_minute(bar(NOW, 99, 100, 98, 99), [resistance], [])
    assert events == []
    events = engine.on_five_minute(bar(NOW + timedelta(minutes=5), 99, 100, 98, 99),
                                   [resistance], [])
    assert events[0].kind == "BREAK-STALE"


def test_rsi_divergence_suppresses_confirmed_continuation():
    closes = (list(range(100, 115))
              + [110, 106, 102, 98, 94, 96, 98, 100, 102, 104, 106, 108,
                 110, 112, 113, 114, 115, 116, 115, 114, 113])
    history = [bar(NOW + timedelta(minutes=5 * index), close,
                   close + 0.5, close - 0.5, close)
               for index, close in enumerate(closes)]
    resistance = level("R1", 116.5, "R", 9000, 400000)
    far_wall_covering = level("R2", 140, "R", -5000, 200000)
    divergences = find_rsi_divergences(history, [resistance])
    assert rsi_values(closes)[-1] is not None
    assert divergences and divergences[0]["kind"] == "BEARISH"

    engine = LevelDecisionEngine()
    pending = engine.on_minute(bar(NOW, 116, 118, 115, 117, interval=1),
                               [resistance, far_wall_covering], "TREND-UP")
    assert pending[0].kind == "BREAK-PENDING"
    events = engine.on_five_minute(bar(NOW, 117, 119, 116, 118),
                                   [resistance, far_wall_covering], history)
    assert any(event.kind == "DIVERGENCE-TRAP-SKIPPED" for event in events)