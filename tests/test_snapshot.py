"""Unit tests for canonical MarketSnapshot (Phase 1)."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from data_sources.snapshot import MarketSnapshot, MarketSnapshotBuilder
from engines.candles import Candle
from utils import ist_now


def test_market_snapshot_immutability():
    now = ist_now()
    snap = MarketSnapshot(
        snapshot_id="SNAP-001",
        timestamp=now,
        spot=22500.0,
        vwap=22480.0,
        atr=25.0,
        pdh=22600.0,
        pdl=22400.0,
        eq=22500.0,
    )
    assert snap.snapshot_id == "SNAP-001"
    assert snap.spot == 22500.0
    assert snap.normalized_vwap_distance() == (22500.0 - 22480.0) / 25.0

    # Ensure frozen dataclass raises on modification
    try:
        snap.spot = 22600.0
        assert False, "Should not be able to mutate frozen snapshot"
    except Exception:
        pass


def test_market_snapshot_builder():
    now = ist_now()
    c1 = Candle(now - timedelta(minutes=10), 22400, 22450, 22390, 22440, volume=1000)
    c2 = Candle(now - timedelta(minutes=5), 22440, 22520, 22430, 22500, volume=1200)

    market = SimpleNamespace(
        spot_value=lambda: 22500.0,
        candles=[c1, c2],
        candle_engine=SimpleNamespace(minute_candles=[]),
        pdh=22600.0,
        pdl=22400.0,
        pdc=22450.0,
        chain=SimpleNamespace(pcr_total=1.25, underlying=22500.0),
        vix=SimpleNamespace(value={"value": 14.5, "prev_close": 13.8}),
        latest_candle_age_minutes=lambda t: 2.0,
    )

    snap = MarketSnapshotBuilder.build(market, now=now)
    assert snap.snapshot_id.startswith("SNAP-")
    assert snap.spot == 22500.0
    assert snap.eq == 22500.0
    assert len(snap.candles) == 2
    assert snap.is_blind is False
    assert snap.atr is not None
    assert snap.vwap is not None
    assert snap.pcr == 1.25
    assert snap.vix == 14.5
