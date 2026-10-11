"""Unit tests for replay data source, day validation, and 1m bar synthesis."""
from datetime import datetime, date, timedelta
import pytz
import pytest

from engines.candles import Candle
from data_sources.replay_data import (
    check_smartapi_configured,
    synthesize_1m_candles,
    DayDiagnosticRecord,
)

IST = pytz.timezone("Asia/Kolkata")


def test_synthesize_1m_candles_bullish():
    t0 = datetime(2026, 8, 10, 9, 15, tzinfo=IST)
    five_min = [
        Candle(
            start=t0,
            open=24000.0,
            high=24050.0,
            low=23980.0,
            close=24040.0,
            volume=5000.0,
            source="test-5m",
            interval_minutes=5,
        )
    ]
    sub_bars = synthesize_1m_candles(five_min)
    assert len(sub_bars) == 5
    assert sub_bars[0].start == t0
    assert sub_bars[0].open == 24000.0
    assert sub_bars[-1].close == 24040.0
    assert min(b.low for b in sub_bars) == 23980.0
    assert max(b.high for b in sub_bars) == 24050.0
    assert sum(b.volume for b in sub_bars) == pytest.approx(5000.0)

    for b in sub_bars:
        assert b.high >= max(b.open, b.close)
        assert b.low <= min(b.open, b.close)


def test_synthesize_1m_candles_bearish():
    t0 = datetime(2026, 8, 10, 9, 20, tzinfo=IST)
    five_min = [
        Candle(
            start=t0,
            open=24040.0,
            high=24060.0,
            low=23950.0,
            close=23970.0,
            volume=10000.0,
            source="test-5m",
            interval_minutes=5,
        )
    ]
    sub_bars = synthesize_1m_candles(five_min)
    assert len(sub_bars) == 5
    assert sub_bars[0].start == t0
    assert sub_bars[0].open == 24040.0
    assert sub_bars[-1].close == 23970.0
    assert min(b.low for b in sub_bars) == 23950.0
    assert max(b.high for b in sub_bars) == 24060.0
    assert sum(b.volume for b in sub_bars) == pytest.approx(10000.0)

    for b in sub_bars:
        assert b.high >= max(b.open, b.close)
        assert b.low <= min(b.open, b.close)


def test_smartapi_check_without_env(monkeypatch, capsys):
    monkeypatch.delenv("ANGEL_API_KEY", raising=False)
    configured = check_smartapi_configured()
    assert configured is False
    captured = capsys.readouterr()
    assert "SETUP NEEDED: register at smartapi.angelbroking.com" in captured.out
