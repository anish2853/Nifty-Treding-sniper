"""Level Lifecycle Management & Liquidity Map Engine (Phase 2).

Solves the Root Problem: "LIQUIDITY IS THE PRIMARY MARKET MAP".
Replaces flat, static level arrays with an explicit 4-tier hierarchy:
- TIER 1: MAJOR LIQUIDITY (PDH, PDL, major previous-session swing, major OI wall)
- TIER 2: SESSION LIQUIDITY (ORB high, ORB low, current session high/low)
- TIER 3: LIVE STRUCTURE (current 5m HH, HL, LH, LL)
- TIER 4: INTERNAL POI (OB, FVG, IFVG, Breaker, micro liquidity)

Enforces strict lifecycle state decay (CANDIDATE -> ACTIVE -> TESTED -> BROKEN -> CONSUMED -> FLIPPED -> EXPIRED).
Provides get_next_liquidity() for dynamic trend progression (e.g. 400-point moves).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from engines.candles import Candle

log = logging.getLogger(__name__)


class LevelTier(int, Enum):
    MAJOR = 1        # Tier 1: PDH, PDL, major previous-session swing, strong HTF swing, major OI wall, equal highs/lows
    SESSION = 2      # Tier 2: ORB high, ORB low, current session high/low, major session swing
    STRUCTURE = 3    # Tier 3: Live 5m structure swings (HH, HL, LH, LL)
    INTERNAL = 4     # Tier 4: Internal POIs (OB, FVG, IFVG, Breaker, micro liquidity, local sweep)


class LevelStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    UNTESTED = "ACTIVE"      # backwards-compat alias
    TESTED = "TESTED"
    REJECTED = "REJECTED"
    BROKEN = "BROKEN"
    ACCEPTED = "ACCEPTED"
    CONSUMED = "CONSUMED"
    FLIPPED = "FLIPPED"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"


class LevelRole(str, Enum):
    RESISTANCE = "RESISTANCE"
    SUPPORT = "SUPPORT"
    BOTH = "BOTH"


@dataclass
class LifecycleLevel:
    """A price level with full lifecycle state, hierarchy tier, and memory."""
    level_id: str
    price: float
    source: str
    role: LevelRole
    created_at: datetime
    tier: LevelTier = LevelTier.MAJOR
    type: str = "MAJOR"
    price_high: float | None = None
    price_low: float | None = None
    timeframe: str = "5m"
    strength: str = "NORMAL"         # "STRONG" | "NORMAL" | "WEAK"
    confluence: list[str] = field(default_factory=list)
    last_tested: datetime | None = None
    test_count: int = 0
    status: LevelStatus = LevelStatus.ACTIVE
    direction: LevelRole | None = None
    distance_from_spot: float = 0.0
    broken_at: datetime | None = None
    consumed_at: datetime | None = None
    invalidated_at: datetime | None = None
    expired_at: datetime | None = None
    expiry: datetime | None = None
    parent_level_id: str | None = None
    consecutive_closes_beyond: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.price_high is None:
            self.price_high = self.price
        if self.price_low is None:
            self.price_low = self.price
        if self.direction is None:
            self.direction = self.role
        if self.invalidated_at is None and self.expired_at is not None:
            self.invalidated_at = self.expired_at
        if self.expiry is None and self.expired_at is not None:
            self.expiry = self.expired_at

    @property
    def is_consumed(self) -> bool:
        return self.status == LevelStatus.CONSUMED

    @property
    def is_broken(self) -> bool:
        return self.status in (LevelStatus.BROKEN, LevelStatus.ACCEPTED)

    @property
    def is_expired(self) -> bool:
        return self.status == LevelStatus.EXPIRED

    @property
    def is_tradeable(self) -> bool:
        """Once BROKEN, CONSUMED, EXPIRED, or INVALID, a level is not tradeable."""
        return (
            self.status in (LevelStatus.ACTIVE, LevelStatus.TESTED, LevelStatus.FLIPPED)
            and self.test_count < 3
            and not self.is_consumed
            and not self.is_broken
            and not self.is_expired
        )

    @property
    def can_trigger_reversal(self) -> bool:
        """A level that is CONSUMED, BROKEN, or EXPIRED cannot trigger fade/reversal setups."""
        return self.is_tradeable


@dataclass(frozen=True)
class LiquidityMap:
    """Canonical representation of the market's current liquidity topography."""
    spot: float
    timestamp: datetime
    nearest_upside_liquidity: LifecycleLevel | None
    nearest_downside_liquidity: LifecycleLevel | None
    next_major_upside: LifecycleLevel | None
    next_major_downside: LifecycleLevel | None
    room_up: float
    room_down: float
    liquidity_confidence: float      # 0.0 to 1.0
    active_pool_count: int
    top_levels: list[LifecycleLevel]  # Clean top 3-5 levels for dashboard display


