"""Tests for Shadow Trading Ledger & Statistical Matrix (Phase 9).

Per Section 24 & Section 58 of Master Architecture.
"""
from datetime import datetime, timedelta
import tempfile
from pathlib import Path

from data_sources.snapshot import MarketSnapshot
from engines.candles import Candle
from engines.shadow_ledger import ShadowLedger
from engines.strategies import StrategyAssessment
from utils import ist_now


def test_record_shadow_candidate_and_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_shadow.sqlite3"
        ledger = ShadowLedger(db_path)

        now = ist_now().replace(hour=10, minute=0, second=0, microsecond=0)
        c0 = Candle(now, 22200.0, 22210.0, 22195.0, 22205.0)

        # Unselected S2 assessment
        assessment = StrategyAssessment(
            strategy_id="S2_VWAP_MOMENTUM",
            timestamp=now,
            snapshot_id="SNAP-TEST-001",
            eligible=True,
            direction="LONG",
            setup_score=80,
            regime_fit_score=75,
            structure_score=80,
            momentum_score=85,
            liquidity_score=70,
            risk_reward_ratio=2.0,
            normalized_extension=0.6,
            empirical_expectancy=0.55,
            sample_size=25,
            confidence_tier="STRONG",
            entry_zone=(22200.0, 22210.0),
            initial_sl=22180.0, # risk = 25 pts (22205 - 22180)
            management_plan="2R_TARGET",
            valid_until=now + timedelta(minutes=15),
            reason_codes=["VWAP retest rejection"],
        )

        trade_id = ledger.record_shadow_candidate(
            assessment=assessment,
            spot=22205.0,
            not_selected_reason="LOWER_COMPOSITE_SCORE",
            regime="TREND-UP",
        )

        assert trade_id is not None
        assert "SHADOW-S2_VWAP_MOMENTUM" in trade_id

        # Verify active in DB
        with ledger._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM shadow_trades WHERE trade_id = ?", (trade_id,))
            row = cursor.fetchone()
            assert row is not None
            assert row["is_active"] == 1
            assert row["risk_pts"] == 25.0

        # Simulate candle that hits 2R target (entry 22205 + 2*25 = 22255)
        c1 = Candle(now + timedelta(minutes=5), 22205.0, 22260.0, 22200.0, 22255.0)
        snap = MarketSnapshot(
            snapshot_id="SNAP-TEST-002",
            timestamp=now + timedelta(minutes=5),
            spot=22255.0,
            candles=(c0, c1),
            vwap=22210.0,
            atr=25.0,
        )

        exited = ledger.update(snap)
        assert exited == 1

        with ledger._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM shadow_trades WHERE trade_id = ?", (trade_id,))
            row = cursor.fetchone()
            assert row["is_active"] == 0
            assert row["exit_reason"] == "TGT_2R"
            assert row["final_r"] == 2.0


def test_statistical_matrix_calculation():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_matrix.sqlite3"
        ledger = ShadowLedger(db_path)
        now = ist_now().replace(hour=10, minute=0, second=0, microsecond=0)

        # Insert 3 completed trades manually: 2 wins (+2.0R, +1.5R) and 1 loss (-1.0R)
        with ledger._get_connection() as conn:
            conn.execute("""
                INSERT INTO shadow_trades (
                    trade_id, timestamp, strategy_id, direction, setup_score,
                    entry_price, initial_sl, risk_pts, not_selected_reason, regime,
                    exit_reason, final_r, mfe_r, mae_r, is_active
                ) VALUES
                ('T1', ?, 'S2_VWAP_MOMENTUM', 'LONG', 80, 22000, 21975, 25, 'CONCURRENCY', 'TREND-UP', 'TGT_2R', 2.0, 2.2, 0.0, 0),
                ('T2', ?, 'S2_VWAP_MOMENTUM', 'LONG', 75, 22100, 22075, 25, 'CONCURRENCY', 'TREND-UP', 'TGT_2R', 1.5, 1.8, -0.2, 0),
                ('T3', ?, 'S2_VWAP_MOMENTUM', 'LONG', 70, 22200, 22175, 25, 'CONCURRENCY', 'TREND-UP', 'SL_HIT', -1.0, 0.3, -1.0, 0)
            """, (now.isoformat(), now.isoformat(), now.isoformat()))
            conn.commit()

        matrix = ledger.get_statistical_matrix()
        key = "S2_VWAP_MOMENTUM::TREND-UP::LONG"
        assert key in matrix

        stats = matrix[key]
        assert stats["sample_size"] == 3
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert abs(stats["win_rate"] - (2 / 3)) < 0.01
        # Expectancy = (2.0 + 1.5 - 1.0) / 3 = 2.5 / 3 = +0.833R
        assert abs(stats["expectancy_r"] - 0.833) < 0.01
        # Profit Factor = (2.0 + 1.5) / 1.0 = 3.5
        assert stats["profit_factor"] == 3.5
