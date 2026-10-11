"""Unit and Integration Tests for Phase 13.4: Compliance Validator, Shadow Ledger, and Outage Exit."""
from datetime import datetime, timedelta
import pytest
from types import SimpleNamespace

from engines.compliance_validator import SignalComplianceValidator, ComplianceResult
from engines.shadow_compliant import ShadowCompliantLedger, ShadowTrade
from engines.position_manager import PositionManager, TradeStateEnum, ExitClassification
from engines.candles import Candle
from utils import ist_now


def test_compliance_validator_blocks_wide_sl():
    res = SignalComplianceValidator.validate(
        strategy_id="S1_TREND_EXPANSION",
        direction="LONG",
        spot_entry=22488.95,
        initial_sl=22396.52,  # 92.43 pts > 33 pts
        score=75.0,
        market_state="RANGE",
        reason_codes=["Breakout above ORB-H", "retest confirmed off broken level"],
        retest_confirmed=True,
        strike=22500,
        expiry="2026-10-15",
        entry_prem=145.0,
        sl_prem=108.75,
        tgt1_spot=22520.0,
        tgt1_prem=160.0,
        tgt2_spot=22550.0,
        tgt2_prem=180.0,
        max_risk_rs=906.0,
        rr_ratio=1.5,
    )
    assert res.is_compliant is False
    assert any("WIDE_SL" in v for v in res.violations)
    assert res.details["sl_distance_pts"] == 92.43


def test_compliance_validator_blocks_no_retest():
    res = SignalComplianceValidator.validate(
        strategy_id="S1_TREND_EXPANSION",
        direction="LONG",
        spot_entry=22488.95,
        initial_sl=22465.0,  # 23.95 pts <= 33 pts
        score=75.0,
        market_state="RANGE",
        reason_codes=["Breakout above ORB-H", "VWAP holding"],  # No retest
        retest_confirmed=False,
        strike=22500,
        expiry="2026-10-15",
        entry_prem=145.0,
        sl_prem=108.75,
        tgt1_spot=22525.0,
        tgt1_prem=160.0,
        tgt2_spot=22550.0,
        tgt2_prem=180.0,
        max_risk_rs=906.0,
        rr_ratio=1.5,
    )
    assert res.is_compliant is False
    assert any("NO_RETEST" in v for v in res.violations)


def test_compliance_validator_blocks_incomplete_card():
    res = SignalComplianceValidator.validate(
        strategy_id="S1_TREND_EXPANSION",
        direction="LONG",
        spot_entry=22488.95,
        initial_sl=22465.0,
        score=75.0,
        market_state="RANGE",
        reason_codes=["retest confirmed off broken level"],
        retest_confirmed=True,
        strike=None,  # Missing strike
        expiry=None,  # Missing expiry
        entry_prem=None,
    )
    assert res.is_compliant is False
    assert any("INCOMPLETE_CARD" in v for v in res.violations)


def test_compliance_validator_passes_fully_compliant():
    res = SignalComplianceValidator.validate(
        strategy_id="S1_TREND_EXPANSION",
        direction="LONG",
        spot_entry=22488.95,
        initial_sl=22468.95,  # 20 pts risk
        score=85.0,
        market_state="TREND",
        reason_codes=["retest confirmed off broken level", "rejection candle printed"],
        retest_confirmed=True,
        strike=22500,
        expiry="2026-10-15",
        entry_prem=145.0,
        sl_prem=108.75,
        tgt1_spot=22518.95,
        tgt1_prem=160.0,
        tgt2_spot=22538.95,
        tgt2_prem=175.0,
        max_risk_rs=906.0,
        rr_ratio=1.5,
    )
    assert res.is_compliant is True
    assert len(res.violations) == 0


def test_shadow_compliant_ledger_simulation(tmp_path):
    ledger_file = tmp_path / "shadow.json"
    ledger = ShadowCompliantLedger(data_file=ledger_file)
    
    # Record non-compliant trade in shadow (wide SL + breakout entry)
    st = ledger.record_signal_shadow(
        strategy_id="S1_TREND_EXPANSION",
        direction="LONG",
        broken_level=22475.0,  # Retest entry price
        spot_close=22488.95,
        initial_sl=22396.52,  # Live wide SL
        fired_r=-1.0,         # Live trade lost -1.0R
        retest_confirmed=True,
        live_trade_id="TRD-1",
        entry_time="2026-10-09T10:15:00",
    )
    assert st.retest_entry_price == 22475.0
    assert st.risk_pts <= 25.0
    assert st.retest_sl == 22450.0  # 22475 - 25

    # Candle 1 pushes price to TGT1 (22475 + 25 = 22500)
    ledger.update_candles(high=22505.0, low=22472.0, close=22502.0, candle_time="2026-10-09T10:20:00")
    assert st.status == "TGT1_HIT"
    assert st.half_booked is True
    assert st.runner_sl == 22475.0  # BE stop

    # Candle 2 drops back and hits BE stop (low 22470 <= 22475)
    ledger.update_candles(high=22502.0, low=22470.0, close=22474.0, candle_time="2026-10-09T10:25:00")
    assert st.status == "CLOSED"
    assert st.shadow_r == 0.50  # Half booked at +1.0R, half at BE -> +0.50R

    summary = ledger.get_eod_summary("2026-10-09")
    assert summary["day_fired_r"] == -1.0
    assert summary["day_shadow_r"] == 0.50
    assert summary["day_cost_of_violation"] == -1.50
    assert "cost of violation: -1.50R" in summary["report_text"]
