"""Comprehensive replay statistics calculator and report generator.

Aggregates replay trades by:
- Setup family (BREAK / SWEEP / PULLBACK / COIL / STATE-OVERRIDE / LEVEL-FADE / MTF-SCALP)
- Day state (TREND-UP / TREND-DOWN / RANGE)
- Direction (CE / PE)
- Monthly breakdown

Applies cost deduction (₹150 per trade) and evaluates gates:
- expectancy positive after costs
- max DD < 20%
- streak <= 7
- trend AND range periods both net positive
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from collections import defaultdict

import settings
from engines.replay_60day import ReplaySession
from journal.replay_store import (DataIntegrityError, fetch_trades, stats_for_run,
                                  verify_run_integrity)


COST_PER_TRADE_INR = 150  # Transaction cost per trade


@dataclass
class FamilyStats:
    """Statistics for a single setup family."""
    name: str
    trades: list = field(default_factory=list)
    total_r: float = 0.0
    total_inr: float = 0.0
    wins: int = 0
    losses: int = 0
    
    def count(self) -> int:
        return len(self.trades)
    
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0.0
    
    def avg_win_r(self) -> float:
        wins = [t["result_r_after_cost"] for t in self.trades if t.get("result_r_after_cost", 0) > 0]
        return (sum(wins) / len(wins)) if wins else 0.0
    
    def avg_loss_r(self) -> float:
        losses = [t["result_r_after_cost"] for t in self.trades if t.get("result_r_after_cost", 0) < 0]
        return (sum(losses) / len(losses)) if losses else 0.0
    
    def expectancy_r(self) -> float:
        """Average R per trade."""
        return (self.total_r / self.count()) if self.count() > 0 else 0.0
    
    def expectancy_inr(self) -> float:
        """Average INR per trade (after costs)."""
        return (self.total_inr / self.count()) if self.count() > 0 else 0.0
    
    def max_streak(self) -> int:
        """Longest consecutive loss streak."""
        if not self.trades:
            return 0
        streak = 0
        max_streak = 0
        for trade in self.trades:
            result_r = trade.get("result_r_after_cost", 0) if isinstance(trade, dict) else (trade.result_r or 0.0)
            if result_r < 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        return max_streak
    
    def max_drawdown(self) -> float:
        """Running maximum drawdown."""
        if not self.trades:
            return 0.0
        running_sum = 0.0
        max_dd = 0.0
        for trade in self.trades:
            result_r = trade.result_r or 0.0
            running_sum += result_r
            max_dd = min(max_dd, running_sum)
        return max_dd


@dataclass
class GateCheckResult:
    """Gate evaluation result."""
    expectancy_positive: bool
    max_dd_under_20: bool
    streak_under_8: bool
    trend_positive: bool
    range_positive: bool
    all_gates_pass: bool
    
    def summary(self) -> list[str]:
        """Return list of gate check lines for report."""
        return [
            f"[{'✓' if self.expectancy_positive else '✗'}] expectancy positive after costs",
            f"[{'✓' if self.max_dd_under_20 else '✗'}] max DD < 20%",
            f"[{'✓' if self.streak_under_8 else '✗'}] longest streak <= 7",
            f"[{'✓' if self.trend_positive else '✗'}] trend periods net positive",
            f"[{'✓' if self.range_positive else '✗'}] range periods net positive",
        ]


class StatsCalculator:
    """Accumulate and report replay statistics."""
    
    def __init__(self, session: ReplaySession, replay_db_path: str | None = None,
                 run_id: str | None = None):
        self.session = session
        self.run_id = run_id or session.run_id
        self.replay_db_path = replay_db_path or str(settings.DATA_DIR / "replay_journal.sqlite3")
        self.trades: list = []
        self.families: dict[str, FamilyStats] = {}
        self.states: dict[str, FamilyStats] = {}
        self.directions: dict[str, FamilyStats] = {}
        self.months: dict[str, FamilyStats] = {}
        self.integrity: dict = {}
        self._load_trades()
        self._compute_stats()

    def _load_trades(self):
        """Load ONE run_id from replay journal; abort if duplicates exist."""
        conn = sqlite3.connect(f"file:{self.replay_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            self.integrity = verify_run_integrity(conn, self.run_id)
        except DataIntegrityError as exc:
            conn.close()
            raise DataIntegrityError(
                f"DATA INTEGRITY error for run {self.run_id}: {exc}") from exc

        for row in fetch_trades(conn, self.run_id):
            trade = {
                "date": row["replay_date"],
                "family": row["family"],
                "direction": row["direction"],
                "entry_time": row["entry_time"],
                "entry_price": row["entry_price"],
                "sl_spot": row["sl_spot"] if row["sl_spot"] is not None else 0.0,
                "exit_time": row["exit_time"],
                "exit_price": row["exit_price"],
                "exit_reason": row["exit_reason"],
                "result_r": row["result_r"] or 0.0,
                "day_state": row["day_state"],
                "notes": row["notes"],
            }
            cost_r = COST_PER_TRADE_INR / 300.0
            trade["result_r_after_cost"] = trade["result_r"] - cost_r
            trade["result_inr"] = trade["result_r"] * 300.0
            trade["result_inr_after_cost"] = trade["result_inr"] - COST_PER_TRADE_INR
            self.trades.append(trade)
        conn.close()
    
    def _compute_stats(self):
        """Aggregate trades by family, state, direction, month."""
        for trade in self.trades:
            # Family stats
            family_name = trade["family"]
            if family_name not in self.families:
                self.families[family_name] = FamilyStats(name=family_name)
            self.families[family_name].trades.append(trade)
            self.families[family_name].total_r += trade["result_r_after_cost"]
            if trade["result_r_after_cost"] > 0:
                self.families[family_name].wins += 1
            elif trade["result_r_after_cost"] < 0:
                self.families[family_name].losses += 1
            
            # State stats
            state = trade.get("day_state", "UNKNOWN")
            if state not in self.states:
                self.states[state] = FamilyStats(name=state)
            self.states[state].trades.append(trade)
            self.states[state].total_r += trade["result_r_after_cost"]
            if trade["result_r_after_cost"] > 0:
                self.states[state].wins += 1
            elif trade["result_r_after_cost"] < 0:
                self.states[state].losses += 1
            
            # Direction stats
            direction = trade["direction"]
            if direction not in self.directions:
                self.directions[direction] = FamilyStats(name=direction)
            self.directions[direction].trades.append(trade)
            self.directions[direction].total_r += trade["result_r_after_cost"]
            if trade["result_r_after_cost"] > 0:
                self.directions[direction].wins += 1
            elif trade["result_r_after_cost"] < 0:
                self.directions[direction].losses += 1
            
            # Monthly stats
            month_key = trade["date"][:7]  # YYYY-MM
            if month_key not in self.months:
                self.months[month_key] = FamilyStats(name=month_key)
            self.months[month_key].trades.append(trade)
            self.months[month_key].total_r += trade["result_r_after_cost"]
            if trade["result_r_after_cost"] > 0:
                self.months[month_key].wins += 1
            elif trade["result_r_after_cost"] < 0:
                self.months[month_key].losses += 1
    
    def check_gates(self) -> GateCheckResult:
        """Evaluate all gates on overall results."""
        total_trades = len(self.trades)
        if total_trades == 0:
            return GateCheckResult(False, False, False, False, False, False)
        
        total_r = sum(t["result_r_after_cost"] for t in self.trades)
        expectancy = total_r / total_trades
        
        # Max drawdown
        running_sum = 0.0
        max_dd = 0.0
        for trade in self.trades:
            running_sum += trade["result_r_after_cost"]
            max_dd = min(max_dd, running_sum)
        max_dd_pct = abs(max_dd) / max(abs(expectancy * total_trades), 1.0) if expectancy != 0 else 0.0
        
        # Longest losing streak
        streak = 0
        max_streak = 0
        for trade in self.trades:
            if trade["result_r_after_cost"] < 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        
        # Trend vs range split
        trend_trades = [t for t in self.trades if t.get("day_state") and "TREND" in t.get("day_state", "")]
        trend_r = sum(t["result_r_after_cost"] for t in trend_trades)
        trend_positive = trend_r > 0 if trend_trades else True
        
        range_trades = [t for t in self.trades if t.get("day_state") and "RANGE" in t.get("day_state", "")]
        range_r = sum(t["result_r_after_cost"] for t in range_trades)
        range_positive = range_r > 0 if range_trades else True
        
        return GateCheckResult(
            expectancy_positive=expectancy > 0,
            max_dd_under_20=(abs(max_dd_pct) < 0.20),
            streak_under_8=(max_streak <= 7),
            trend_positive=trend_positive,
            range_positive=range_positive,
            all_gates_pass=(
                expectancy > 0 and abs(max_dd_pct) < 0.20 and max_streak <= 7 
                and trend_positive and range_positive
            ),
        )
    
    def generate_markdown_report(self) -> str:
        """Generate comprehensive markdown report."""
        total_trades = len(self.trades)
        if total_trades == 0:
            return "# 60-Day Replay Report\n\n**ERROR**: No trades found in replay journal.\n"

        unique_n = self.integrity.get("unique", total_trades)
        if unique_n != total_trades:
            return (f"# 60-Day Replay Report\n\n**DATA INTEGRITY ERROR**: "
                    f"journal rows {total_trades} != unique trades {unique_n}. "
                    f"Stats aborted.\n")

        total_r = sum(t["result_r_after_cost"] for t in self.trades)
        total_inr = sum(t.get("result_inr_after_cost", 0) for t in self.trades)
        expectancy = total_r / total_trades
        
        # Win rate
        wins = len([t for t in self.trades if t["result_r_after_cost"] > 0])
        losses = len([t for t in self.trades if t["result_r_after_cost"] < 0])
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
        
        # Max drawdown
        running_sum = 0.0
        max_dd = 0.0
        for trade in self.trades:
            running_sum += trade["result_r_after_cost"]
            max_dd = min(max_dd, running_sum)
        
        # Longest streak
        streak = 0
        max_streak = 0
        for trade in self.trades:
            if trade["result_r_after_cost"] < 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        
        gates = self.check_gates()
        
        report = f"""# 60-Day Replay Report

