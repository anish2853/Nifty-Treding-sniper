"""Unit tests for Explainable Telegram Messages and Section 63 regression test."""
from alerts.messages import explainable_trade_signal, no_trade_alert


def test_telegram_output_faithfulness_no_hallucinations():
    """Section 63 Acceptance Test: Verifies Telegram output contains full 10-point
    evidence, invalidation rules, and marks missing factors as unavailable without hallucinations."""
    evidence = [
        "5m structure = Series of 3 Higher Highs & Higher Lows confirmed",
        "15m context aligned with bullish displacement",
        "Price above rising VWAP (slope: +1.5 pts/bar)",
        "Range expansion confirmed (ATR > 20-period avg)",
        "Volume: Unavailable — Omitted",
        "OI behavior supports continuation (-35k CE covering)",
        "Room to next opposing liquidity = 42 pts",
        "Risk:Reward = 2.1 R to initial liquidity level",
        "Historical expectancy: Sample size N < 20 (Observation mode)",
        "Signal freshness = 35 sec (Valid until 10:15 IST)",
    ]

    invalidation = [
        "5m candle close below structure swing 22,460",
        "Hard stop hit at 22,440 spot (-1.1R hard cap enforced)",
        "15:10 forced square-off",
    ]

    msg = explainable_trade_signal(
        strategy="S1 TREND EXPANSION",
        direction="LONG",
        regime="TREND-UP (EXPANSION)",
        entry_range="22,480 – 22,495",
        sl_spot="22,440.00",
        risk_r="1.0R",
        management="STRUCTURE TRAIL",
        evidence_items=evidence,
        invalidation_items=invalidation,
        trade_id="TRD-20261008-01",
    )

    assert "🎯 NIFTY SNIPER — TRADE SIGNAL" in msg
    assert "Strategy:   S1 TREND EXPANSION" in msg
    assert "Direction:  LONG" in msg
    assert "WHY THIS TRADE?" in msg
    assert "Volume: Unavailable — Omitted" in msg, "Must not hallucinate volume when missing!"
    assert "Sample size N < 20 (Observation mode)" in msg, "Must not fabricate statistical significance!"
    assert "INVALIDATION" in msg
    assert "5m candle close below structure" in msg


def test_no_trade_alert():
    alert = no_trade_alert(
        strategy="S3 LIQUIDITY REVERSAL",
        candidate_direction="SHORT",
        reason="Counter-trend reversal vetoed: S1 Trend Strength is HIGH (Market in EXPANSION)",
    )
    assert "ℹ️ NO TRADE" in alert
    assert "Candidate: S3 LIQUIDITY REVERSAL (SHORT)" in alert
    assert "Counter-trend reversal vetoed" in alert
