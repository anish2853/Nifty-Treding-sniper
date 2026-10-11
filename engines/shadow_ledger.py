"""Shadow Trading Ledger & Statistical Matrix (Phase 9).

Per Section 24 & Section 58 of Master Architecture:
When primary strategy (e.g. S1) is selected by the Arbiter, all other valid strategies
(e.g. S2, S3) are persisted as counterfactual shadow trades.
Shadow trades track hypothetical executions without risking capital, building
a robust statistical expectancy matrix (strategy x regime x direction).
"""
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import settings
from data_sources.snapshot import MarketSnapshot
from engines.strategies import StrategyAssessment

log = logging.getLogger("shadow_ledger")


class ShadowLedger:
    """Manages SQLite storage and lifecycle for counterfactual shadow trades."""

    def __init__(self, db_path: Path | str | None = None):
        if db_path is None:
            db_path = settings.DATA_DIR / "shadow_ledger.sqlite3"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shadow_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT UNIQUE NOT NULL,
                    snapshot_id TEXT,
                    timestamp TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    setup_score INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    initial_sl REAL NOT NULL,
                    risk_pts REAL NOT NULL,
                    target_plan TEXT,
                    not_selected_reason TEXT NOT NULL,
                    regime TEXT,
                    exit_time TEXT,
                    exit_price REAL,
                    exit_reason TEXT,
                    mfe_r REAL DEFAULT 0.0,
                    mae_r REAL DEFAULT 0.0,
                    final_r REAL DEFAULT 0.0,
                    is_active INTEGER DEFAULT 1
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_active ON shadow_trades(is_active)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_strategy ON shadow_trades(strategy_id, regime, direction)")
            conn.commit()

    def record_shadow_candidate(
        self,
        assessment: StrategyAssessment,
        spot: float,
        not_selected_reason: str,
        regime: str = "NORMAL",
    ) -> str | None:
        """Records an unselected valid strategy assessment as a shadow trade."""
        if not assessment.eligible or assessment.direction == "NONE":
            return None

        entry_price = spot
        initial_sl = assessment.initial_sl
        risk_pts = abs(entry_price - initial_sl) if initial_sl else 25.0
        if risk_pts <= 0:
            risk_pts = 25.0

        ts_str = assessment.timestamp.isoformat()
        trade_id = f"SHADOW-{assessment.strategy_id}-{assessment.timestamp.strftime('%Y%m%d%H%M%S')}"

        try:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO shadow_trades (
                        trade_id, snapshot_id, timestamp, strategy_id, direction,
                        setup_score, entry_price, initial_sl, risk_pts, target_plan,
                        not_selected_reason, regime, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """, (
                    trade_id,
                    assessment.snapshot_id,
                    ts_str,
                    assessment.strategy_id,
                    assessment.direction,
                    assessment.setup_score,
                    entry_price,
                    initial_sl,
                    risk_pts,
                    assessment.management_plan,
                    not_selected_reason,
                    regime,
                ))
                conn.commit()
                log.info("Recorded shadow trade: %s (%s %s)", trade_id, assessment.strategy_id, assessment.direction)
                return trade_id
        except Exception as e:
            log.error("Failed to record shadow candidate: %s", e)
            return None

    def update(self, snapshot: MarketSnapshot) -> int:
        """Updates active shadow trades against current candle. Returns count of exited trades."""
        candle = snapshot.latest_candle
        if not candle:
            return 0

        now_str = snapshot.timestamp.isoformat()
        high = candle.high
        low = candle.low
        close = candle.close
        is_eod = snapshot.timestamp.time() >= settings.SQUARE_OFF_TIME

        exited_count = 0

        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM shadow_trades WHERE is_active = 1")
            active_trades = cursor.fetchall()

            for row in active_trades:
                trade_id = row["trade_id"]
                direction = row["direction"]
                entry = row["entry_price"]
                sl = row["initial_sl"]
                risk = row["risk_pts"]
                mfe = row["mfe_r"]
                mae = row["mae_r"]

                exit_time = None
                exit_price = None
                exit_reason = None
                final_r = 0.0
                is_active = 1

                if direction == "LONG":
                    curr_mfe = max(mfe, (high - entry) / risk)
                    curr_mae = min(mae, (low - entry) / risk)

                    if low <= sl:
                        exit_price = sl
                        exit_reason = "SL_HIT"
                        final_r = -1.0
                        is_active = 0
                    elif high >= entry + (2.0 * risk):
                        exit_price = entry + (2.0 * risk)
                        exit_reason = "TGT_2R"
                        final_r = 2.0
                        is_active = 0
                    elif is_eod:
                        exit_price = close
                        exit_reason = "SQUARE_OFF"
                        final_r = (close - entry) / risk
                        is_active = 0

                else:  # SHORT
                    curr_mfe = max(mfe, (entry - low) / risk)
                    curr_mae = min(mae, (entry - high) / risk)

                    if high >= sl:
                        exit_price = sl
                        exit_reason = "SL_HIT"
                        final_r = -1.0
                        is_active = 0
                    elif low <= entry - (2.0 * risk):
                        exit_price = entry - (2.0 * risk)
                        exit_reason = "TGT_2R"
                        final_r = 2.0
                        is_active = 0
                    elif is_eod:
                        exit_price = close
                        exit_reason = "SQUARE_OFF"
                        final_r = (entry - close) / risk
                        is_active = 0

                if is_active == 0:
                    exited_count += 1
                    exit_time = now_str
                    conn.execute("""
                        UPDATE shadow_trades SET
                            exit_time = ?, exit_price = ?, exit_reason = ?,
                            mfe_r = ?, mae_r = ?, final_r = ?, is_active = 0
                        WHERE trade_id = ?
                    """, (exit_time, exit_price, exit_reason, curr_mfe, curr_mae, final_r, trade_id))
                else:
                    conn.execute("""
                        UPDATE shadow_trades SET
                            mfe_r = ?, mae_r = ?
                        WHERE trade_id = ?
                    """, (curr_mfe, curr_mae, trade_id))

            conn.commit()

        return exited_count

    def get_statistical_matrix(self) -> dict[str, Any]:
        """Calculates performance statistics grouped by strategy, regime, and direction."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT
                    strategy_id,
                    regime,
                    direction,
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN final_r > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN final_r <= 0 THEN 1 ELSE 0 END) as losses,
                    AVG(final_r) as expectancy_r,
                    AVG(CASE WHEN final_r > 0 THEN final_r ELSE NULL END) as avg_win_r,
                    AVG(CASE WHEN final_r < 0 THEN final_r ELSE NULL END) as avg_loss_r,
                    AVG(mfe_r) as avg_mfe_r,
                    AVG(mae_r) as avg_mae_r,
                    SUM(CASE WHEN final_r > 0 THEN final_r ELSE 0 END) as total_profit_r,
                    SUM(CASE WHEN final_r < 0 THEN ABS(final_r) ELSE 0 END) as total_loss_r
                FROM shadow_trades
                WHERE is_active = 0
                GROUP BY strategy_id, regime, direction
            """)
            rows = cursor.fetchall()

            matrix: dict[str, Any] = {}
            for r in rows:
                key = f"{r['strategy_id']}::{r['regime']}::{r['direction']}"
                n = r["total_trades"]
                wins = r["wins"]
                win_rate = (wins / n) if n > 0 else 0.0
                profit_factor = (
                    r["total_profit_r"] / r["total_loss_r"]
                    if r["total_loss_r"] and r["total_loss_r"] > 0
                    else (float("inf") if r["total_profit_r"] > 0 else 0.0)
                )

                matrix[key] = {
                    "strategy_id": r["strategy_id"],
                    "regime": r["regime"],
                    "direction": r["direction"],
                    "sample_size": n,
                    "wins": wins,
                    "losses": r["losses"],
                    "win_rate": round(win_rate, 3),
                    "expectancy_r": round(r["expectancy_r"] or 0.0, 3),
                    "avg_win_r": round(r["avg_win_r"] or 0.0, 3),
                    "avg_loss_r": round(r["avg_loss_r"] or 0.0, 3),
                    "avg_mfe_r": round(r["avg_mfe_r"] or 0.0, 3),
                    "avg_mae_r": round(r["avg_mae_r"] or 0.0, 3),
                    "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
                }
            return matrix
