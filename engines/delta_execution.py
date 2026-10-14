"""Delta-Aware Option Strike Selection & Geometry Execution Engine (Phase 14).

Addresses:
1. Target Delta 0.65-0.75 (Slightly ITM strikes) to ensure meaningful option moves (25-100 pts)
   instead of ATM 1:1 scraps.
2. Live Delta conversion for Targets & SL (never flat 0.5).
3. Premium-space Geometry Gate: TGT1 >= 15 premium points.
4. Capital Check: 1 lot ITM premium <= 70% of account.
5. Dual R Accounting: Spot R and Premium R computed and reported on trade cards.
"""

from __future__ import annotations

import logging
import math
import sys
from pathlib import Path
from typing import Any

# Ensure repository root is in sys.path
root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import settings

log = logging.getLogger("engines.delta_execution")


def calculate_black_scholes_delta(
    spot: float,
    strike: float,
    dte_days: float,
    iv: float,
    is_call: bool,
    r: float = 0.065,
) -> float:
    """Computes exact European Black-Scholes Delta using Python's standard normal CDF.

    Returns:
        float: Call delta between 0.0 and 1.0; Put delta between -1.0 and 0.0.
    """
    if spot <= 0 or strike <= 0:
        return 0.50 if is_call else -0.50

    T = max(0.5, float(dte_days)) / 365.0
    sigma = max(0.05, min(1.0, float(iv)))

    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        # Standard normal CDF using math.erf
        cdf = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        return float(cdf if is_call else (cdf - 1.0))
    except Exception as exc:
        log.warning("BS Delta calculation failed (spot=%.1f, strike=%.1f): %s", spot, strike, exc)
        return 0.50 if is_call else -0.50