**MODE**: PRICE-ONLY VARIANT | threshold: 52/65
**Run ID**: {self.run_id}
**Period**: {self.session.start_date} to {self.session.end_date}
**Days Replayed**: {self.session.days_replayed}

## Headline Results

| Metric | Value |
|--------|-------|
| Trades (unique) | {total_trades} |
| Journal rows | {self.integrity.get('rows', total_trades)} |
| Win Rate | {win_rate:.1f}% ({wins}W / {losses}L) |
| Avg Win (R) | {(sum([t['result_r_after_cost'] for t in self.trades if t['result_r_after_cost'] > 0]) / wins) if wins > 0 else 0.0:+.2f}R |
| Avg Loss (R) | {(sum([t['result_r_after_cost'] for t in self.trades if t['result_r_after_cost'] < 0]) / losses) if losses > 0 else 0.0:+.2f}R |
| Expectancy (R) | {expectancy:+.2f}R per trade |
| Expectancy (₹) | ₹{total_inr:+.0f} ({total_inr/total_trades:+.0f}₹/trade after ₹150 cost) |
| Max Drawdown | {max_dd:+.2f}R |
| Longest Streak | {max_streak} losses |

## Gates

{chr(10).join(gates.summary())}

**Result**: {'✓ ALL GATES PASS' if gates.all_gates_pass else '✗ SOME GATES FAIL'}

