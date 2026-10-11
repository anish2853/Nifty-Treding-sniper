"""Tests for Phase 13 Freeze Governance & Parameter Auto-Revert Protocol."""
import pytest
from engines.freeze_governance import (
    FROZEN_V1_SPEC,
    auto_revert,
    get_frozen_spec,
    log_parameter_change_attempt,
    verify_freeze_integrity,
)


def test_frozen_spec_immutability():
    spec = get_frozen_spec()
    assert spec["config_version"] == "FROZEN-v1"
    assert spec["entry_mechanics"] == "RETEST_ONLY"
    assert spec["retest_max_bars"] == 12
    assert spec["be_trigger_r"] == 0.7
    assert spec["partial_book_trigger_r"] == 1.5
    assert spec["real_money_gate"] == "BLOCKED"
    assert spec["status"] == "PAPER ONLY"


def test_verify_freeze_integrity_clean():
    clean_params = {
        "config_version": "FROZEN-v1",
        "entry_mechanics": "RETEST_ONLY",
        "real_money_gate": "BLOCKED",
        "status": "PAPER ONLY",
        "max_trades_per_day": 2,
        "max_trades_per_direction": 1,
        "consecutive_loss_halt": 2,
    }
    is_clean, mismatches = verify_freeze_integrity(clean_params)
    assert is_clean is True
    assert len(mismatches) == 0


def test_verify_freeze_integrity_drift():
    tampered_params = {
        "config_version": "EXPERIMENTAL-v2",
        "max_trades_per_day": 5,  # Breached cap!
    }
    is_clean, mismatches = verify_freeze_integrity(tampered_params)
    assert is_clean is False
    assert len(mismatches) == 2
    assert any("config_version" in m for m in mismatches)
    assert any("max_trades_per_day" in m for m in mismatches)


def test_unregistered_change_auto_revert():
    allowed, active_val, msg = log_parameter_change_attempt(
        param_name="max_trades_per_day",
        old_value=2,
        attempted_value=4,
        reason="Testing without protocol",
        experiment_id=None,
    )
    assert allowed is False
    assert active_val == 2  # Reverted to frozen baseline
    assert "rejected" in msg.lower()
    assert "auto-reverted" in msg.lower()


def test_registered_experiment_allowed():
    allowed, active_val, msg = log_parameter_change_attempt(
        param_name="be_trigger_r",
        old_value=0.7,
        attempted_value=0.8,
        reason="Testing under approved protocol EXP-14A",
        experiment_id="EXP-14A",
    )
    assert allowed is True
    assert active_val == 0.8
    assert "permitted" in msg.lower()


def test_auto_revert_lookup():
    assert auto_revert("retest_max_bars") == 12
    assert auto_revert("be_trigger_r") == 0.7
    with pytest.raises(KeyError):
        auto_revert("non_existent_parameter")