def select_delta_aware_strike(
    spot: float,
    direction: str,
    chain_snapshot: Any = None,
    vix_value: float = 14.5,
    target_delta_min: float = 0.65,
    target_delta_max: float = 0.75,
    target_delta: float = 0.70,
    dte_days: float = 3.0,
    expiry: str = "",
) -> dict[str, Any]:
    """Selects a slightly In-The-Money (ITM) strike targeting Delta 0.65 - 0.75.

    For LONG / Call: K <= Spot (1-2 strikes ITM)
    For SHORT / Put: K >= Spot (1-2 strikes ITM)

    Returns:
        dict with keys:
            strike (int), option_type (str), option_symbol (str),
            delta (float), entry_prem (float), expiry (str)
    """
    is_call = (direction.upper() in ("LONG", "CE", "BUY_CE"))
    opt_type = "CE" if is_call else "PE"

    # Default IV from India VIX if chain row IV is missing
    fallback_iv = max(0.10, min(0.40, vix_value / 100.0))

    # Determine candidate strikes around spot
    atm_strike = int(round(spot / 50.0) * 50)
    candidates: list[dict[str, Any]] = []

    # Generate ITM strikes (up to 4 strikes ITM)
    step = 50
    if is_call:
        # ITM strikes for Call are below spot: e.g. atm, atm - 50, atm - 100, atm - 150, atm - 200
        strike_list = [atm_strike - (i * step) for i in range(5) if (atm_strike - (i * step)) > 0]
    else:
        # ITM strikes for Put are above spot: e.g. atm, atm + 50, atm + 100, atm + 150, atm + 200
        strike_list = [atm_strike + (i * step) for i in range(5)]

    # Evaluate Greeks for each strike
    for k in strike_list:
        # Retrieve row from chain if available
        row_iv = fallback_iv
        row_ltp = None
        in_chain = False

        if chain_snapshot and hasattr(chain_snapshot, "rows"):
            for r in chain_snapshot.rows:
                if int(round(getattr(r, "strike", 0))) == int(round(k)) and (
                    not expiry or str(getattr(r, "expiry", "")).strip().upper() == str(expiry).strip().upper()
                ):
                    in_chain = True
                    if is_call:
                        iv_val = getattr(r, "ce_iv", None)
                        ltp_val = getattr(r, "ce_ltp", None)
                    else:
                        iv_val = getattr(r, "pe_iv", None)
                        ltp_val = getattr(r, "pe_ltp", None)

                    if iv_val and iv_val > 5.0:
                        row_iv = iv_val / 100.0

                    if ltp_val is not None:
                        row_ltp = float(ltp_val)
                    else:
                        side_data = getattr(r, opt_type.lower(), None)
                        if side_data:
                            if hasattr(side_data, "iv") and side_data.iv and side_data.iv > 5.0:
                                row_iv = side_data.iv / 100.0
                            if hasattr(side_data, "ltp") and side_data.ltp is not None:
                                row_ltp = float(side_data.ltp)
                        elif hasattr(r, "last_price") and r.last_price is not None:
                            row_ltp = float(r.last_price)
                    break

        d = calculate_black_scholes_delta(
            spot=spot,
            strike=float(k),
            dte_days=dte_days,
            iv=row_iv,
            is_call=is_call,
        )
        abs_d = abs(d)

        # Fallback pricing model only if chain LTP completely missing/unknown
        if row_ltp is None:
            intrinsic = max(0.0, (spot - k) if is_call else (k - spot))
            # Model time value based on delta and DTE
            time_value = max(40.0, round((spot * row_iv * math.sqrt(max(1.0, dte_days) / 365.0) * 0.40) / 5.0) * 5.0)
            row_ltp = round(intrinsic + time_value, 1)

        candidates.append({
            "strike": k,
            "delta": round(abs_d, 3),
            "signed_delta": round(d, 3),
            "iv": round(row_iv, 3),
            "ltp": round(row_ltp, 1),
            "diff_from_target": abs(abs_d - target_delta),
            "in_band": (target_delta_min <= abs_d <= target_delta_max),
            "in_chain": in_chain,
        })

    # If any candidate exists in the chain, prioritize chain strikes
    pool = [c for c in candidates if c["in_chain"]]
    if not pool:
        pool = candidates

    # Pick strike: prioritize in-band [0.65, 0.75], closest to 0.70
    in_band_candidates = [c for c in pool if c["in_band"]]
    if in_band_candidates:
        selected = min(in_band_candidates, key=lambda c: c["diff_from_target"])
    else:
        # Pick the candidate closest to 0.70
        selected = min(pool, key=lambda c: c["diff_from_target"])

    strike_val = selected["strike"]
    chosen_delta = selected["delta"]
    chosen_ltp = selected["ltp"]
    opt_symbol = f"NIFTY {strike_val} {opt_type}"
    if expiry:
        opt_symbol = f"{opt_symbol} ({expiry})"

    return {
        "strike": strike_val,
        "option_type": opt_type,
        "option_symbol": opt_symbol,
        "delta": chosen_delta,
        "signed_delta": selected["signed_delta"],
        "entry_prem": chosen_ltp,
        "expiry": expiry,
        "iv": selected["iv"],
    }


