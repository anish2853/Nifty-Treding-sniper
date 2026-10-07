"""Replay scoring renormalization: OI and volume components EXCLUDED from denominator.

Price-only mode (replay always):
- Available: level_break, confirmation, regime, time_window
- OI (+20) and volume (+15) never included in replay (EXCLUDED from max denominator)
- Max available = W_LEVEL_BREAK + W_CONFIRMATION + W_REGIME_VIX + W_TIME_WINDOW
- Fire threshold = 80% of max available (not 80 absolute)
- Example: max 65 pts → threshold 52/65 to fire
- Report: "MODE: PRICE-ONLY VARIANT | components: level, confirm, regime, time | threshold 52/65"

Details:
- W_LEVEL_BREAK: 20 (price only - candle close above ORB/PDH)
- W_CONFIRMATION: 20 (price only - next candle also closes above level)
- W_REGIME_VIX: 15 (price only - regime allows direction, VIX stable, not event day)
- W_TIME_WINDOW: 10 (price only - 09:30-11:00 window)
- W_OI_CONFIRMATION: 20 (EXCLUDED - no historical OI snapshots in replay)
- W_VOLUME: 15 (EXCLUDED - no volume data in replay)
"""
import settings


def get_price_only_info():
    """Return max available points and 80% threshold for price-only replay.
    
    Returns:
        dict with keys:
            - max_points: 65 (sum of available components)
            - threshold: 52 (80% of 65)
            - components: "level, confirm, regime, time"
            - mode: "PRICE-ONLY VARIANT"
            - label: "52/65"
            - excluded: "OI, volume"
    """
    # In replay mode, OI_CONFIRMATION and VOLUME are never available
    max_available = (
        settings.W_LEVEL_BREAK +       # 20: price only
        settings.W_CONFIRMATION +       # 20: price only
        settings.W_REGIME_VIX +         # 15: price only
        settings.W_TIME_WINDOW          # 10: price only
        # EXCLUDED: W_OI_CONFIRMATION (20) - no historical chain snapshots
        # EXCLUDED: W_VOLUME (15) - no volume data available in replay
    )
    threshold = int(max_available * 0.80)
    
    return {
        "max_points": max_available,
        "threshold": threshold,
        "components": "level_break, confirmation, regime_vix, time_window",
        "excluded": "oi_confirmation, volume",
        "mode": "PRICE-ONLY VARIANT",
        "label": f"{threshold}/{max_available}",
    }


def should_fire_price_only(score: int) -> bool:
    """Check if a replay signal meets the 80% price-only threshold.
    
    In replay mode with no historical OI snapshots or volume data,
    we renormalize: 80% of 65 (available points) = 52 threshold.
    
    Args:
        score: Raw score from entry engine (0-100 theoretical max)
    
    Returns:
        True if score >= 52 (threshold for price-only mode)
    """
    info = get_price_only_info()
    return score >= info["threshold"]


def get_price_only_summary() -> str:
    """Human-readable summary for all replay reports."""
    info = get_price_only_info()
    return (
        f"MODE: {info['mode']} | "
        f"components: {info['components']} | "
        f"excluded: {info['excluded']} | "
        f"threshold: {info['label']}"
    )
