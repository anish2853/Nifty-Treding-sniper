# Quick Reference - Nifty Trading Bot

## System Status
✅ **PRODUCTION READY** | All tests passing | All replay tools working | Windows compatible

---

## What You Have

### Live Trading (Phase B)
- **Start**: `python main.py` or scheduled via Windows Task Scheduler (08:45 IST)
- **Features**: Breakout + sweep-fade entries, 10-priority exits, paper accounting, Telegram alerts
- **Output**: 7 message types to Telegram (pre-market, regime, signals, exits, EOD, errors, shutdown)

### Historical Replay (Phase C)
Three tools, same engine, different interfaces:

| Tool | Command | Use Case |
|------|---------|----------|
| **Batch Mode** | `python main.py --mode replay60 --days 7` | Backtest 7 days, generate report |
| **CLI Comparator** | `python replay_compare.py --date 2026-10-06` | View trades + state timeline |
| **Streamlit Viewer** | `streamlit run dashboard/replay_viewer.py` | Animated playback, click-to-mark |

### Live Dashboard
- **Start**: `streamlit run dashboard/app.py`
- **Features**: Live candles, signal markers, liquidity ladder, state monitor, 60-sec refresh

---

## Key Files

```
main.py                          # Entry point: modes live/now/replay60/chain-test/score
engines/
  ├─ entry.py                    # Breakout + sweep-fade scoring (frozen weights)
  ├─ exit.py                     # 10-priority exit triggers
  ├─ paper.py                    # Paper accounting, R calculation
  ├─ day_state.py                # TREND/RANGE state machine
  ├─ replay_mtf.py               # Replay engine core
  ├─ replay_60day.py             # 60-day batch runner
  ├─ stats_calculator.py         # Report generation
  └─ replay_scoring.py           # Price-only scoring (52/65 threshold)
replay_compare.py                # CLI comparator
dashboard/
  ├─ app.py                      # Live dashboard
  └─ replay_viewer.py            # Streamlit interactive replay
journal/store.py                 # SQLite journal + dedup
settings.py                      # All thresholds (frozen)
README.md                        # Main documentation
SYSTEM_FLOW.md                   # Architecture + strategies
STATUS.md                        # System status
VERIFICATION_REPORT.md           # Test results
```

---

## Today's Fixes

### ✅ TelegramSender UnboundLocalError
- **Problem**: `--mode live` failed with UnboundLocalError
- **Fix**: Removed local import causing scope shadowing
- **Status**: RESOLVED

### ✅ Windows Unicode Encoding Errors
- **Problem**: Emoji characters (→, ✓) crashed on Windows cmd
- **Fix**: Replaced with ASCII equivalents (to, [OK])
- **Status**: RESOLVED

---

## Testing (All Pass)

```bash
python -m pytest tests -q          # 132/132 ✅

python main.py --mode replay60 --days 1    # Report generated ✅

python replay_compare.py --date 2026-10-06 # 5 trades shown ✅

python main.py --mode chain-test   # NSE handshake OK ✅
```

---

## Common Commands

```bash
# Live trading (scheduled)
python main.py --mode live

# Live trading (immediate test)
python main.py --mode now

# Replay 60 days
python main.py --mode replay60 --days 60

# Replay 7 days
python main.py --mode replay60 --days 7

# CLI: View Oct 6 trades
python replay_compare.py --date 2026-10-06

# CLI: Export to CSV
python replay_compare.py --date 2026-10-06 --export report.csv

# Interactive marking (record your trades vs engine)
python replay_compare.py --date 2026-10-06 --interactive

# Dashboard: Live chart
streamlit run dashboard/app.py

# Dashboard: Replay playback
streamlit run dashboard/replay_viewer.py

# Diagnostics: Chain API flow
python main.py --mode chain-test

# Diagnostics: Single snapshot scoring
python main.py --mode score

# Audit: Why trades didn't fire
python main.py --mode audit
```

---

## Key Numbers (Frozen Parameters)

| Parameter | Value | Notes |
|-----------|-------|-------|
| Entry Score to Fire | ≥80 | out of 100 |
| Price-Only Threshold | 52/65 | replay mode (OI/volume excluded) |
| Max Trades/Day | 2 | 1 per direction |
| Daily Loss Cap | 5% | of capital |
| Time Stop (Range) | 75 min | RANGE state only |
| Entry Window | 09:30-11:00 | + 13:30-14:45 for sweeps |
| Square-Off | 15:10 | forced |
| Market Hours | 09:15-15:30 | IST |
| Cost Deduction | ₹150/trade | applied in replay reports |

