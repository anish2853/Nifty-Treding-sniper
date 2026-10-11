"""Tests for Phase 13 Forward Protocol & Saturday Audit Report."""
from datetime import date
from journal.forward_audit import (
    check_data_gaps,
    compute_metrics,
    detect_anomalies,
    generate_weekly_report,
)


def test_compute_metrics_empty():
    res = compute_metrics([])
    assert res["total_trades"] == 0
    assert res["win_rate"] == 0.0
    assert res["net_r"] == 0.0
    assert res["max_dd_r"] == 0.0


def test_compute_metrics_trades():
    sample_trades = [
        {"net_r": 1.2},
        {"net_r": -1.0},
        {"net_r": 1.5},
        {"net_r": -0.5},
        {"net_r": -0.5},
    ]
    res = compute_metrics(sample_trades)
    assert res["total_trades"] == 5
    assert res["wins"] == 2
    assert res["losses"] == 3
    assert res["win_rate"] == 40.0
    assert res["net_r"] == 0.7
    assert res["net_expectancy"] == 0.14
    assert res["max_loss_streak"] == 2


def test_detect_anomalies():
    # 1. Clean metrics
    clean_metrics = {
        "max_dd_r": 2.5,  # <= 3.07 baseline
        "max_loss_streak": 2,  # <= 4 target
    }
    anomalies = detect_anomalies(clean_metrics, gaps=[])
    assert len(anomalies) == 0

    # 2. Drawdown anomaly
    dd_metrics = {
        "max_dd_r": 4.5,  # > 3.07 baseline
        "max_loss_streak": 2,
    }
    anomalies = detect_anomalies(dd_metrics, gaps=[])
    assert len(anomalies) == 1
    assert "DRAWDOWN ANOMALY" in anomalies[0]

    # 3. Streak anomaly
    streak_metrics = {
        "max_dd_r": 2.0,
        "max_loss_streak": 5,  # > 4 limit
    }
    anomalies = detect_anomalies(streak_metrics, gaps=[])
    assert len(anomalies) == 1
    assert "LOSING STREAK ANOMALY" in anomalies[0]

    # 4. Data gap anomaly
    anomalies = detect_anomalies(clean_metrics, gaps=["Missing session: 2026-10-01"])
    assert len(anomalies) == 1
    assert "DATA INTEGRITY ANOMALY" in anomalies[0]


def test_check_data_gaps_weekdays():
    # Monday to Friday with Wednesday missing
    d_mon = date(2026, 10, 5)
    d_tue = date(2026, 10, 6)
    d_thu = date(2026, 10, 8)
    d_fri = date(2026, 10, 9)

    session_dates = [d_mon, d_tue, d_thu, d_fri]
    gaps = check_data_gaps(session_dates, start_date=d_mon, end_date=d_fri)
    assert len(gaps) == 1
    assert "2026-10-07" in gaps[0]  # Wednesday missing


def test_generate_weekly_report_structure():
    metrics = {
        "total_trades": 10,
        "wins": 5,
        "losses": 5,
        "win_rate": 50.0,
        "net_r": 1.5,
        "net_expectancy": 0.15,
        "max_dd_r": 2.0,
        "max_loss_streak": 2,
    }
    report = generate_weekly_report(
        session_count=10,
        metrics=metrics,
        gaps=[],
        anomalies=[],
    )
    assert "FROZEN-v1" in report
    assert "PAPER ONLY" in report
    assert "BLOCKED" in report
    assert "SCORECARD BENCHMARK COMPARISON" in report
    assert "REAL-MONEY GATE VERDICT" in report