class LevelLifecycleManager:
    """Manages hierarchical liquidity pools, dynamic decay, and next-liquidity targets."""

    def __init__(self, tolerance_pts: float = 5.0, max_tests_before_consumed: int = 3):
        self.tolerance_pts = tolerance_pts
        self.max_tests = max_tests_before_consumed
        self.levels: dict[str, LifecycleLevel] = {}

    def register_level(
        self,
        level_id: str,
        price: float,
        source: str,
        role: LevelRole = LevelRole.RESISTANCE,
        timestamp: datetime | None = None,
        strength: str = "NORMAL",
        confluence: list[str] | None = None,
        tier: LevelTier | int = LevelTier.MAJOR,
        level_type: str | None = None,
        price_high: float | None = None,
        price_low: float | None = None,
        parent_level_id: str | None = None,
        status: LevelStatus = LevelStatus.ACTIVE,
        metadata: dict[str, Any] | None = None,
    ) -> LifecycleLevel:
        now = timestamp or datetime.now()
        tier_enum = LevelTier(tier) if isinstance(tier, int) else tier
        inferred_type = level_type or source or "LEVEL"

        if level_id in self.levels:
            lvl = self.levels[level_id]
            # If already broken, consumed, or expired, do not overwrite state
            if lvl.status in (LevelStatus.ACTIVE, LevelStatus.TESTED):
                lvl.price = price
                lvl.price_high = price_high if price_high is not None else price
                lvl.price_low = price_low if price_low is not None else price
                lvl.strength = strength
                lvl.tier = tier_enum
            return lvl

        lvl = LifecycleLevel(
            level_id=level_id,
            price=price,
            source=source,
            role=role,
            created_at=now,
            tier=tier_enum,
            type=inferred_type,
            price_high=price_high if price_high is not None else price,
            price_low=price_low if price_low is not None else price,
            strength=strength,
            confluence=confluence or [],
            status=status,
            parent_level_id=parent_level_id,
            metadata=metadata or {},
        )
        self.levels[level_id] = lvl
        return lvl

    def update(self, snapshot, candle: Candle | None = None) -> list[LifecycleLevel]:
        """Update lifecycle status, decay, and live structure of all registered levels."""
        candle = candle or (snapshot.candles[-1] if snapshot.candles else None)
        if not candle:
            return list(self.levels.values())

        now = snapshot.timestamp
        atr = getattr(snapshot, "atr", None) or 20.0
        spot = snapshot.spot

        # -------------------------------------------------------------
        # 1. Register TIER 1 Major Structural Reference Levels
        # -------------------------------------------------------------
        if getattr(snapshot, "pdh", None) is not None:
            self.register_level(
                "PDH", snapshot.pdh, "PDH", LevelRole.RESISTANCE, now,
                strength="STRONG", tier=LevelTier.MAJOR, level_type="PDH"
            )
        if getattr(snapshot, "pdl", None) is not None:
            self.register_level(
                "PDL", snapshot.pdl, "PDL", LevelRole.SUPPORT, now,
                strength="STRONG", tier=LevelTier.MAJOR, level_type="PDL"
            )
        if getattr(snapshot, "eq", None) is not None:
            role = LevelRole.SUPPORT if spot >= snapshot.eq else LevelRole.RESISTANCE
            self.register_level(
                "EQ", snapshot.eq, "EQ", role, now,
                strength="STRONG", tier=LevelTier.MAJOR, level_type="EQ"
            )

        # -------------------------------------------------------------
        # 2. Register TIER 2 Session Reference Levels
        # -------------------------------------------------------------
        orb = getattr(snapshot, "orb", None)
        if orb:
            if "high" in snapshot.orb:
                self.register_level(
                    "ORB-H", snapshot.orb["high"], "ORB-H", LevelRole.RESISTANCE, now,
                    tier=LevelTier.SESSION, level_type="ORB_HIGH"
                )
            if "low" in snapshot.orb:
                self.register_level(
                    "ORB-L", snapshot.orb["low"], "ORB-L", LevelRole.SUPPORT, now,
                    tier=LevelTier.SESSION, level_type="ORB_LOW"
                )

        snap_candles = getattr(snapshot, "candles", None) or ()
        if snap_candles:
            session_h = max(c.high for c in snap_candles)
            session_l = min(c.low for c in snap_candles)
            self.register_level(
                "SESSION-H", session_h, "SESSION", LevelRole.RESISTANCE, now,
                tier=LevelTier.SESSION, level_type="SESSION_HIGH"
            )
            self.register_level(
                "SESSION-L", session_l, "SESSION", LevelRole.SUPPORT, now,
                tier=LevelTier.SESSION, level_type="SESSION_LOW"
            )

        # -------------------------------------------------------------
        # 3. Register TIER 3 Live 5m Structure Swings
        # -------------------------------------------------------------
        if len(snap_candles) >= 3:
            recent_candles = snap_candles[-10:]
            for i in range(1, len(recent_candles) - 1):
                prev_c, curr_c, next_c = recent_candles[i - 1], recent_candles[i], recent_candles[i + 1]
                # Swing High (LH or HH)
                if curr_c.high > prev_c.high and curr_c.high >= next_c.high:
                    sw_id = f"STRUC-H-{round(curr_c.high, 1)}"
                    self.register_level(
                        sw_id, curr_c.high, "5M_SWING", LevelRole.RESISTANCE, curr_c.end,
                        tier=LevelTier.STRUCTURE, level_type="SWING_HIGH", strength="NORMAL"
                    )
                # Swing Low (HL or LL)
                if curr_c.low < prev_c.low and curr_c.low <= next_c.low:
                    sw_id = f"STRUC-L-{round(curr_c.low, 1)}"
                    self.register_level(
                        sw_id, curr_c.low, "5M_SWING", LevelRole.SUPPORT, curr_c.end,
                        tier=LevelTier.STRUCTURE, level_type="SWING_LOW", strength="NORMAL"
                    )

        # -------------------------------------------------------------
        # 4. Lifecycle State Transitions & Decay
        # -------------------------------------------------------------
        for lvl in list(self.levels.values()):
            lvl.distance_from_spot = abs(candle.close - lvl.price)

            if lvl.status in (LevelStatus.EXPIRED, LevelStatus.INVALID):
                continue

            # Decay rule for Tier 4 (Internal POIs):
            # Rapid decay: If older than 60 mins without being tested or > 60 pts away -> EXPIRE
            if lvl.tier == LevelTier.INTERNAL:
                age_mins = (candle.end - lvl.created_at).total_seconds() / 60.0
                if (age_mins > 60.0 and lvl.test_count == 0) or lvl.distance_from_spot > 60.0:
                    lvl.status = LevelStatus.EXPIRED
                    lvl.expired_at = candle.end
                    lvl.invalidated_at = candle.end
                    continue

            # Resistance transitions
            if lvl.role == LevelRole.RESISTANCE:
                breakout_dist = candle.close - lvl.price
                if lvl.status in (LevelStatus.BROKEN, LevelStatus.ACCEPTED):
                    # Distance expiry: broken by > 2.5 ATR or 60+ points
                    if breakout_dist >= 60.0 or breakout_dist >= (2.5 * atr):
                        lvl.status = LevelStatus.EXPIRED
                        lvl.expired_at = candle.end
                        lvl.invalidated_at = candle.end
                        log.info("Level %s EXPIRED (broken by %.2f pts)", lvl.level_id, breakout_dist)
                        continue
                    # Retest from above -> FLIP to SUPPORT
                    elif candle.low <= (lvl.price + self.tolerance_pts) and candle.close >= lvl.price:
                        lvl.role = LevelRole.SUPPORT
                        lvl.direction = LevelRole.SUPPORT
                        lvl.status = LevelStatus.FLIPPED
                        lvl.test_count = 0
                        lvl.consecutive_closes_beyond = 0
                        log.info("Level %s FLIPPED from RESISTANCE to SUPPORT @ %.2f", lvl.level_id, lvl.price)
                    elif candle.close < (lvl.price - self.tolerance_pts):
                        # Closed back inside -> return to ACTIVE
                        lvl.status = LevelStatus.ACTIVE
                        lvl.consecutive_closes_beyond = 0
                    elif candle.close > (lvl.price + self.tolerance_pts):
                        lvl.consecutive_closes_beyond += 1
                        if lvl.consecutive_closes_beyond >= 2:
                            lvl.status = LevelStatus.ACCEPTED
                else:
                    # Currently ACTIVE, TESTED, or FLIPPED Resistance
                    if candle.close > (lvl.price + self.tolerance_pts):
                        lvl.consecutive_closes_beyond = 1
                        if breakout_dist >= 60.0 or breakout_dist >= (2.5 * atr):
                            lvl.status = LevelStatus.EXPIRED
                            lvl.expired_at = candle.end
                            lvl.invalidated_at = candle.end
                            log.info("Level %s EXPIRED immediately (broken by %.2f pts)", lvl.level_id, breakout_dist)
                        else:
                            lvl.status = LevelStatus.BROKEN
                            lvl.broken_at = candle.end
                    elif abs(candle.high - lvl.price) <= self.tolerance_pts and candle.close <= lvl.price:
                        # Resistance held test
                        lvl.consecutive_closes_beyond = 0
                        lvl.last_tested = candle.end
                        lvl.test_count += 1
                        if lvl.test_count >= self.max_tests:
                            lvl.status = LevelStatus.CONSUMED
                            lvl.consumed_at = candle.end
                            log.info("Level %s CONSUMED after %d tests", lvl.level_id, lvl.test_count)
                        else:
                            lvl.status = LevelStatus.TESTED

            # Support transitions
            elif lvl.role == LevelRole.SUPPORT:
                breakout_dist = lvl.price - candle.close
                if lvl.status in (LevelStatus.BROKEN, LevelStatus.ACCEPTED):
                    # Distance expiry: broken by > 2.5 ATR or 60+ points
                    if breakout_dist >= 60.0 or breakout_dist >= (2.5 * atr):
                        lvl.status = LevelStatus.EXPIRED
                        lvl.expired_at = candle.end
                        lvl.invalidated_at = candle.end
                        log.info("Level %s EXPIRED (broken by %.2f pts)", lvl.level_id, breakout_dist)
                        continue
                    # Retest from below -> FLIP to RESISTANCE
                    elif candle.high >= (lvl.price - self.tolerance_pts) and candle.close <= lvl.price:
                        lvl.role = LevelRole.RESISTANCE
                        lvl.direction = LevelRole.RESISTANCE
                        lvl.status = LevelStatus.FLIPPED
                        lvl.test_count = 0
                        lvl.consecutive_closes_beyond = 0
                        log.info("Level %s FLIPPED from SUPPORT to RESISTANCE @ %.2f", lvl.level_id, lvl.price)
                    elif candle.close > (lvl.price + self.tolerance_pts):
                        # Closed back inside -> return to ACTIVE
                        lvl.status = LevelStatus.ACTIVE
                        lvl.consecutive_closes_beyond = 0
                    elif candle.close < (lvl.price - self.tolerance_pts):
                        lvl.consecutive_closes_beyond += 1
                        if lvl.consecutive_closes_beyond >= 2:
                            lvl.status = LevelStatus.ACCEPTED
                else:
                    # Currently ACTIVE, TESTED, or FLIPPED Support
                    if candle.close < (lvl.price - self.tolerance_pts):
                        lvl.consecutive_closes_beyond = 1
                        if breakout_dist >= 60.0 or breakout_dist >= (2.5 * atr):
                            lvl.status = LevelStatus.EXPIRED
                            lvl.expired_at = candle.end
                            lvl.invalidated_at = candle.end
                            log.info("Level %s EXPIRED immediately (broken by %.2f pts)", lvl.level_id, breakout_dist)
                        else:
                            lvl.status = LevelStatus.BROKEN
                            lvl.broken_at = candle.end
                    elif abs(candle.low - lvl.price) <= self.tolerance_pts and candle.close >= lvl.price:
                        # Support held test
                        lvl.consecutive_closes_beyond = 0
                        lvl.last_tested = candle.end
                        lvl.test_count += 1
                        if lvl.test_count >= self.max_tests:
                            lvl.status = LevelStatus.CONSUMED
                            lvl.consumed_at = candle.end
                            log.info("Level %s CONSUMED after %d tests", lvl.level_id, lvl.test_count)
                        else:
                            lvl.status = LevelStatus.TESTED

        return list(self.levels.values())

    # -------------------------------------------------------------------------
    # NEXT LIQUIDITY ENGINE (Section 6 & 7)
    # -------------------------------------------------------------------------
    def get_next_liquidity(
        self,
        spot: float,
        market_state: str = "NORMAL",
        atr: float = 20.0,
    ) -> LiquidityMap:
        """Computes authoritative nearest/major liquidity destinations and room."""
        # 1. Actionable pools only: non-broken, non-consumed, non-expired
        actionable = [lvl for lvl in self.levels.values() if lvl.is_tradeable]

        upside_candidates = [lvl for lvl in actionable if lvl.price > spot]
        downside_candidates = [lvl for lvl in actionable if lvl.price < spot]

        # 2. Ranking function based on Tier, Strength, Freshness, Distance, Regime
        def rank_pool(lvl: LifecycleLevel, is_downside: bool) -> float:
            tier_scores = {
                LevelTier.MAJOR: 300.0,
                LevelTier.SESSION: 250.0,
                LevelTier.STRUCTURE: 180.0,
                LevelTier.INTERNAL: 50.0,
            }
            strength_scores = {"STRONG": 40.0, "NORMAL": 20.0, "WEAK": 0.0}

            score = tier_scores.get(lvl.tier, 100.0) + strength_scores.get(lvl.strength, 20.0)

            # Freshness bonus: untouched pools have maximum uncollected liquidity
            if lvl.test_count == 0:
                score += 30.0
            elif lvl.test_count == 1:
                score += 10.0
            else:
                score -= 20.0

            # Distance penalty along the path
            dist = abs(lvl.price - spot)
            score -= (dist * 1.5)

            # Regime alignment bonus
            if "DOWN" in market_state.upper() and is_downside:
                score += 30.0
            elif "UP" in market_state.upper() and not is_downside:
                score += 30.0

            return score

        # Rank pools
        ranked_upside = sorted(upside_candidates, key=lambda l: rank_pool(l, False), reverse=True)
        ranked_downside = sorted(downside_candidates, key=lambda l: rank_pool(l, True), reverse=True)

        nearest_up = ranked_upside[0] if ranked_upside else None
        nearest_down = ranked_downside[0] if ranked_downside else None

        major_up_list = [l for l in ranked_upside if l.tier in (LevelTier.MAJOR, LevelTier.SESSION)]
        major_down_list = [l for l in ranked_downside if l.tier in (LevelTier.MAJOR, LevelTier.SESSION)]

        next_major_up = major_up_list[0] if major_up_list else nearest_up
        next_major_down = major_down_list[0] if major_down_list else nearest_down

        room_up = (nearest_up.price - spot) if nearest_up else 999.0
        room_down = (spot - nearest_down.price) if nearest_down else 999.0

        # Confidence: higher when major pools exist in runway
        confidence = 0.5
        if next_major_down if "DOWN" in market_state.upper() else next_major_up:
            confidence = 0.85
        elif nearest_down or nearest_up:
            confidence = 0.70

        # Top levels for dashboard display
        top_disp = self.get_dashboard_display_levels(spot, market_state=market_state)

        return LiquidityMap(
            spot=spot,
            timestamp=datetime.now(),
            nearest_upside_liquidity=nearest_up,
            nearest_downside_liquidity=nearest_down,
            next_major_upside=next_major_up,
            next_major_downside=next_major_down,
            room_up=max(0.0, room_up),
            room_down=max(0.0, room_down),
            liquidity_confidence=confidence,
            active_pool_count=len(actionable),
            top_levels=top_disp,
        )

    # -------------------------------------------------------------------------
    # DASHBOARD DISPLAY FILTER (Section 8 & 12)
    # -------------------------------------------------------------------------
    def get_dashboard_display_levels(
        self,
        spot: float,
        market_state: str = "NORMAL",
        max_major: int = 3,
    ) -> list[LifecycleLevel]:
        """Filters down to strictly: top major/session levels + live structure + 1 active POI."""
        display_levels: list[LifecycleLevel] = []

        # 1. Top 3 Major/Session Liquidity levels closest to spot
        major_session = [
            lvl for lvl in self.levels.values()
            if lvl.tier in (LevelTier.MAJOR, LevelTier.SESSION)
            and lvl.status in (LevelStatus.ACTIVE, LevelStatus.TESTED, LevelStatus.FLIPPED, LevelStatus.BROKEN)
        ]
        sorted_major = sorted(major_session, key=lambda l: abs(l.price - spot))
        display_levels.extend(sorted_major[:max_major])

        # 2. Current Live 5m Structure (closest HL and LH)
        structures = [
            lvl for lvl in self.levels.values()
            if lvl.tier == LevelTier.STRUCTURE and lvl.status in (LevelStatus.ACTIVE, LevelStatus.TESTED)
        ]
        struc_highs = [s for s in structures if s.price >= spot]
        struc_lows = [s for s in structures if s.price <= spot]
        if struc_highs:
            display_levels.append(min(struc_highs, key=lambda s: s.price))
        if struc_lows:
            display_levels.append(max(struc_lows, key=lambda s: s.price))

        # 3. At most 1 strategy-relevant POI (Tier 4) near price
        active_pois = [
            lvl for lvl in self.levels.values()
            if lvl.tier == LevelTier.INTERNAL and lvl.status in (LevelStatus.ACTIVE, LevelStatus.TESTED)
        ]
        if active_pois:
            closest_poi = min(active_pois, key=lambda p: abs(p.price - spot))
            if abs(closest_poi.price - spot) <= 30.0:
                display_levels.append(closest_poi)

        return display_levels

    def get_actionable_levels(self) -> list[LifecycleLevel]:
        return [lvl for lvl in self.levels.values() if lvl.is_tradeable]

    def get_resistance(self, spot: float) -> list[LifecycleLevel]:
        return [lvl for lvl in self.get_actionable_levels() if lvl.role == LevelRole.RESISTANCE and lvl.price >= spot]

    def get_support(self, spot: float) -> list[LifecycleLevel]:
        return [lvl for lvl in self.get_actionable_levels() if lvl.role == LevelRole.SUPPORT and lvl.price <= spot]
