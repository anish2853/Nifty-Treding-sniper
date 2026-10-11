"""Phase 13 Freeze Governance & Parameter Enforcement Module.

Enforces configuration version 'FROZEN-v1':
- Retest entries (max 12 bars, 8 pts pullback tolerance, rejection candle)
- Structural trailing exits (+0.7R BE, +1.5R 50% partial, runner trail)
- Phase 9 MSS displacement body-close gate
- Sniper gates (room-to-run >= 40 pts, body ratio >= 50%, dead-zone filter)
- Risk & trade caps (max 2 trades/day, max 1/dir, 2-loss halt)
- Strict sizing math & capital guardrails

Auto-Revert Protocol:
Any unapproved parameter change attempt is logged to logs/freeze_audit.log
and automatically reverted to the frozen baseline.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import settings

log = logging.getLogger("freeze_governance")

FROZEN_V1_SPEC: dict[str, Any] = {
    "config_version": "FROZEN-v1",
    "entry_mechanics": "RETEST_ONLY",
    "retest_max_bars": 12,
    "retest_pullback_tolerance_pts": 8.0,
    "exit_mechanics": "STRUCTURAL_TRAILING",
    "be_trigger_r": 0.7,
    "partial_book_trigger_r": 1.5,
    "partial_book_fraction": 0.5,
    "s3_mss_gate": True,
    "sniper_min_room_pts": 40.0,
    "sniper_min_body_ratio": 0.50,
    "sniper_no_entry_start": "11:30",
    "sniper_no_entry_end": "13:30",
    "max_trades_per_day": 2,
    "max_trades_per_direction": 1,
    "consecutive_loss_halt": 2,
    "real_money_gate": "BLOCKED",
    "status": "PAPER ONLY",
    "capital": 30000.0,
    "lot_size": 75,
    "max_risk_per_trade_inr": 2500.0,
}

AUDIT_LOG_PATH = settings.LOGS_DIR / "freeze_audit.log"


@dataclass
class FreezeAuditEntry:
    timestamp: str
    param_name: str
    old_value: Any
    attempted_value: Any
    reason: str
    experiment_id: str | None
    action_taken: str


def get_frozen_spec() -> dict[str, Any]:
    """Return a copy of the immutable FROZEN-v1 specification."""
    return dict(FROZEN_V1_SPEC)


def verify_freeze_integrity(current_params: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    """Verify that current runtime configuration exactly matches FROZEN-v1.
    
    Returns (True, []) if perfectly aligned, or (False, [mismatches]) if drift detected.
    """
    if current_params is None:
        current_params = {
            "config_version": getattr(settings, "CONFIG_VERSION", "UNKNOWN"),
            "real_money_gate": getattr(settings, "REAL_MONEY_GATE", "UNKNOWN"),
            "status": getattr(settings, "SYSTEM_STATUS", "UNKNOWN"),
            "max_trades_per_day": settings.MAX_TRADES_PER_DAY,
            "max_trades_per_direction": settings.MAX_TRADES_PER_DIRECTION,
            "consecutive_loss_halt": settings.CONSECUTIVE_LOSS_HALT,
            "lot_size": settings.LOT_SIZE,
        }

    mismatches: list[str] = []
    for key, expected in FROZEN_V1_SPEC.items():
        if key in current_params:
            actual = current_params[key]
            if actual != expected:
                mismatches.append(f"{key}: expected {expected!r}, got {actual!r}")

    is_clean = len(mismatches) == 0
    if not is_clean:
        log.warning("FREEZE INTEGRITY BREACH: %s", mismatches)
    return is_clean, mismatches


def log_parameter_change_attempt(
    param_name: str,
    old_value: Any,
    attempted_value: Any,
    reason: str,
    experiment_id: str | None = None,
) -> tuple[bool, Any, str]:
    """Log an attempted parameter change and enforce auto-revert protocol.
    
    Returns:
        (allowed: bool, active_value: Any, message: str)
    """
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat()

    # Rule: Without a validated experiment protocol and explicit ID, changes are REVERTED
    if not experiment_id or not experiment_id.startswith("EXP-"):
        action = "REVERTED_TO_BASELINE"
        active_val = FROZEN_V1_SPEC.get(param_name, old_value)
        msg = f"Change to '{param_name}' rejected. Unregistered experiment. Auto-reverted to {active_val}."
        allowed = False
    else:
        action = f"ALLOWED_UNDER_{experiment_id}"
        active_val = attempted_value
        msg = f"Change to '{param_name}' permitted under experiment {experiment_id}."
        allowed = True

    entry = FreezeAuditEntry(
        timestamp=ts,
        param_name=param_name,
        old_value=old_value,
        attempted_value=attempted_value,
        reason=reason,
        experiment_id=experiment_id,
        action_taken=action,
    )

    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(entry)) + "\n")

    log.info("FREEZE AUDIT: %s", msg)
    return allowed, active_val, msg


def auto_revert(param_name: str) -> Any:
    """Return the frozen baseline value for any parameter."""
    if param_name not in FROZEN_V1_SPEC:
        raise KeyError(f"Parameter '{param_name}' is not tracked under FROZEN-v1.")
    return FROZEN_V1_SPEC[param_name]
