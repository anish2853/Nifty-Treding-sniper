"""Module 2 - regime classifier. Pure logic, no I/O, so it is trivially unit-testable
and reusable by the Phase B entry engine.

Interpretations the spec leaves open (documented in README, all in settings.py):
- Gaps in the 0.3%-0.6% band: no restriction is specified for them, so they are
  treated as NORMAL with a note.
- LOW-CONFIDENCE is evaluated only when the gap has a definite direction (|gap| beyond
  the 0.3% NORMAL band) - a 0.05% wiggle against a -2% US close is noise, not a signal.
"""
import logging
from dataclasses import dataclass, field
from enum import Enum

import settings

log = logging.getLogger(__name__)


class Regime(str, Enum):
    NORMAL = "NORMAL"
    GAP_UP_EXTENSION = "GAP-UP-EXTENSION"
    GAP_DOWN = "GAP-DOWN"
    NO_TRADE_DAY = "NO-TRADE-DAY"


@dataclass
class RegimeState:
    regime: Regime
    gap_pct: float | None = None
    us_direction: int = 0                # +1 US closed up, -1 down, 0 unknown
    low_confidence: bool = False         # gap direction opposite to US close -> suppress signals
    pcr: float | None = None
    pcr_tilt: str = "UNKNOWN"            # BULLISH / BEARISH / NEUTRAL / UNKNOWN
    mean_reversion: bool = False         # PCR < 0.7 or > 1.4 -> suppress breakout entries
    vix: float | None = None
    vix_prev_close: float | None = None
    vix_spike: bool = False
    event_day: bool = False
    notes: list = field(default_factory=list)

    @classmethod
    def from_reasons(cls, data: dict) -> "RegimeState":
        """Rebuild a state from its reasons() dict - used after a watchdog crash to
        recover the day's regime from the journal instead of mis-classifying."""
        return cls(
            regime=Regime(data.get("regime", Regime.NORMAL.value)),
            gap_pct=data.get("gap_pct"),
            us_direction=data.get("us_direction") or 0,
            low_confidence=bool(data.get("low_confidence")),
            pcr=data.get("pcr"),
            pcr_tilt=data.get("pcr_tilt", "UNKNOWN"),
            mean_reversion=bool(data.get("mean_reversion")),
            vix=data.get("vix"),
            vix_prev_close=data.get("vix_prev_close"),
            vix_spike=bool(data.get("vix_spike")),
            event_day=bool(data.get("event_day")),
            notes=list(data.get("notes", [])))

    def reasons(self) -> dict:
        return {
            "regime": self.regime.value,
            "gap_pct": self.gap_pct,
            "us_direction": self.us_direction,
            "low_confidence": self.low_confidence,
            "pcr": self.pcr,
            "pcr_tilt": self.pcr_tilt,
            "mean_reversion": self.mean_reversion,
            "vix": self.vix,
            "vix_prev_close": self.vix_prev_close,
            "vix_spike": self.vix_spike,
            "event_day": self.event_day,
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        gap = f"{self.gap_pct:+.2%}" if self.gap_pct is not None else "MISSING"
        pcr = f"{self.pcr:.3f}" if self.pcr is not None else "MISSING"
        vix = f"{self.vix:.2f}" if self.vix is not None else "MISSING"
        flags = [name for enabled, name in (
            (self.low_confidence, "LOW-CONFIDENCE"),
            (self.mean_reversion, "MEAN-REVERSION"),
            (self.vix_spike, "VIX-SPIKE")) if enabled]
        flag_txt = (" | " + ", ".join(flags)) if flags else ""
        return f"{self.regime.value} | gap {gap} | PCR {pcr} ({self.pcr_tilt}) | VIX {vix}{flag_txt}"


class RegimeClassifier:
    """Stateless methods; the caller owns one RegimeState per trading day."""

    def __init__(self, holiday_dates=None, event_dates=None):
        self.holiday_dates = holiday_dates or set()
        self.event_dates = event_dates or set()

    def is_event_day(self, iso_date: str) -> bool:
        return iso_date in self.event_dates

    # -- 09:15 classification from the first spot print ----------------------
    def classify_at_open(self, spot, pdc, us_direction: int, event_day: bool) -> RegimeState:
        """INVARIANT (Prompt-3 fix #3): `spot` here is ALWAYS the actual underlying
        value from the option chain vs the previous-day close - never the GIFT Nifty
        level. GIFT only feeds the clearly-labeled PRELIMINARY verdict at 08:45."""
        state = RegimeState(regime=Regime.NORMAL, us_direction=us_direction, event_day=event_day)
        if event_day:
            state.regime = Regime.NO_TRADE_DAY
            state.notes.append("date in EVENT_CALENDAR -> NO-TRADE DAY (silent, journal it)")
            return state
        if spot is None or not pdc:
            state.notes.append("spot or PDC missing -> classification deferred")
            return state
        gap = spot / pdc - 1
        state.gap_pct = gap
        if gap > settings.GAP_RESTRICTION_ABS:
            state.regime = Regime.GAP_UP_EXTENSION
            state.notes.append("gap > +0.6% -> no fresh ORB longs")
        elif gap < -settings.GAP_RESTRICTION_ABS:
            state.regime = Regime.GAP_DOWN
            state.notes.append("gap < -0.6% -> no counter-trend longs")
        else:
            state.regime = Regime.NORMAL
            if abs(gap) > settings.GAP_NORMAL_ABS:
                state.notes.append("gap in 0.3%-0.6% band: spec defines no restriction "
                                   "there -> NORMAL")
        if abs(gap) > settings.GAP_NORMAL_ABS and us_direction != 0:
            if (gap > 0 and us_direction < 0) or (gap < 0 and us_direction > 0):
                state.low_confidence = True
                state.notes.append("gap direction opposite to US close -> LOW-CONFIDENCE "
                                   "(suppress signals)")
        return state

    # -- PCR context (applied once chain snapshots start) ---------------------
    def apply_pcr(self, state: RegimeState, pcr) -> bool:
        """Set PCR tilt / mean-reversion flag. Returns True when either changed."""
        if pcr is None:
            return False
        if pcr > settings.PCR_MEAN_REVERSION_ABOVE:
            tilt, mean_rev = "BULLISH", True
            note = (f"PCR {pcr:.3f} > {settings.PCR_MEAN_REVERSION_ABOVE:.2f} -> "
                    f"mean-reversion regime (breakout entries suppressed)")
        elif pcr < settings.PCR_MEAN_REVERSION_BELOW:
            tilt, mean_rev = "BEARISH", True
            note = (f"PCR {pcr:.3f} < {settings.PCR_MEAN_REVERSION_BELOW:.2f} -> "
                    f"mean-reversion regime (breakout entries suppressed)")
        elif pcr > settings.PCR_BULLISH_ABOVE:
            tilt, mean_rev = "BULLISH", False
            note = f"PCR {pcr:.3f} > {settings.PCR_BULLISH_ABOVE:.2f} -> bullish tilt"
        elif pcr < settings.PCR_BEARISH_BELOW:
            tilt, mean_rev = "BEARISH", False
            note = f"PCR {pcr:.3f} < {settings.PCR_BEARISH_BELOW:.2f} -> bearish tilt"
        else:
            tilt, mean_rev = "NEUTRAL", False
            note = f"PCR {pcr:.3f} -> neutral"
        changed = (tilt != state.pcr_tilt) or (mean_rev != state.mean_reversion)
        if changed:
            state.notes.append(note)
        state.pcr = pcr
        state.pcr_tilt = tilt
        state.mean_reversion = mean_rev
        return changed

    # -- intraday VIX spike (Module 2 regime-change flag) ----------------------
    def apply_vix(self, state: RegimeState, vix, vix_prev_close=None) -> bool:
        """Returns True when the spike flag toggled (onset or offset)."""
        prev = vix_prev_close if vix_prev_close is not None else state.vix_prev_close
        if vix is None or not prev:
            return False
        state.vix = vix
        state.vix_prev_close = prev
        spike = (vix / prev - 1) > settings.VIX_SPIKE_PCT
        changed = spike != state.vix_spike
        state.vix_spike = spike
        if spike and changed:
            state.notes.append(
                f"India VIX {vix:.2f} is >8% above prev close {prev:.2f} -> regime-change flag")
        return changed


def expected_gap_verdict(gap_pts, pdc, us_direction: int, event_day: bool) -> RegimeState:
    """Module 1's preliminary verdict: same gap bands as Module 2 applied to the
    expected gap (GIFT Nifty - PDC). Final classification happens at 09:15."""
    if gap_pts is None or not pdc:
        state = RegimeState(regime=Regime.NORMAL, us_direction=us_direction, event_day=event_day)
        state.notes.append("GIFT Nifty or PDC MISSING -> preliminary gap verdict unavailable")
        if event_day:
            state.regime = Regime.NO_TRADE_DAY
            state.notes.append("date in EVENT_CALENDAR -> NO-TRADE DAY (silent, journal it)")
        return state
    state = RegimeClassifier().classify_at_open(
        spot=pdc + gap_pts, pdc=pdc, us_direction=us_direction, event_day=event_day)
    state.notes.insert(0, "PRELIMINARY verdict from GIFT Nifty; final classification at 09:15")
    return state
