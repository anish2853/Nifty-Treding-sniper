"""Unit tests for Arbiter and RiskGovernor (Phase 6)."""
from datetime import timedelta, time as dtime
from engines.arbiter import Arbiter, RiskGovernor, TradeIntent
from engines.strategies import StrategyAssessment
from utils import ist_now


def test_arbiter_selects_best_valid_strategy():
    """Section 62 Acceptance Test: Arbiter selects top strategy and routes others to shadows."""
    now = ist_now()
    ttl = now + timedelta(minutes=10)

    # S1 evaluates strongly
    a1 = StrategyAssessment(
        strategy_id="S1_TREND_EXPANSION",
        timestamp=now,
        snapshot_id="SNAP-100",
        eligible=True,
        direction="LONG",
        setup_score=92,
        regime_fit_score=90,
        structure_score=88,
        momentum_score=90,
        liquidity_score=85,
        risk_reward_ratio=2.2,
        normalized_extension=0.8,
        empirical_expectancy=0.71,
        sample_size=38,
        confidence_tier="STRONG",
        entry_zone=(22490.0, 22505.0),
        initial_sl=22450.0,
        management_plan="STRUCTURE_TRAIL",
        valid_until=ttl,
        reason_codes=["Clean breakout above ORB-H"],
    )

    # S2 evaluates modestly
    a2 = StrategyAssessment(
        strategy_id="S2_VWAP_MOMENTUM",
        timestamp=now,
        snapshot_id="SNAP-100",
        eligible=True,
        direction="LONG",
        setup_score=75,
        regime_fit_score=75,
        structure_score=70,
        momentum_score=75,
        liquidity_score=70,
        risk_reward_ratio=1.8,
        normalized_extension=0.8,
        empirical_expectancy=0.45,
        sample_size=32,
        confidence_tier="VALID",
        entry_zone=(22485.0, 22500.0),
        initial_sl=22455.0,
        management_plan="ATR_TRAIL",
        valid_until=ttl,
        reason_codes=["VWAP retest bounce"],
    )

    arbiter = Arbiter()
    intent, shadows, no_trade = arbiter.evaluate([a1, a2])

    assert intent is not None
    assert intent.strategy_id == "S1_TREND_EXPANSION"
    assert intent.direction == "LONG"
    assert len(shadows) == 1
    assert shadows[0].strategy_id == "S2_VWAP_MOMENTUM"


def test_arbiter_returns_no_trade_when_strategies_conflict_without_confirmation():
    """Section 34 & 62 Acceptance Test: Arbiter detects directional disagreement and returns NO_TRADE."""
    now = ist_now()
    ttl = now + timedelta(minutes=10)

    # S1 evaluates LONG with score 84
    a1 = StrategyAssessment(
        strategy_id="S1_TREND_EXPANSION",
        timestamp=now,
        snapshot_id="SNAP-200",
        eligible=True,
        direction="LONG",
        setup_score=84,
        regime_fit_score=75,
        structure_score=80,
        momentum_score=80,
        liquidity_score=75,
        risk_reward_ratio=1.8,
        normalized_extension=0.7,
        empirical_expectancy=0.50,
        sample_size=30,
        confidence_tier="STRONG",
        entry_zone=(22490.0, 22505.0),
        initial_sl=22450.0,
        management_plan="STRUCTURE_TRAIL",
        valid_until=ttl,
    )

    # S3 evaluates SHORT with score 88 (close score, severe directional conflict)
    a3 = StrategyAssessment(
        strategy_id="S3_LIQUIDITY_REVERSAL",
        timestamp=now,
        snapshot_id="SNAP-200",
        eligible=True,
        direction="SHORT",
        setup_score=88,
        regime_fit_score=80,
        structure_score=85,
        momentum_score=70,
        liquidity_score=85,
        risk_reward_ratio=2.0,
        normalized_extension=0.7,
        empirical_expectancy=0.55,
        sample_size=28,
        confidence_tier="STRONG",
        entry_zone=(22500.0, 22515.0),
        initial_sl=22540.0,
        management_plan="POOL_TARGET",
        valid_until=ttl,
    )

    arbiter = Arbiter()
    intent, shadows, no_trade = arbiter.evaluate([a1, a3])

    assert intent is None, "Arbiter must NOT take a trade on unresolved conflict!"
    assert "Unresolved strategy conflict" in no_trade


def test_risk_governor_blindness_and_short_gate():
    """Section 45 & 47 Acceptance Test: RiskGovernor halts on blindness and enforces short gating."""
    now = ist_now().replace(hour=10, minute=30, second=0)
    gov = RiskGovernor()

    intent_short = TradeIntent(
        strategy_id="S3_LIQUIDITY_REVERSAL",
        direction="SHORT",
        entry_zone=(22500.0, 22510.0),
        initial_sl=22540.0,
        target_plan="POOL_TARGET",
        confidence_tier="STRONG",
        reason_codes=["Swept PDH"],
        blocker_codes=[],
        snapshot_id="SNAP-300",
        valid_until=now + timedelta(minutes=10),
    )

    # Case A: Candle engine is blind -> Halt!
    app, reason, _ = gov.validate_intent(intent_short, now, is_blind=True)
    assert app is False
    assert "CANDLE_ENGINE_BLIND" in reason

    # Case B: Not blind, but Short score = 85 (<90) -> Rejected by Short Gate!
    app, reason, _ = gov.validate_intent(intent_short, now, is_blind=False, short_score=85, short_has_strong_confluence=True)
    assert app is False
    assert "SHORT_GATE_REJECT" in reason

    # Case C: Short score = 92 AND strong confluence -> Approved with 0.5R risk!
    app, reason, risk = gov.validate_intent(intent_short, now, is_blind=False, short_score=92, short_has_strong_confluence=True)
    assert app is True
    assert risk == 0.5
    assert intent_short.approved is True
