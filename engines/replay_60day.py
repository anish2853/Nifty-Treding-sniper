"""Batch replay across N trading days with run-scoped journal integrity."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import settings
from data_sources.replay_data import prepare_replay_data
from engines.replay_mtf import _replay_day, DayReplay
from journal.replay_store import ReplayJournal, print_run_summary, stats_for_run

log = logging.getLogger("replay_60day")


@dataclass
class ReplaySession:
    """Accumulated stats across N days of replay (in-memory mirror of journal)."""
    run_id: str
    start_date: date
    end_date: date
    days_replayed: int = 0
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0
    total_r: float = 0.0
    max_dd: float = 0.0
    current_dd: float = 0.0
    trades_by_family: dict = field(default_factory=dict)
    trades_by_state: dict = field(default_factory=dict)
    daily_summaries: list = field(default_factory=list)
    gaps: list = field(default_factory=list)

    def win_rate(self) -> float:
        total = self.total_wins + self.total_losses
        return (self.total_wins / total * 100) if total > 0 else 0.0

    def expectancy(self) -> float:
        return (self.total_r / self.total_trades) if self.total_trades > 0 else 0.0

    def summary(self) -> str:
        return (
            f"Replay {self.start_date} to {self.end_date} | run {self.run_id} | "
            f"{self.days_replayed} days | trades (unique) {self.total_trades} | "
            f"Win rate {self.win_rate():.1f}% | Expectancy {self.expectancy():+.2f}R | "
            f"Max DD {self.max_dd:+.2f}R"
        )


def run_replay_60day(days: int = 60, db_path: str | None = None) -> ReplaySession:
    """Execute full replay; clears prior replay60 journal rows before starting."""
    dataset = prepare_replay_data(days=days, print_table=True)
    valid_dates = sorted(dataset.valid_days.keys())

    journal = ReplayJournal(db_path)
    run_id = journal.begin_run("replay60", days)

    if not valid_dates:
        log.warning("No valid trading days found in the last %d days", days)
        journal.finish_run()
        journal.close()
        today = date.today()
        return ReplaySession(run_id=run_id, start_date=today, end_date=today)

    session = ReplaySession(
        run_id=run_id,
        start_date=valid_dates[0],
        end_date=valid_dates[-1],
    )

    log.info("Starting replay60 %s: %s to %s (%d valid trading days)",
             run_id, valid_dates[0], valid_dates[-1], len(valid_dates))

    for day_idx, trading_day in enumerate(valid_dates, 1):
        day_data = dataset.valid_days[trading_day]
        try:
            day_replay = _replay_day(trading_day, day_data=day_data)
            if day_replay is None:
                log.warning("[%d/%d] %s: No data", day_idx, len(valid_dates), trading_day)
                session.gaps.append(trading_day)
                continue

            session.days_replayed += 1

            for trade in day_replay.mtf_trades:
                result_r = trade.result_r or 0.0
                inserted = journal.record_trade(
                    replay_date=day_replay.date, day_num=day_idx, family=trade.family,
                    direction=trade.direction, entry_time=trade.entry_time,
                    entry_price=trade.entry, sl_spot=trade.sl, sl_prem=trade.sl_prem,
                    exit_time=trade.exit_time, exit_price=trade.exit_spot,
                    exit_reason=trade.exit_reason, result_r=result_r,
                    zone_id=trade.zone_id or None, notes=trade.notes,
                    risk_units=getattr(trade, "risk_units", 1.0))
                if not inserted:
                    log.warning("Duplicate trade blocked: %s %s @ %s",
                                trade.family, trade.direction, trade.entry_time)
                    continue

                session.total_trades += 1
                session.total_r += result_r
                if result_r > 0:
                    session.total_wins += 1
                elif result_r < 0:
                    session.total_losses += 1
                session.current_dd += result_r
                session.max_dd = min(session.max_dd, session.current_dd)

                session.trades_by_family.setdefault(trade.family, []).append(trade)
                state = "UNKNOWN"
                if trade.notes and "state:" in trade.notes:
                    state = trade.notes.split("state:")[1].split()[0]
                session.trades_by_state.setdefault(state, []).append(trade)

            day_trades_r = [t.result_r or 0.0 for t in day_replay.mtf_trades]
            session.daily_summaries.append({
                "date": trading_day.isoformat(),
                "trades": len(day_replay.mtf_trades),
                "total_r": sum(day_trades_r),
                "win_count": len([r for r in day_trades_r if r > 0]),
                "win_rate": (len([r for r in day_trades_r if r > 0]) / len(day_trades_r) * 100)
                if day_trades_r else 0.0,
            })
            log.info("[%d/%d] %s: %d trades (day total R: %+.2fR)",
                     day_idx, len(valid_dates), trading_day,
                     len(day_replay.mtf_trades), sum(day_trades_r))
        except Exception as exc:
            log.error("[%d/%d] %s: %s", day_idx, len(valid_dates), trading_day, exc)
            session.gaps.append(trading_day)

    journal.finish_run()
    db_stats = stats_for_run(journal.conn, run_id)
    session.total_trades = db_stats["trades_unique"]
    session.total_wins = db_stats["wins"]
    session.total_losses = db_stats["losses"]
    session.total_r = db_stats["total_r"]
    session.max_dd = db_stats["max_dd"]
    csv_path = journal.export_csv()

    log.info("=" * 80)
    log.info(session.summary())
    log.info("Export: %s", csv_path)
    print_run_summary(journal.conn, run_id)
    log.info("=" * 80)

    for family, trades in session.trades_by_family.items():
        family_r = [t.result_r or 0.0 for t in trades]
        wins = len([r for r in family_r if r > 0])
        wr = (wins / len(family_r) * 100) if family_r else 0.0
        exp = sum(family_r) / len(family_r) if family_r else 0.0
        log.info("  [%s] %d trades | Win rate %.1f%% | Expectancy %+.2fR",
                 family, len(trades), wr, exp)

    journal.close()
    return session
