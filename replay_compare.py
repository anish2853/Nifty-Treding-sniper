#!/usr/bin/env python
"""Command-line replay comparator: YOUR marks vs ENGINE signals.

Usage:
  python replay_compare.py --date 2026-10-06
  python replay_compare.py --date 2026-10-06 --export marks.csv

Features:
- Day-by-day replay viewer
- Manual marking: record your entry points
- Comparison: YOUR marks vs ENGINE signals
- Statistics: accuracy, timing differences, etc.
- Export: save marks and comparison to CSV
"""

import argparse
import csv
from datetime import date, datetime
from pathlib import Path
from typing import Optional
import sys

import settings
from engines.replay_mtf import _replay_day, _fetch_intraday
from utils import safe_print


def format_time(dt: datetime | None) -> str:
    """Format datetime to HH:MM."""
    if dt is None:
        return "—"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.strftime("%H:%M")


def format_price(p: float | None) -> str:
    """Format price to 2 decimals."""
    if p is None:
        return "—"
    return f"{p:.2f}"


def display_day_replay(replay_date: date):
    """Display replay for a single day with comparison."""
    
    print(f"\n{'='*100}")
    print(f"REPLAY: {replay_date.strftime('%A, %B %d, %Y')}")
    print(f"{'='*100}\n")
    
    # Load replay
    day_replay = _replay_day(replay_date)
    if day_replay is None or not day_replay.mtf_trades:
        print(f"No replay data for {replay_date}")
        return
    
    # Fetch candles for reference
    try:
        one_min, five_min, pdh, pdl, pdc = _fetch_intraday(replay_date)
        if not five_min:
            print("No candle data available")
            return
    except Exception as e:
        print(f"Error loading candles: {e}")
        return
    
    print(f"Candles: {len(five_min)} five-minute bars")
    print(f"PDH: {pdh:.2f} | PDL: {pdl:.2f} | PDC: {pdc:.2f}\n")
    
    # Display engine signals
    print("ENGINE SIGNALS")
    print("-" * 100)
    print(f"{'Time':<10} {'Family':<20} {'Dir':<8} {'Entry':<12} {'SL':<12} {'Exit':<12} {'Result':<10} {'Reason':<20}")
    print("-" * 100)
    
    total_r = 0.0
    wins = 0
    losses = 0
    
    for trade in day_replay.mtf_trades:
        time_str = format_time(trade.entry_time)
        result_str = f"{trade.result_r:+.2f}R" if trade.result_r is not None else "Open"
        
        if trade.result_r and trade.result_r > 0:
            wins += 1
        elif trade.result_r and trade.result_r < 0:
            losses += 1
        
        if trade.result_r:
            total_r += trade.result_r
        
        print(
            f"{time_str:<10} {trade.family:<20} {trade.direction:<8} "
            f"{format_price(trade.entry):<12} {format_price(trade.sl):<12} "
            f"{format_price(trade.exit_spot):<12} {result_str:<10} {trade.exit_reason:<20}"
        )
    
    print("-" * 100)
    print(f"Summary: {len(day_replay.mtf_trades)} trades | Win {wins}W/{losses}L | Total {total_r:+.2f}R")
    
    # State timeline
    print("\n[TIMELINE] STATE TIMELINE")
    print("-" * 100)
    for event in day_replay.zone_log[:30]:  # Show first 30 events
        print(f"  {event}")
    
    if len(day_replay.zone_log) > 30:
        print(f"  ... and {len(day_replay.zone_log) - 30} more events")
    
    # Near-misses (ORB signals that didn't fire)
    if day_replay.orb_signals:
        print("\n[WARN]  ORB SIGNALS (ADDITIONAL)")
        print("-" * 100)
        for sig in day_replay.orb_signals[:10]:
            print(f"  {sig}")
    
    print(f"\n{'='*100}\n")