---

## Telegram Messages

**Scheduled Delivery** (IST):
- 08:45: 📊 PRE-MARKET (factors: US, crude, VIX, FII/DII)
- 09:15: 🌤 REGIME (gap, PCR, classification)
- 09:30-15:10: 🎯 SIGNAL / 🚨 EXIT (trades + reasons)
- Any time: ⚠️ DATA FEED (source failures)
- Any time: ⚠️ ERROR (crashes)
- 15:10: ⏹ SQUARE-OFF (if trade open)
- 15:40: 📕 EOD SUMMARY (stats + P&L)
- 15:45: 🔴 SHUTDOWN (confirmation)

---

## Data Files

```
data/
  ├─ journal.sqlite3             # Live trading journal
  ├─ replay_journal.sqlite3      # Replay backtesting journal
  └─ exports/
      ├─ 2026-10-06_journal.csv
      ├─ 2026-10-06_oi_snapshots.csv
      └─ 2026-10-06_oi_strike_snapshots.csv

backtest_results/
  └─ replay60_report.md          # Latest replay report (markdown)

logs/
  ├─ trading_assistant.log       # Rotating 5MB logs
  └─ bot_2026-10-06.log          # Task Scheduler output

config/
  ├─ .env                        # Credentials (gitignored)
  ├─ holidays.json               # Trading holidays
  └─ event_calendar.json         # Your no-trade dates
```

---

## Setup Checklist

### Before First Run

- [ ] Python 3.11+ installed
- [ ] `cd /path/to/trading && python -m venv .venv`
- [ ] `.venv\Scripts\activate` (Windows)
- [ ] `pip install -r requirements.txt`
- [ ] Create `config/.env` with:
  ```
  TELEGRAM_BOT_TOKEN=<token>
  TELEGRAM_CHAT_ID=<chat_id>
  CAPITAL=100000
  ```
- [ ] Verify holidays: `config/holidays.json`
- [ ] Run tests: `python -m pytest tests -q` (should see 132 passed)

### For Scheduled Execution (Windows Task Scheduler)

- [ ] `start_bot.bat` exists in project root
- [ ] Create task with trigger: Mon-Fri, 08:30 IST
- [ ] "Run as soon as possible after missed start" ticked
- [ ] Untick AC power restrictions
- [ ] Right-click task → Run (test)

---

## Performance

- **Memory**: <100 MB (SQLite + caching)
- **CPU**: Low (60-second loop)
- **Disk**: <500 MB total (logs + journals)
- **Network**: 3-5 Mbps stable (NSE + Telegram)

### Timing
- NSE chain fetch: 3-5 sec
- Candle calc: <100 ms
- Entry score: <50 ms
- Full cycle: 8-12 sec (60-sec window)

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| No Telegram messages | Check `config/.env` tokens; test with `python main.py --mode chain-test` |
| Tests fail | Run `pip install -r requirements.txt` again; check Python 3.11+ |
| yfinance empty | Wait for market hours (09:15+); rerun `premarket_report.py` next day |
| NSE HTTP 404 | Automatic fallback; if persistent, set `NSE_CHAIN_ENDPOINT` in `.env` |
| Laptop off at 08:30 | Task Scheduler catches up if "run as soon as possible" is ticked |
| Unicode errors on Windows | Already fixed; use latest code |

---

## Next Steps (Optional)

### Phase D (Order Placement)
- Integrate Angel One SmartAPI
- Implement TOTP authentication
- Add 15:10 automatic square-off
- Requires explicit approval to enable

### Enhanced Analytics
- Weekly/monthly P&L tracking
- Equity curve visualization
- Sharpe ratio calculation
- Max DD recovery analysis

### Strategy Extensions
- Additional setup families
- Market-regime-dependent weights
- Correlation hedges
- Dynamic position sizing

---

## Support

- **Live Issues**: Check `logs/trading_assistant.log` (full error traces)
- **Replay Issues**: Run `python main.py --mode chain-test` to diagnose
- **Dashboard Issues**: Run `streamlit run dashboard/app.py` in new terminal
- **Understanding Flow**: Read `SYSTEM_FLOW.md` (complete architecture)

---

**Last Updated**: October 7, 2026 | **Status**: PRODUCTION READY ✅

