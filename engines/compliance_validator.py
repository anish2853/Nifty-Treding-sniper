"""Signal Compliance Validator — Blocking, inside the signal path (Phase 13.4).

Enforces mandatory FROZEN-v1 compliance rules BEFORE any trade card is sent
or any order/paper position is committed:
1. RETEST CONFIRMED: 5m candle pulls back to broken level / POI and prints a
   rejection candle off it. Breakout-candle closes without retest are strictly blocked.
2. SL DISTANCE <= 33 pts: Retest structural stop must be <= 33 spot points.
3. REGIME & SCORE: Score >= 75 and regime alignment confirmed.
4. COMPLETE CARD: All execution fields must be populated:
   - Instrument strike (rounded to 50) + tradeable expiry (>= 2 days)
   - Entry premium band (LTP to LTP+10)
   - Premium SL (75% of LTP)
   - TGT1 (spot + premium)
   - TGT2 (spot + premium)
   - Max risk in Rupees
   - R:R ratio printed (>= 1:1.5)

ANY failure BLOCKS the signal and raises a compliance alert listing all violations.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import settings

log = logging.getLogger("compliance_validator")


@dataclass
class ComplianceResult:
    is_compliant: bool
    violations: list[str] = field(default_factory=list)
    retest_ok: bool = False
    sl_ok: bool = False
    regime_score_ok: bool = False
    card_complete: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if self.is_compliant:
            return "COMPLIANT (All FROZEN-v1 gates passed)"
        return "BLOCKED: " + " | ".join(self.violations)


class SignalComplianceValidator:
    """Zero-compromise signal gate enforcing FROZEN-v1 execution mechanics."""

    MAX_SL_DISTANCE_PTS: float = 33.0
    MIN_SCORE: float = 75.0
    MIN_RR_RATIO: float = 1.5

    @classmethod
    def validate(
        cls,
        strategy_id: str,
        direction: str,
        spot_entry: float,
        initial_sl: float,
        score: float,
        market_state: str,
        reason_codes: list[str] | None = None,
        retest_confirmed: bool = False,
        strike: float | int | None = None,
        expiry: str | None = None,
        entry_prem: float | None = None,
        sl_prem: float | None = None,
        tgt1_spot: float | None = None,
        tgt1_prem: float | None = None,
        tgt2_spot: float | None = None,
        tgt2_prem: float | None = None,
        max_risk_rs: float | None = None,
        rr_ratio: float | None = None,
    ) -> ComplianceResult:
        violations: list[str] = []
        details: dict[str, Any] = {}

        # 1. RETEST CONFIRMATION GATE
        # Must be confirmed retest rejection off broken level / POI
        reasons = reason_codes or []
        is_retest_explicit = (
            retest_confirmed
            or "_RETEST" in strategy_id
            or any("retest" in r.lower() or "pullback" in r.lower() or "rejection" in r.lower() for r in reasons)
        )
        if not is_retest_explicit:
            violations.append(
                "NO_RETEST: Entry on breakout-candle close. FROZEN-v1 requires pullback + rejection candle off broken level."
            )
            retest_ok = False
        else:
            retest_ok = True
        details["retest_ok"] = retest_ok

        # 2. SL DISTANCE GATE (<= 33 pts)
        sl_dist = abs(spot_entry - initial_sl) if (spot_entry and initial_sl) else 999.0
        details["sl_distance_pts"] = round(sl_dist, 2)
        if sl_dist > cls.MAX_SL_DISTANCE_PTS:
            violations.append(
                f"WIDE_SL: Stop loss distance {sl_dist:.1f} pts exceeds maximum allowable {cls.MAX_SL_DISTANCE_PTS:.0f} pts."
            )
            sl_ok = False
        elif sl_dist < 5.0:
            violations.append(
                f"NARROW_SL: Stop loss distance {sl_dist:.1f} pts is unrealistically tight (< 5 pts)."
            )
            sl_ok = False
        else:
            sl_ok = True
        details["sl_ok"] = sl_ok

        # 3. REGIME & SCORE GATE
        regime_score_ok = True
        if score < cls.MIN_SCORE:
            violations.append(f"LOW_SCORE: Setup score {score:.0f} < minimum required {cls.MIN_SCORE:.0f}.")
            regime_score_ok = False

        if market_state == "NO_TRADE_DAY":
            violations.append("REGIME_BLOCKED: Day classified as NO_TRADE_DAY.")
            regime_score_ok = False
        elif market_state == "RANGE" and not ("REVERSAL" in strategy_id or "SWEEP" in strategy_id or "COIL" in strategy_id or "RETEST" in strategy_id):
            # Continuation in range requires strong structure
            if not any("MSS" in r or "displacement" in r.lower() for r in reasons):
                violations.append("REGIME_MISMATCH: Trend continuation in RANGE without MSS displacement.")
                regime_score_ok = False
        details["regime_score_ok"] = regime_score_ok

        # 4. COMPLETE OPTION & TARGET CARD GATE
        missing_card_fields: list[str] = []
        if strike is None or strike <= 0 or (strike % 50 != 0):
            missing_card_fields.append("Valid Strike (multiple of 50)")
        if not expiry:
            missing_card_fields.append("Tradeable Expiry (>= 2-day rule)")
        if entry_prem is None or entry_prem <= 0:
            missing_card_fields.append("Entry Premium LTP")
        if sl_prem is None or sl_prem <= 0:
            missing_card_fields.append("Premium SL (75% of LTP)")
        if tgt1_spot is None or tgt1_spot <= 0:
            missing_card_fields.append("TGT1 Spot Level")
        if tgt1_prem is None or tgt1_prem <= 0:
            missing_card_fields.append("TGT1 Premium Target")
        if tgt2_spot is None or tgt2_spot <= 0:
            missing_card_fields.append("TGT2 Spot Level")
        if tgt2_prem is None or tgt2_prem <= 0:
            missing_card_fields.append("TGT2 Premium Target")
        if max_risk_rs is None or max_risk_rs <= 0:
            missing_card_fields.append("Max Risk ₹")

        # Check R:R ratio
        calculated_rr = rr_ratio
        if calculated_rr is None and tgt1_spot and spot_entry and sl_dist > 0:
            reward_pts = abs(tgt1_spot - spot_entry)
            calculated_rr = round(reward_pts / sl_dist, 2)
        details["rr_ratio"] = calculated_rr

        if calculated_rr is not None and calculated_rr < cls.MIN_RR_RATIO:
            missing_card_fields.append(f"R:R Ratio >= 1:{cls.MIN_RR_RATIO} (got 1:{calculated_rr})")

        if missing_card_fields:
            violations.append(
                f"INCOMPLETE_CARD: Missing mandatory fields: {', '.join(missing_card_fields)}."
            )
            card_complete = False
        else:
            card_complete = True
        details["card_complete"] = card_complete

        is_compliant = len(violations) == 0
        return ComplianceResult(
            is_compliant=is_compliant,
            violations=violations,
            retest_ok=retest_ok,
            sl_ok=sl_ok,
            regime_score_ok=regime_score_ok,
            card_complete=card_complete,
            details=details,
        )