def evaluate_delta_geometry_gate(
    entry_prem: float,
    spot_entry: float,
    spot_sl: float,
    spot_tgt1: float,
    spot_tgt2: float,
    delta: float,
    capital: float = 100000.0,
    lot_size: int = 75,
    min_tgt1_premium_pts: float = 15.0,
    max_account_capital_pct: float = 0.70,
) -> dict[str, Any]:
    """Validates premium geometry, risk limits, and converts targets/SL with live delta.

    Gates:
    1. Premium Geometry: TGT1 Prem Gain >= 15.0 pts (Blocks tiny 1:1 ATM scraps).
    2. Capital Cap: 1 Lot ITM Premium <= 70% of account capital.
    3. Live Delta Conversion: SL and Targets scaled with actual delta, not flat 0.5.
    """
    delta_mag = max(0.40, min(1.0, abs(delta)))
    spot_risk = max(5.0, abs(spot_entry - spot_sl))
    spot_tgt1_pts = max(5.0, abs(spot_tgt1 - spot_entry))
    spot_tgt2_pts = max(10.0, abs(spot_tgt2 - spot_entry))

    # Delta-scaled premium levels
    sl_prem_drop = spot_risk * delta_mag
    sl_prem = max(5.0, round(entry_prem - sl_prem_drop, 1))

    tgt1_prem_gain = round(spot_tgt1_pts * delta_mag, 1)
    tgt1_prem = round(entry_prem + tgt1_prem_gain, 1)

    tgt2_prem_gain = round(spot_tgt2_pts * delta_mag, 1)
    tgt2_prem = round(entry_prem + tgt2_prem_gain, 1)

    actual_prem_risk = max(5.0, round(entry_prem - sl_prem, 1))

    # Dual R metrics
    spot_r = round(spot_tgt1_pts / spot_risk, 2) if spot_risk > 0 else 1.0
    prem_r = round(tgt1_prem_gain / actual_prem_risk, 2) if actual_prem_risk > 0 else 1.0

    # Capital requirements
    req_capital = round(entry_prem * lot_size, 2)
    max_capital = round(capital * max_account_capital_pct, 2)
    capital_pct = round((req_capital / capital) * 100.0, 1) if capital > 0 else 0.0

    reasons: list[str] = []
    passed = True

    # Gate 1: Premium Space Geometry (TGT1 >= 15 pts)
    if tgt1_prem_gain < min_tgt1_premium_pts:
        passed = False
        reasons.append(
            f"GEOMETRY_REJECT: TGT1 premium gain ({tgt1_prem_gain:.1f} pts) < {min_tgt1_premium_pts:.1f} pts minimum requirement"
        )

    # Gate 2: Capital Limit Check (<= 70% account)
    if req_capital > max_capital:
        passed = False
        reasons.append(
            f"CAPITAL_LIMIT_EXCEEDED: 1 lot capital ₹{req_capital:,.0f} ({capital_pct:.1f}%) exceeds {max_account_capital_pct:.0%} cap (₹{max_capital:,.0f})"
        )

    return {
        "passed": passed,
        "reasons": reasons,
        "delta": delta_mag,
        "entry_prem": entry_prem,
        "sl_prem": sl_prem,
        "tgt1_prem": tgt1_prem,
        "tgt2_prem": tgt2_prem,
        "tgt1_prem_gain": tgt1_prem_gain,
        "prem_risk_pts": actual_prem_risk,
        "spot_risk_pts": spot_risk,
        "spot_tgt1_pts": spot_tgt1_pts,
        "spot_tgt2_pts": spot_tgt2_pts,
        "spot_r": spot_r,
        "prem_r": prem_r,
        "req_capital": req_capital,
        "capital_pct": capital_pct,
    }


def evaluate_premium_room_gate(
    room_spot_pts: float,
    delta: float,
    min_prem_pts: float = 20.0,
) -> dict[str, Any]:
    """Validates that the next opposing zone is at least 20 premium points away (Phase 15 Sec 4).

    Delta-converted room: room_prem = room_spot_pts * delta.
    If room_prem < 20.0 pts: NO ROOM -> NO TRADE.
    """
    delta_mag = max(0.40, min(1.0, abs(delta)))
    prem_room = round(room_spot_pts * delta_mag, 1)
    passed = prem_room >= min_prem_pts

    reason = ""
    if not passed:
        reason = f"NO_ROOM: {prem_room:.1f} prem pts to next opposing zone (< {min_prem_pts:.1f} pts minimum required)"

    return {
        "passed": passed,
        "prem_room": prem_room,
        "min_required": min_prem_pts,
        "room_spot_pts": room_spot_pts,
        "delta": delta_mag,
        "reason": reason,
    }


def calculate_zone_far_edge_sl(
    entry_spot: float,
    zone_far_edge: float,
    direction: str,
    buffer_pts: float = 3.0,
    max_spot_pts: float = 33.0,
) -> dict[str, float]:
    """Computes protective stop placed beyond zone far edge, capped at max 33 spot pts (Phase 15 Sec 4)."""
    is_long = (direction.upper() in ("LONG", "CE", "BUY_CE"))
    if is_long:
        sl = round(zone_far_edge - buffer_pts, 1)
        if (entry_spot - sl) > max_spot_pts:
            sl = round(entry_spot - max_spot_pts, 1)
        risk = round(entry_spot - sl, 1)
    else:
        sl = round(zone_far_edge + buffer_pts, 1)
        if (sl - entry_spot) > max_spot_pts:
            sl = round(entry_spot + max_spot_pts, 1)
        risk = round(sl - entry_spot, 1)

    return {
        "sl_spot": sl,
        "risk_pts": max(5.0, risk),
    }
