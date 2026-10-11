"""Canonical, immutable, versioned MarketSnapshot (Phase 1).

Every strategy, the Arbiter, the PositionManager, and the RiskGovernor
evaluate THE SAME snapshot instance. No strategy may independently fetch or
invent its own market state.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import settings
from engines.candles import Candle
from utils import ist_now

log = logging.getLogger(__name__)


def _compute_atr(candles: tuple[Candle, ...], period: int = 14) -> float | None:
    """True Range smoothed over `period` bars on 5-min candles."""
    if len(candles) < 2:
        return None
    tr_list = []
    for i in range(1, len(candles)):
        h = candles[i].high
        l = candles[i].low
        prev_c = candles[i - 1].close
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)
    if len(tr_list) < period:
        return sum(tr_list) / len(tr_list) if tr_list else None
    # Simple rolling average of recent TR
    return sum(tr_list[-period:]) / period


def _compute_ema(closes: list[float], period: int = 20) -> float | None:
    if not closes:
        return None
    if len(closes) < period:
        return sum(closes) / len(closes)
    k = 2.0 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return ema


def _compute_vwap(candles: tuple[Candle, ...]) -> tuple[float | None, bool]:
    """Returns (vwap, is_proxy)."""
    if not candles:
        return None, False
    vol_sum = 0.0
    pv_sum = 0.0
    has_vol = any((c.volume or 0) > 0 for c in candles)
    for c in candles:
        typical = (c.high + c.low + c.close) / 3.0
        vol = float(c.volume) if has_vol and c.volume else 1.0
        pv_sum += typical * vol
        vol_sum += vol
    return (pv_sum / vol_sum) if vol_sum > 0 else None, not has_vol


@dataclass(frozen=True)
class MarketSnapshot:
    """Immutable versioned market state for one evaluation instant."""
    snapshot_id: str
    timestamp: datetime
    spot: float
    candles: tuple[Candle, ...] = field(default_factory=tuple)
    minute_candles: tuple[Candle, ...] = field(default_factory=tuple)
    vwap: float | None = None
    vwap_is_proxy: bool = False
    ema20: float | None = None
    atr: float | None = None
    chain: Any = None
    oi_velocity: dict[int, float] = field(default_factory=dict)
    pcr: float | None = None
    vix: float | None = None
    vix_prev: float | None = None
    pdh: float | None = None
    pdl: float | None = None
    pdc: float | None = None
    eq: float | None = None
    orb: dict[str, float] | None = None
    is_blind: bool = False
    data_freshness_sec: float = 0.0
    liquidity_map: Any = None
    source_timestamps: dict[str, str] = field(default_factory=dict)

    @property
    def latest_candle(self) -> Candle | None:
        return self.candles[-1] if self.candles else None

    @property
    def latest_minute(self) -> Candle | None:
        return self.minute_candles[-1] if self.minute_candles else None

    def normalized_vwap_distance(self) -> float | None:
        """Distance from VWAP normalized by ATR: |spot - VWAP| / ATR."""
        if self.vwap is None or not self.atr or self.atr <= 0:
            return None
        return abs(self.spot - self.vwap) / self.atr


class MarketSnapshotBuilder:
    """Builds canonical MarketSnapshot from MarketDataStore."""

    _sequence: int = 0

    @classmethod
    def build(cls, market, now: datetime | None = None) -> MarketSnapshot:
        cls._sequence += 1
        now = now or ist_now()
        ts_str = now.strftime("%Y%m%d-%H%M%S")
        snapshot_id = f"SNAP-{ts_str}-{cls._sequence:04d}"

        spot = float(market.spot_value() or (market.candles[-1].close if market.candles else 0.0))
        candles = tuple(market.candles or ())
        minute_candles = tuple(getattr(market.candle_engine, "minute_candles", ()) or ())

        # Volatility / Indicators
        atr = _compute_atr(candles, period=14)
        vwap, vwap_is_proxy = _compute_vwap(candles)
        ema20 = _compute_ema([c.close for c in candles], period=20)

        # Previous day levels & EQ
        pdh = float(market.pdh) if market.pdh is not None else None
        pdl = float(market.pdl) if market.pdl is not None else None
        pdc = float(market.pdc) if market.pdc is not None else None
        eq = (pdh + pdl) / 2.0 if (pdh is not None and pdl is not None) else None

        # ORB
        from engines.candles import orb_levels
        orb = orb_levels(candles) if candles else None

        # Chain & VIX
        chain = market.chain
        pcr = float(chain.pcr_total) if chain and getattr(chain, "pcr_total", None) is not None else None
        vix_val = None
        vix_prev = None
        if market.vix and market.vix.value:
            vix_val = market.vix.value.get("value")
            vix_prev = market.vix.value.get("prev_close")

        # Blindness check
        age_min = market.latest_candle_age_minutes(now) if hasattr(market, "latest_candle_age_minutes") else None
        is_blind = age_min is None or age_min > 5.0
        data_freshness_sec = (age_min * 60.0) if age_min is not None else 9999.0

        # OI Velocities
        oi_velocity = {}
        if hasattr(market, "level_runtime") and hasattr(market.level_runtime, "velocities"):
            oi_velocity = dict(market.level_runtime.velocities or {})

        sources = {
            "candle_source": getattr(market, "chain_source", "nse_chart"),
            "asof": now.isoformat(timespec="seconds"),
        }

        return MarketSnapshot(
            snapshot_id=snapshot_id,
            timestamp=now,
            spot=spot,
            candles=candles,
            minute_candles=minute_candles,
            vwap=vwap,
            vwap_is_proxy=vwap_is_proxy,
            ema20=ema20,
            atr=atr,
            chain=chain,
            oi_velocity=oi_velocity,
            pcr=pcr,
            vix=float(vix_val) if vix_val is not None else None,
            vix_prev=float(vix_prev) if vix_prev is not None else None,
            pdh=pdh,
            pdl=pdl,
            pdc=pdc,
            eq=eq,
            orb=orb,
            is_blind=is_blind,
            data_freshness_sec=data_freshness_sec,
            source_timestamps=sources,
        )
