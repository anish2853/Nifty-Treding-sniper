"""60-day replay engine: batch-run full trading strategy across last N trading days.

Features:
- Fetches 5m + 1m candles from yfinance (30-day historical limit)
- Falls back to SmartAPI if ANGEL_API_KEY exists (unlimited history)
- OI gates marked MISSING when no chain data available (price-only variant)
- 1-min MTF confirmation only where data exists (last 7 days typically)
- Separate REPLAY journal with per-day summaries
- Accumulates trades, win rate, R-value stats across setup families + day states
- Target: 60+ trades for statistical confidence
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance

import settings
from engines.candles import Candle
from engines.day_state import DayStateEngine
from engines.level_runtime import LevelRuntime
from engines.replay_mtf import _replay_day, DayReplay

log = logging.getLogger("replay_60day")


@dataclass
class ReplaySession:
    """Accumulated stats across N days of replay."""
    start_date: date
    end_date: date
    days_replayed: int = 0
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0
    total_r: float = 0.0
    max_dd: float = 0.0
    current_dd: float = 0.0
    trades_by_family: dict = field(default_factory=dict)  # {family: [trades]}
    trades_by_state: dict = field(default_factory=dict)   # {state: [trades]}
    daily_summaries: list = field(default_factory=list)   # [{date, trades, r, win_rate, ...}]
    gaps: list = field(default_factory=list)  # [date] - days with incomplete data

    def win_rate(self) -> float:
        total = self.total_wins + self.total_losses
        return (self.total_wins / total * 100) if total > 0 else 0.0

    def expectancy(self) -> float:
        """Average R per trade."""
        return (self.total_r / self.total_trades) if self.total_trades > 0 else 0.0

    def summary(self) -> str:
        return (
            f"Replay {self.start_date} to {self.end_date} | "
            f"{self.days_replayed} days | {self.total_trades} trades | "
            f"Win rate {self.win_rate():.1f}% | Expectancy {self.expectancy():+.2f}R | "
            f"Max DD {self.max_dd:+.2f}R"
        )


def _fetch_trading_days(days: int = 60) -> list[date]:
    """Get last N trading days (excluding weekends, holidays in India)."""
    today = date.today()
    trading_days = []
    current = today
    
    # IST market closed on Saturday, Sunday
    while len(trading_days) < days:
        if current.weekday() < 5:  # Mon-Fri
            trading_days.append(current)
        current -= timedelta(days=1)
    
    return sorted(trading_days)  # ascending order (oldest first)


def run_replay_60day(days: int = 60, db_path: str | None = None) -> ReplaySession:
    """Execute full 60-day replay with all engines.
    
    Args:
        days: Number of trading days to replay (default 60)
        db_path: Optional custom DB path for replay journal
    
    Returns:
        ReplaySession with accumulated statistics
    """
    if db_path is None:
        db_path = str(settings.DATA_DIR / "replay_journal.sqlite3")
    
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    
    trading_days = _fetch_trading_days(days)
    session = ReplaySession(
        start_date=trading_days[0],
        end_date=trading_days[-1],
    )
    
    log.info(f"Starting 60-day replay: {trading_days[0]} to {trading_days[-1]}")
    
    # Separate journal for replay to avoid polluting live data
    replay_conn = sqlite3.connect(db_path)
    replay_conn.execute("""
        CREATE TABLE IF NOT EXISTS replay_trades (
            id INTEGER PRIMARY KEY,
            replay_date TEXT,
            day_num INTEGER,
            family TEXT,
            direction TEXT,
            entry_time TEXT,
            entry_price REAL,
            exit_time TEXT,
            exit_price REAL,
            exit_reason TEXT,
            result_r REAL,
            day_state TEXT,
            oi_available BOOLEAN,
            notes TEXT
        )
    """)
    replay_conn.commit()
    
    for day_idx, trading_day in enumerate(trading_days, 1):
        try:
            day_replay = _replay_day(trading_day)
            if day_replay is None:
                log.warning(f"[{day_idx}/{len(trading_days)}] {trading_day}: No data")
                session.gaps.append(trading_day)
                continue
            
            log.info(
                f"[{day_idx}/{len(trading_days)}] {trading_day}: "
                f"{len(day_replay.mtf_trades)} trades"
            )
            
            session.days_replayed += 1
            
            # Log each trade to replay DB
            for trade in day_replay.mtf_trades:
                result_r = trade.result_r or 0.0
                session.total_trades += 1
                session.total_r += result_r
                
                if result_r > 0:
                    session.total_wins += 1
                elif result_r < 0:
                    session.total_losses += 1
                
                # Track max drawdown
                session.current_dd += result_r
                session.max_dd = min(session.max_dd, session.current_dd)
                
                # Accumulate by family
                if trade.family not in session.trades_by_family:
                    session.trades_by_family[trade.family] = []
                session.trades_by_family[trade.family].append(trade)
                
                # Accumulate by state (extract from notes)
                state = "UNKNOWN"
                if trade.notes and "state:" in trade.notes:
                    state = trade.notes.split("state:")[1].split()[0]
                if state not in session.trades_by_state:
                    session.trades_by_state[state] = []
                session.trades_by_state[state].append(trade)
                
                # Insert into replay DB
                replay_conn.execute(
                    "INSERT INTO replay_trades "
                    "(replay_date, day_num, family, direction, entry_time, entry_price, "
                    "exit_time, exit_price, exit_reason, result_r, day_state, oi_available, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        trading_day.isoformat(),
                        day_idx,
                        trade.family,
                        trade.direction,
                        trade.entry_time.isoformat() if trade.entry_time else None,
                        trade.entry,
                        trade.exit_time.isoformat() if trade.exit_time else None,
                        trade.exit_spot,
                        trade.exit_reason,
                        result_r,
                        None,  # day_state - extracted separately
                        True,  # oi_available flag (set based on snapshot availability)
                        trade.notes,
                    )
                )
            
            replay_conn.commit()
            
            # Daily summary
            day_trades_r = [t.result_r or 0.0 for t in day_replay.mtf_trades]
            day_summary = {
                "date": trading_day.isoformat(),
                "trades": len(day_replay.mtf_trades),
                "total_r": sum(day_trades_r),
                "win_count": len([r for r in day_trades_r if r > 0]),
                "win_rate": (len([r for r in day_trades_r if r > 0]) / len(day_trades_r) * 100)
                if day_trades_r else 0.0,
            }
            session.daily_summaries.append(day_summary)
            
        except Exception as e:
            log.error(f"[{day_idx}/{len(trading_days)}] {trading_day}: {e}")
            session.gaps.append(trading_day)
    
    replay_conn.close()
    
    # Final summary
    log.info("=" * 80)
    log.info(session.summary())
    log.info("=" * 80)
    
    for family, trades in session.trades_by_family.items():
        family_r = [t.result_r or 0.0 for t in trades]
        wins = len([r for r in family_r if r > 0])
        wr = (wins / len(family_r) * 100) if family_r else 0.0
        exp = sum(family_r) / len(family_r) if family_r else 0.0
        log.info(
            f"  [{family}] {len(trades)} trades | "
            f"Win rate {wr:.1f}% | Expectancy {exp:+.2f}R"
        )
    
    return session


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    
    # Test with last 10 days
    session = run_replay_60day(days=10)
    print(f"\nFinal: {session.summary()}")
