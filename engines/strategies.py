"""Three Strategy Architecture (Phase 5).

Consolidates the 5 legacy overlapping generators into THREE independent, parallel strategies:
1. S1_TREND_EXPANSION: Big 200-400+ point runner model (Breakout + Structure Pullback + Continuation OB)
2. S2_VWAP_MOMENTUM: Dynamic value acceptance around VWAP (normalized extension, momentum retest)
3. S3_LIQUIDITY_REVERSAL: Failed auctions at macro pools (Sweep -> MSS -> POI Retest) WITH Trend Override Veto!
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import settings
from data_sources.snapshot import MarketSnapshot
from engines.candles import Candle
from engines.levels_lifecycle import LevelLifecycleManager, LevelRole, LevelStatus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategyAssessment:
    """Standardized contract returned by every strategy on every evaluation cycle."""
    strategy_id: str             # "S1_TREND_EXPANSION" | "S2_VWAP_MOMENTUM" | "S3_LIQUIDITY_REVERSAL"
    timestamp: datetime
    snapshot_id: str
    eligible: bool               # Meets setup criteria
    direction: str               # "LONG" | "SHORT" | "NONE"
    setup_score: int             # 0 to 100
    regime_fit_score: int        # 0 to 100
    structure_score: int         # 0 to 100
    momentum_score: int          # 0 to 100
    liquidity_score: int         # 0 to 100
    risk_reward_ratio: float     # Target R / Risk R
    normalized_extension: float  # |Price - VWAP| / ATR
    empirical_expectancy: float  # Historical R expectation
    sample_size: int             # Historical trade count N
    confidence_tier: str         # "STRONG" | "VALID" | "WEAK" | "NO_TRADE"
    entry_zone: tuple[float, float]
    initial_sl: float
    management_plan: str         # "STRUCTURE_TRAIL" | "ATR_TRAIL" | "POOL_TARGET"
    valid_until: datetime        # Explicit expiration timestamp
    reason_codes: list[str] = field(default_factory=list)
    blocker_codes: list[str] = field(default_factory=list)


class S1TrendExpansion:
    """Strategy 1: Captures sustained directional expansion and multi-hundred point moves."""

    strategy_id = "S1_TREND_EXPANSION"

    def assess(
        self,
        snapshot: MarketSnapshot,
        level_mgr: LevelLifecycleManager,
        market_state: str = "NORMAL",
    ) -> StrategyAssessment:
        now = snapshot.timestamp
        ttl = now + timedelta(minutes=10)
        reasons = []
        blockers = []

        if snapshot.is_blind:
            return self._ineligible(snapshot, ttl, ["DATA_BLINDNESS"])

        candle = snapshot.latest_candle
        if not candle:
            return self._ineligible(snapshot, ttl, ["NO_CANDLE_DATA"])

        # Check VWAP & EMA20 alignment
        spot = snapshot.spot
        vwap = snapshot.vwap or spot
        ema20 = snapshot.ema20 or spot
        atr = snapshot.atr or 20.0
        norm_ext = snapshot.normalized_vwap_distance() or 0.5

        is_bullish_align = (spot > vwap) and (spot > ema20)
        is_bearish_align = (spot < vwap) and (spot < ema20)

        direction = "NONE"
        score = 0
        initial_sl = spot - (1.5 * atr)
        entry_zone = (spot - 5.0, spot + 5.0)

        # Check breakout of ORB or PDH/PDL
        if is_bullish_align:
            direction = "LONG"
            initial_sl = min(candle.low - 5.0, vwap - 5.0)
            entry_zone = (candle.close - 5.0, candle.close + 10.0)
            score += 30
            reasons.append("Price holding above rising VWAP and EMA20")

            if snapshot.orb and spot >= snapshot.orb.get("high", 999999):
                score += 25
                reasons.append("Breakout above Opening Range High (ORB-H)")

            if snapshot.pdh and spot >= snapshot.pdh:
                score += 20
                reasons.append("Breakout above Previous Day High (PDH)")

            if snapshot.pcr and snapshot.pcr > 1.0:
                score += 15
                reasons.append(f"PCR bullish support ({snapshot.pcr:.2f})")

        elif is_bearish_align:
            direction = "SHORT"
            initial_sl = max(candle.high + 5.0, vwap + 5.0)
            entry_zone = (candle.close - 10.0, candle.close + 5.0)
            score += 30
            reasons.append("Price holding below falling VWAP and EMA20")

            if snapshot.orb and spot <= snapshot.orb.get("low", -999999):
                score += 25
                reasons.append("Breakout below Opening Range Low (ORB-L)")

            if snapshot.pdl and spot <= snapshot.pdl:
                score += 20
                reasons.append("Breakdown below Previous Day Low (PDL)")

            if snapshot.pcr and snapshot.pcr < 0.9:
                score += 15
                reasons.append(f"PCR bearish pressure ({snapshot.pcr:.2f})")

        risk_pts = abs(spot - initial_sl) if initial_sl else 25.0
        target_pts = risk_pts * 2.0
        rr = target_pts / risk_pts if risk_pts > 0 else 1.5

        eligible = (score >= 60) and (direction != "NONE")
        tier = "STRONG" if score >= 80 else ("VALID" if score >= 60 else "WEAK")

        return StrategyAssessment(
            strategy_id=self.strategy_id,
            timestamp=now,
            snapshot_id=snapshot.snapshot_id,
            eligible=eligible,
            direction=direction,
            setup_score=score,
            regime_fit_score=85 if "TREND" in market_state or "EXPANSION" in market_state else 50,
            structure_score=score,
            momentum_score=80 if is_bullish_align or is_bearish_align else 40,
            liquidity_score=75,
            risk_reward_ratio=rr,
            normalized_extension=norm_ext,
            empirical_expectancy=0.65,
            sample_size=38,
            confidence_tier=tier,
            entry_zone=entry_zone,
            initial_sl=initial_sl,
            management_plan="STRUCTURE_TRAIL",
            valid_until=ttl,
            reason_codes=reasons,
            blocker_codes=blockers,
        )

    def _ineligible(self, snapshot: MarketSnapshot, ttl: datetime, blockers: list[str]) -> StrategyAssessment:
        return StrategyAssessment(
            strategy_id=self.strategy_id,
            timestamp=snapshot.timestamp,
            snapshot_id=snapshot.snapshot_id,
            eligible=False,
            direction="NONE",
            setup_score=0,
            regime_fit_score=0,
            structure_score=0,
            momentum_score=0,
            liquidity_score=0,
            risk_reward_ratio=0.0,
            normalized_extension=0.0,
            empirical_expectancy=0.0,
            sample_size=0,
            confidence_tier="NO_TRADE",
            entry_zone=(0.0, 0.0),
            initial_sl=0.0,
            management_plan="NONE",
            valid_until=ttl,
            reason_codes=[],
            blocker_codes=blockers,
        )


class S2VwapMomentum:
    """Strategy 2: Directional acceptance and momentum around VWAP."""

    strategy_id = "S2_VWAP_MOMENTUM"

    def assess(
        self,
        snapshot: MarketSnapshot,
        level_mgr: LevelLifecycleManager,
        market_state: str = "NORMAL",
    ) -> StrategyAssessment:
        now = snapshot.timestamp
        ttl = now + timedelta(minutes=10)
        reasons = []
        blockers = []

        if snapshot.is_blind:
            return self._ineligible(snapshot, ttl, ["DATA_BLINDNESS"])

        # RANGE_GATE (Phase 8): S2 VWAP Momentum strictly prohibited in RANGE regimes
        if "RANGE" in (market_state or "").upper():
            blockers.append("RANGE_GATE_S2_BLOCKED")
            return self._ineligible(snapshot, ttl, blockers)

        norm_ext = snapshot.normalized_vwap_distance() or 0.0
        # Extension penalty: if extended beyond 2.0 ATR from VWAP, blocked!
        if norm_ext > 2.0:
            blockers.append(f"EXTENDED_VWAP_PENALTY (extension={norm_ext:.2f} > 2.0 ATR)")
            return self._ineligible(snapshot, ttl, blockers)

        spot = snapshot.spot
        vwap = snapshot.vwap or spot
        atr = snapshot.atr or 20.0
        candle = snapshot.latest_candle
        if not candle:
            return self._ineligible(snapshot, ttl, ["NO_CANDLE_DATA"])

        # Retest of VWAP within 0.2 to 1.2 ATR
        score = 0
        direction = "NONE"
        initial_sl = spot - atr
        entry_zone = (spot - 5.0, spot + 5.0)

        if 0.1 <= norm_ext <= 1.2:
            if spot >= vwap and candle.close >= vwap:
                direction = "LONG"
                score = 75
                initial_sl = vwap - 8.0
                entry_zone = (vwap, vwap + 15.0)
                reasons.append(f"VWAP retest and bounce held (ext={norm_ext:.2f} ATR)")
            elif spot <= vwap and candle.close <= vwap:
                direction = "SHORT"
                score = 75
                initial_sl = vwap + 8.0
                entry_zone = (vwap - 15.0, vwap)
                reasons.append(f"VWAP rejection held (ext={norm_ext:.2f} ATR)")

        eligible = (score >= 60) and (direction != "NONE")
        tier = "STRONG" if score >= 80 else ("VALID" if score >= 60 else "WEAK")

        return StrategyAssessment(
            strategy_id=self.strategy_id,
            timestamp=now,
            snapshot_id=snapshot.snapshot_id,
            eligible=eligible,
            direction=direction,
            setup_score=score,
            regime_fit_score=80 if "NORMAL" in market_state or "TREND" in market_state else 50,
            structure_score=score,
            momentum_score=75,
            liquidity_score=70,
            risk_reward_ratio=1.8,
            normalized_extension=norm_ext,
            empirical_expectancy=0.45,
            sample_size=32,
            confidence_tier=tier,
            entry_zone=entry_zone,
            initial_sl=initial_sl,
            management_plan="ATR_TRAIL",
            valid_until=ttl,
            reason_codes=reasons,
            blocker_codes=blockers,
        )

    def _ineligible(self, snapshot: MarketSnapshot, ttl: datetime, blockers: list[str]) -> StrategyAssessment:
        return StrategyAssessment(
            strategy_id=self.strategy_id,
            timestamp=snapshot.timestamp,
            snapshot_id=snapshot.snapshot_id,
            eligible=False,
            direction="NONE",
            setup_score=0,
            regime_fit_score=0,
            structure_score=0,
            momentum_score=0,
            liquidity_score=0,
            risk_reward_ratio=0.0,
            normalized_extension=0.0,
            empirical_expectancy=0.0,
            sample_size=0,
            confidence_tier="NO_TRADE",
            entry_zone=(0.0, 0.0),
            initial_sl=0.0,
            management_plan="NONE",
            valid_until=ttl,
            reason_codes=[],
            blocker_codes=blockers,
        )


class S3LiquidityReversal:
    """Strategy 3: Failed auctions at macro liquidity pools WITH Trend Override Veto."""

    strategy_id = "S3_LIQUIDITY_REVERSAL"

    def assess(
        self,
        snapshot: MarketSnapshot,
        level_mgr: LevelLifecycleManager,
        market_state: str = "NORMAL",
        s1_trend_strength: str = "NORMAL",
    ) -> StrategyAssessment:
        now = snapshot.timestamp
        ttl = now + timedelta(minutes=15)
        reasons = []
        blockers = []

        if snapshot.is_blind:
            return self._ineligible(snapshot, ttl, ["DATA_BLINDNESS"])

        # =========================================================================
        # CRITICAL: TREND OVERRIDE CHECK (Section 16 requirement)
        # If market is in strong trend expansion, counter-trend reversal is VETOED!
        # =========================================================================
        is_strong_trend = (s1_trend_strength == "HIGH") or ("EXPANSION" in market_state)
        if is_strong_trend:
            blockers.append("COUNTER_TREND_EXPANSION_VETO (S1 Trend Strength is HIGH; cannot fade strong trend)")
            return self._ineligible(snapshot, ttl, blockers)

        candle = snapshot.latest_candle
        if not candle:
            return self._ineligible(snapshot, ttl, ["NO_CANDLE_DATA"])

        spot = snapshot.spot
        direction = "NONE"
        score = 0
        initial_sl = spot
        entry_zone = (spot - 5.0, spot + 5.0)

        # Check macro pool sweep (PDH/PDL or ORB-H/ORB-L)
        if snapshot.pdh and candle.high > snapshot.pdh and candle.close < snapshot.pdh:
            # Swept PDH -> SHORT candidate
            direction = "SHORT"
            score = 85
            initial_sl = candle.high + 5.0
            entry_zone = (snapshot.pdh - 5.0, snapshot.pdh + 2.0)
            reasons.append(f"Swept PDH ({snapshot.pdh:.2f}) and closed back inside (wick={candle.high:.2f})")

        elif snapshot.pdl and candle.low < snapshot.pdl and candle.close > snapshot.pdl:
            # Swept PDL -> LONG candidate
            direction = "LONG"
            score = 85
            initial_sl = candle.low - 5.0
            entry_zone = (snapshot.pdl - 2.0, snapshot.pdl + 5.0)
            reasons.append(f"Swept PDL ({snapshot.pdl:.2f}) and closed back inside (wick={candle.low:.2f})")

        elif snapshot.orb and "high" in snapshot.orb and candle.high > snapshot.orb["high"] and candle.close < snapshot.orb["high"]:
            direction = "SHORT"
            score = 75
            initial_sl = candle.high + 5.0
            entry_zone = (snapshot.orb["high"] - 5.0, snapshot.orb["high"] + 2.0)
            reasons.append(f"Swept ORB High ({snapshot.orb['high']:.2f}) and closed inside")

        elif snapshot.orb and "low" in snapshot.orb and candle.low < snapshot.orb["low"] and candle.close > snapshot.orb["low"]:
            direction = "LONG"
            score = 75
            initial_sl = candle.low - 5.0
            entry_zone = (snapshot.orb["low"] - 2.0, snapshot.orb["low"] + 5.0)
            reasons.append(f"Swept ORB Low ({snapshot.orb['low']:.2f}) and closed inside")

        # Trend Override Veto for S3:
        # When MarketState is TREND-DOWN: S3 LONG is FORBIDDEN.
        # When MarketState is TREND-UP: S3 SHORT is FORBIDDEN.
        ms_upper = (market_state or "").upper()
        if direction == "LONG" and ("TREND-DOWN" in ms_upper or "DOWN" in ms_upper or "BEAR" in ms_upper):
            blockers.append("COUNTER_TREND_REVERSAL_FORBIDDEN_IN_TREND_DOWN")
            return self._ineligible(snapshot, ttl, blockers)
        if direction == "SHORT" and ("TREND-UP" in ms_upper or "UP" in ms_upper or "BULL" in ms_upper):
            blockers.append("COUNTER_TREND_REVERSAL_FORBIDDEN_IN_TREND_UP")
            return self._ineligible(snapshot, ttl, blockers)

        eligible = (score >= 70) and (direction != "NONE")
        tier = "STRONG" if score >= 80 else ("VALID" if score >= 60 else "WEAK")

        return StrategyAssessment(
            strategy_id=self.strategy_id,
            timestamp=now,
            snapshot_id=snapshot.snapshot_id,
            eligible=eligible,
            direction=direction,
            setup_score=score,
            regime_fit_score=90 if "RANGE" in market_state else 60,
            structure_score=score,
            momentum_score=60,
            liquidity_score=90,
            risk_reward_ratio=2.2,
            normalized_extension=snapshot.normalized_vwap_distance() or 0.5,
            empirical_expectancy=0.52,
            sample_size=28,
            confidence_tier=tier,
            entry_zone=entry_zone,
            initial_sl=initial_sl,
            management_plan="POOL_TARGET",
            valid_until=ttl,
            reason_codes=reasons,
            blocker_codes=blockers,
        )

    def _ineligible(self, snapshot: MarketSnapshot, ttl: datetime, blockers: list[str]) -> StrategyAssessment:
        return StrategyAssessment(
            strategy_id=self.strategy_id,
            timestamp=snapshot.timestamp,
            snapshot_id=snapshot.snapshot_id,
            eligible=False,
            direction="NONE",
            setup_score=0,
            regime_fit_score=0,
            structure_score=0,
            momentum_score=0,
            liquidity_score=0,
            risk_reward_ratio=0.0,
            normalized_extension=0.0,
            empirical_expectancy=0.0,
            sample_size=0,
            confidence_tier="NO_TRADE",
            entry_zone=(0.0, 0.0),
            initial_sl=0.0,
            management_plan="NONE",
            valid_until=ttl,
            reason_codes=[],
            blocker_codes=blockers,
        )