def interactive_marking(replay_date: date):
    """Interactive mode: manually mark your entries and compare."""
    
    print(f"\n{'='*100}")
    print(f"INTERACTIVE MARKING: {replay_date.strftime('%A, %B %d, %Y')}")
    print(f"{'='*100}\n")
    
    day_replay = _replay_day(replay_date)
    if day_replay is None or not day_replay.mtf_trades:
        print(f"[WARN]  No replay data for {replay_date}")
        return
    
    # Fetch candles
    try:
        one_min, five_min, pdh, pdl, pdc = _fetch_intraday(replay_date)
        if not five_min:
            print("[WARN]  No candle data available")
            return
    except Exception as e:
        print(f"[WARN]  Error loading candles: {e}")
        return
    
    # Manual marking
    your_marks = []
    
    print("Interactive Mode: Mark candles you would enter")
    print("Commands: [t]ime [d]irection [p]rice | [s]how [c]ompare | [q]uit\n")
    
    # Show engine trades first
    print("[ENGINE] ENGINE TRADES (for reference)")
    print("-" * 80)
    for i, trade in enumerate(day_replay.mtf_trades[:5]):  # Show first 5
        time_str = format_time(trade.entry_time)
        print(f"{i}: {time_str} {trade.family} {trade.direction} @ {trade.entry:.2f}")
    print("... (see full report above)\n")
    
    # Simple CLI marking interface
    while True:
        user_input = input("Mark entry (time direction price) or [c]ompare [q]uit: ").strip()
        
        if user_input.lower() == "q":
            break
        elif user_input.lower() == "c":
            # Show comparison
            print("\n[DATA] COMPARISON: YOUR MARKS vs ENGINE SIGNALS")
            print("-" * 80)
            print(f"{'#':<5} {'Time':<10} {'Your Dir':<12} {'Your Price':<12} {'Engine Dir':<12} {'Engine Price':<12} {'Match':<10}")
            print("-" * 80)
            
            for i, mark in enumerate(your_marks):
                # Find closest engine trade
                engine_match = None
                min_time_diff = float('inf')
                
                for trade in day_replay.mtf_trades:
                    if not trade.entry_time:
                        continue
                    time_diff = abs((mark['time'] - trade.entry_time).total_seconds())
                    if time_diff < min_time_diff:
                        min_time_diff = time_diff
                        engine_match = trade
                
                engine_dir = engine_match.direction if engine_match else "—"
                engine_price = f"{engine_match.entry:.2f}" if engine_match else "—"
                match_str = "[OK]" if (engine_match and engine_match.direction == mark['direction']) else "[FAIL]"
                
                print(
                    f"{i+1:<5} {format_time(mark['time']):<10} {mark['direction']:<12} "
                    f"{mark['price']:<12} {engine_dir:<12} {engine_price:<12} {match_str:<10}"
                )
            print("-" * 80)
        else:
            # Parse mark
            parts = user_input.split()
            if len(parts) == 3:
                try:
                    time_str = parts[0]  # HH:MM
                    direction = parts[1].upper()
                    price = float(parts[2])
                    
                    if direction not in ["LONG", "SHORT"]:
                        print("[ERROR] Direction must be LONG or SHORT")
                        continue
                    
                    # Parse time
                    hour, minute = map(int, time_str.split(":"))
                    mark_time = datetime.combine(
                        replay_date,
                        datetime.min.time().replace(hour=hour, minute=minute),
                        tzinfo=settings.IST
                    )
                    
                    your_marks.append({
                        'time': mark_time,
                        'direction': direction,
                        'price': price,
                    })
                    
                    print(f"[OK] Marked {direction} @ {price:.2f} @ {time_str}")
                except ValueError as e:
                    print(f"[ERROR] Invalid input: {e}")
            else:
                print("[ERROR] Usage: time direction price (e.g., '10:30 LONG 22650')")
    
    print(f"\nSession complete. You marked {len(your_marks)} entries.\n")


def export_marks(replay_date: date, output_file: str):
    """Export engine signals to CSV."""
    
    day_replay = _replay_day(replay_date)
    if day_replay is None:
        print(f"[WARN]  No replay data for {replay_date}")
        return
    
    with open(output_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Date', 'Time', 'Family', 'Direction', 'Entry', 'SL', 'Exit', 'Result_R', 'Exit_Reason'])
        
        for trade in day_replay.mtf_trades:
            writer.writerow([
                replay_date.isoformat(),
                format_time(trade.entry_time),
                trade.family,
                trade.direction,
                format_price(trade.entry),
                format_price(trade.sl),
                format_price(trade.exit_spot),
                f"{trade.result_r:+.2f}" if trade.result_r else "Open",
                trade.exit_reason,
            ])
    
    print(f"[OK] Exported {len(day_replay.mtf_trades)} trades to {output_file}")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Interactive replay comparator: your marks vs engine signals"
    )
    parser.add_argument(
        "--date",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date.today(),
        help="Replay date (YYYY-MM-DD, default: today)"
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Interactive marking mode (vs. just showing engine signals)"
    )
    parser.add_argument(
        "--export",
        type=str,
        help="Export engine signals to CSV file"
    )
    
    args = parser.parse_args()
    
    # Display replay
    display_day_replay(args.date)
    
    # Export if requested
    if args.export:
        export_marks(args.date, args.export)
    
    # Interactive mode if requested
    if args.interactive:
        interactive_marking(args.date)


if __name__ == "__main__":
    main()
