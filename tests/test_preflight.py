"""Tests for Live Preflight 15 Operational Checkpoints (Phase 10)."""
import pytest
from engines.preflight import (
    check_chain_live,
    check_candles_fresh,
    check_snapshot_builder,
    check_level_lifecycle,
    check_s1_strategy,
    check_s2_strategy,
    check_s3_strategy,
    check_arbiter_resolution,
    check_risk_governor_gates,
    check_position_manager_state,
    check_telegram_explainable_format,
    check_intrabar_sl_armed,
    check_journal_unique_constraint,
    check_state_machine_symmetry,
    check_shadow_ledger,
    run_full_preflight,
)


def test_preflight_snapshot_builder():
    ok, detail = check_snapshot_builder()
    assert ok is True
    assert "✓" in detail


def test_preflight_level_lifecycle():
    ok, detail = check_level_lifecycle()
    assert ok is True
    assert "✓" in detail


def test_preflight_strategies():
    ok1, d1 = check_s1_strategy()
    ok2, d2 = check_s2_strategy()
    ok3, d3 = check_s3_strategy()
    assert ok1 is True and "✓" in d1
    assert ok2 is True and "✓" in d2
    assert ok3 is True and "✓" in d3


def test_preflight_arbiter_and_risk_governor():
    ok_arb, d_arb = check_arbiter_resolution()
    ok_gov, d_gov = check_risk_governor_gates()
    assert ok_arb is True and "✓" in d_arb
    assert ok_gov is True and "✓" in d_gov


def test_preflight_position_manager_and_telegram():
    ok_pm, d_pm = check_position_manager_state()
    ok_tg, d_tg = check_telegram_explainable_format()
    assert ok_pm is True and "✓" in d_pm
    assert ok_tg is True and "✓" in d_tg


def test_preflight_state_machine_symmetry():
    ok, detail = check_state_machine_symmetry()
    assert ok is True
    assert "✓" in detail


def test_preflight_journal_unique_constraint():
    ok, detail = check_journal_unique_constraint()
    assert ok is True
    assert "✓" in detail


def test_preflight_intrabar_sl_armed():
    ok, detail = check_intrabar_sl_armed()
    assert ok is True
    assert "✓" in detail


def test_preflight_shadow_ledger():
    ok, detail = check_shadow_ledger()
    assert ok is True
    assert "✓" in detail