## By Setup Family

"""
        for family_name in sorted(self.families.keys()):
            stats = self.families[family_name]
            if stats.count() == 0:
                continue
            report += f"""### {family_name}

| Metric | Value |
|--------|-------|
| Trades | {stats.count()} |
| Win Rate | {stats.win_rate():.1f}% |
| Avg Win | {stats.avg_win_r():+.2f}R |
| Avg Loss | {stats.avg_loss_r():+.2f}R |
| Expectancy | {stats.expectancy_r():+.2f}R |
| Max Streak | {stats.max_streak()} |
| Total R | {stats.total_r:+.2f}R |

"""
        
        # By day state
        report += "## By Day State\n\n"
        for state_name in sorted(self.states.keys()):
            stats = self.states[state_name]
            if stats.count() == 0:
                continue
            report += f"""### {state_name}

| Metric | Value |
|--------|-------|
| Trades | {stats.count()} |
| Win Rate | {stats.win_rate():.1f}% |
| Expectancy | {stats.expectancy_r():+.2f}R |
| Total R | {stats.total_r:+.2f}R |

"""
        
        # By direction
        report += "## By Direction (Symmetry Check)\n\n"
        for direction in sorted(self.directions.keys()):
            stats = self.directions[direction]
            report += f"""### {direction}

| Metric | Value |
|--------|-------|
| Trades | {stats.count()} |
| Win Rate | {stats.win_rate():.1f}% |
| Expectancy | {stats.expectancy_r():+.2f}R |
| Total R | {stats.total_r:+.2f}R |

"""
        
        # By month
        if len(self.months) > 1:
            report += "## By Month\n\n"
            for month_key in sorted(self.months.keys()):
                stats = self.months[month_key]
                report += f"""### {month_key}

| Metric | Value |
|--------|-------|
| Trades | {stats.count()} |
| Win Rate | {stats.win_rate():.1f}% |
| Expectancy | {stats.expectancy_r():+.2f}R |
| Total R | {stats.total_r:+.2f}R |

"""
        
        report += f"""## Cost Impact

- **Cost per trade**: ₹{COST_PER_TRADE_INR}
- **Total cost**: ₹{COST_PER_TRADE_INR * total_trades}
- **Impact on expectancy**: ~₹{COST_PER_TRADE_INR}₹/trade (already deducted in results above)

## Notes

- All results are post-cost (₹{COST_PER_TRADE_INR} per trade deducted)
- R-value is in standard position size units (1R = stop-loss distance)
- Price-only variant: no OI or volume components (not available in historical replay)
- Separate replay journal: `data/replay_journal.sqlite3`
"""
        
        return report


def generate_replay_report(session: ReplaySession, output_path: str | None = None) -> str:
    """Main entry point: generate full report and save to file.
    
    Args:
        session: ReplaySession with accumulated stats
        output_path: Optional output path for markdown file (default: backtest_results/replay60_report.md)
    
    Returns:
        Markdown report content
    """
    if output_path is None:
        output_path = str(Path(settings.DATA_DIR.parent) / "backtest_results" / "replay60_report.md")
    
    calculator = StatsCalculator(session)
    report = calculator.generate_markdown_report()
    
    # Save to file with UTF-8 encoding
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(report, encoding="utf-8")
    
    return report
