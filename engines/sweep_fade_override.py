"""SWEEP-FADE state override: allow sweep-fade entries at strong support/resistance
in ANY day state when setup coincides with liquidity confluences (PDH/PDL, OI walls,
divergence). Trend exhaustion at strong walls is where traps form.

Gate: STATE-OVERRIDE (journaled separately for win-rate tracking)
"""
import logging
from dataclasses import dataclass

import settings

log = logging.getLogger(__name__)


@dataclass
class SweepFadeOverride:
    """Check if a sweep-fade setup qualifies for state override."""
    triggered: bool = False
    reason: str = ""
    confluence_score: int = 0  # 0-3: none, weak, medium, strong


def is_strong_level(level: float, pdh: float | None, pdl: float | None,
                   oi_wall_strikes: list[float], spot: float) -> bool:
    """Level is strong if it aligns with PDH, PDL, or OI wall within tolerance."""
    tol = 20  # 20 pts tolerance for confluence
    
    confluences = 0
    if pdh and abs(level - pdh) < tol:
        confluences += 1
    if pdl and abs(level - pdl) < tol:
        confluences += 1
    for wall_strike in oi_wall_strikes:
        if abs(level - wall_strike) < tol * 2:  # OI walls are looser
            confluences += 1
    
    return confluences >= 2  # Strong = 2+ confluences


def check_sweep_fade_override(
    direction: str,  # LONG | SHORT
    level: float,  # fade level (support/resistance)
    candle_close: float,
    pdh: float | None,
    pdl: float | None,
    oi_wall_strikes: list[float],
    divergence_present: bool = False,
    day_state: str | None = None,
) -> SweepFadeOverride:
    """
    Determine if a sweep-fade at this level bypasses day-state restrictions.
    
    Returns:
        SweepFadeOverride with triggered=True if override applies, reason string,
        and confluence_score (0-3) for metrics.
    """
    
    # No-op if day state would allow it anyway (TREND)
    if day_state and "TREND" in day_state:
        return SweepFadeOverride(triggered=False, reason="day state allows SWEEP-FADE")
    
    # Check if level is strong confluence
    strong = is_strong_level(level, pdh, pdl, oi_wall_strikes, candle_close)
    
    if not strong:
        return SweepFadeOverride(
            triggered=False,
            reason="level not a strong confluence (PDH/PDL/OI-wall)",
            confluence_score=0
        )
    
    # Strong level + divergence = high-confidence trap setup
    if divergence_present:
        reason = (
            f"STATE-OVERRIDE: sweep-fade at strong level + divergence "
            f"({direction} fade {level:.0f}). Trap setup. "
            f"Trade in any day state."
        )
        return SweepFadeOverride(
            triggered=True,
            reason=reason,
            confluence_score=3
        )
    
    # Strong level alone = moderate trap confidence
    reason = (
        f"STATE-OVERRIDE: sweep-fade at strong confluence "
        f"({direction} fade {level:.0f}). Trend exhaustion trap. "
        f"Trade allowed."
    )
    return SweepFadeOverride(
        triggered=True,
        reason=reason,
        confluence_score=2
    )
