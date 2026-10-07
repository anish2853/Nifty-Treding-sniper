# Nifty Options Alert-Only Assistant (NSE India)

Personal, **alert-only** worker bot for Nifty index options. It auto-starts each
trading day, works the day on a fixed lifecycle, pushes everything to Telegram
(the phone is the only UI), and shuts itself down. **No order placement exists
anywhere in this codebase** (Phases A-B are alert/journal only; execution is
Phase D, gated behind explicit approval and `PAPER_MODE=false`).

## Phase status

| Phase | Scope | Status |
|-------|-------|--------|
| A | Data layer, pre-market report, regime classifier, OI snapshot logger, tests | **built** |
| A+ | Daily-worker upgrade: hard lifecycle, self-restart watchdog, feed alarms, Telegram-only UI, Task Scheduler auto-start | **built** |
| B | Sniper engine: candle engine, entry/exit engines, 🎯 trade cards, paper accounting, Streamlit dashboard | **built** |
| C | Replay backtester on the journal (`--mode backtest`) | waiting for "proceed" |
| D | Angel One SmartAPI execution (SmartConnect, TOTP, ROBO orders, 15:10 square-off) | requires explicit approval |

## Phase B — how the sniper works (all frozen parameters live in `settings.py`)

- **Candle engine** (`engines/candles.py`): NSE chart API
  (`chart-databyindex-dynamic?index=NIFTY 50&type=index`, verified working; the
  legacy `chart-databyindex` returns an empty shell) polled every 60 s → 5-min OHLC
  (`PO` pre-open ticks excluded; NSE timestamps are IST wall time encoded as UTC
  epoch). Fallback: spot samples bucketed into 5-min windows. Mid-day start: the
  first refresh backfills the whole day, so ORB (09:15–09:30 H/L) and the day's
  opening print are available immediately — a 12:46 start works, and the 09:15
  regime uses the STORED opening gap, never the current spot.
- **Entry engine** (`engines/entry.py`): evaluates BOTH directions on every 5-min
  candle CLOSE inside 09:30–11:00. Frozen score: +20 level break (close beyond ORB
  high/PDH or ORB low/PDL), +20 confirmation (prior candle also beyond), +20 OI
  (writers covering at the 3 nearest strikes beyond spot over the last 3 snapshots —
  any RISING change-in-OI = REJECT outright), +15 volume (MISSING ok → 0 pts; index
  volume is usually unavailable), +15 regime+VIX+event gates, +10 window.
  FIRE at score ≥ 80 + regime allows + caps (max 2/day, 1/direction). 55–79 →
  journaled near-misses (grey dots on the dashboard).
- **Trade card** (exact format): strike = spot rounded to 50, LTP from the live
  chain, entry band LTP–LTP+10, SL spot = ORB mid, SL prem = 0.75×LTP, TGT 1 =
  spot ±1R → prem ≈ LTP+0.5×1R → BOOK 50%, TGT 2 = ±2R → EXIT rest,
  MAX RISK = 0.25×LTP×75, exit by 15:10. 1 lot = 75.
- **Exit engine** (`engines/exit.py`), priority order: candle close beyond SL spot →
  premium SL (LTP ≤ 75% of entry) → thesis dead (close back inside the level) → OI
  flip (fresh writing beyond the direction) → VIX spike >8% → 75-min time stop →
  runner +2R; plus +1R BOOK-50% (SL to breakeven, re-armed triggers), opposite
  signal ≥80, and the 15:10 forced square-off. Every 🚨 shows reason + P&L +
  amount saved vs hard SL.
- **Paper accounting**: entry premium = chain LTP at signal; mark-to-market every
  chain poll; result_R = blended premium points / (0.25 × entry premium) — the half
  booked at +1R is included. Everything lands in the journal (`SIGNAL`/`EXIT` rows
  with `trade_id`, `result_r`, `pnl_rupees`) and flows into the EOD summary.
- **Crash recovery**: open paper trades are rebuilt from the journal on watchdog
  restarts (SIGNAL rows without EXIT; 1R rows re-arm the runner at the booked
  premium), so exits keep working after a crash.
- **Dashboard**: `streamlit run dashboard/app.py` — candlestick with 🎯 on signal
  candles, ✕ on exits (reason on hover), grey near-miss dots, ORB box, PDH/PDL,
  max call/put-OI walls, open trades + running stats; refreshes every 60 s.

## Phase B+ — liquidity map + sweep-fade (second setup family)

- **Liquidity map** (`engines/liquidity.py`), recomputed on every chain poll:
  S1–S3 = top-3 PUT-OI strikes below spot, R1–R3 = top-3 CALL-OI strikes above
  spot (nearest expiry), each with a change-in-OI arrow (↑ writing / ↓ unwinding /
  → flat) and distance from spot. A level is **STRONG** when it sits within 0.15%
  of PDH/PDL (`STRONG_LEVEL_BAND_PCT`). PDH/PDL are shown as liquidity pools. The
  ladder ships inside the 🌤 regime message and on the dashboard (lines + expander).
- **Sweep-fade** (`engines/sweep.py`): price TRADES through a STRONG level (or pool)
  but the 5-min candle CLOSES back inside. Frozen score: +20 sweep, +20 rejection
  (close back inside by >25% of the candle range), +20 OI accelerating into the
  sweep (writers defending that strike — the OPPOSITE polarity of the breakout OI
  rule), +15 volume spike, +15 regime allows counter-direction, +10 window
  09:30–11:00 **or 13:30–14:45**. FIRE ≥ 80: BUY PE after a failed UP-side sweep,
  BUY CE after a failed DOWN-side sweep. **SL = sweep extreme ± 5 pts.** Same exit
  engine, same 1R/partial-book/runner mechanics, and the daily 2-trade cap is
  SHARED with breakout signals (verified: the second family signal is blocked and
  journaled). Documented interpretation: mean-reversion regime does NOT block
  sweeps (Module 2 suppresses *breakout* entries; a fade IS the mean-reversion
  trade) — LOW-CONFIDENCE, VIX spike and event days still block everything.
- **Family tagging** (`family` journal column): every SIGNAL/EXIT row is tagged
  BREAKOUT or SWEEP-FADE. The 📕 EOD summary gains a **BY FAMILY** block (trades,
  total R, expectancy, win rate per family) so we can see which setup carries the
  other; the dashboard shows the same split.
- **Mode safety**: `--mode now` between 09:15 and 15:30 prints and Telegrams
  "⚠️ TEST MODE during market hours — use --mode live for monitoring".
- **VIX fix**: the 09:15 regime classification now applies the live NSE VIX quote
  immediately (the 🌤 line used to show MISSING until a later cycle).

## Single source of truth — MarketDataStore (data-integrity hotfix)

After two data-integrity incidents (a synthetic signal reaching Telegram, and
mixed fixture/live values across modules), **all market data flows through one
store** (`data_sources/market_data.py`):

- **One fetch pass per 60 s cycle**: 5-min candles (chart API), option chain
  (nearest + NEXT expiry only), India VIX — fetched once, validated, stored with
  timestamp + source. PDC/PDH/PDL come from the pre-market yfinance fetch,
  stored once and reused all day.
- **Validation on every fetch**: symbol must be NIFTY, every strike a multiple
  of 50, `underlyingValue` within ±3% of the stored PDC — otherwise the chain is
  rejected for that cycle (and feed alarms escalate after 5 straight failures).
- **One read path**: regime, liquidity map, entry, exit, sanity gates, trade
  cards and EOD read only from the store; the liquidity map only sees strikes
  within ±2% of the stored spot (`LIQUIDITY_STRIKE_BAND_PCT`).
- **One Telegram choke point** (`DaySession._notify`): non-live data sources
  never transmit — tests use `NullTelegramSender`, so synthetic data can never
  reach a real chat again.
- **Startup guarantee**: if PDC is unavailable after 3 retries, the bot declares
  NO-TRADE DAY and stands down silently — a silent bot beats a wrong bot.
- **Visible provenance**: every cycle logs and every card prints
  `spot 22,612 | PDC 22,555 | store 10:19:12 IST`; `--mode score` prints one
  consistent set from a single store pass. A meta-test
  (`test_no_hardcoded_market_values_in_production`) fails the build if any
  market literal reappears in production code.

## Freshness gate, graded exits, post-exit shadow (integrity hotfixes)

- **Freshness gate**: ONLY the latest closed 5-min candle can generate a signal.
  Startup backlogs are discarded unprocessed (journaled, never evaluated);
  superseded candles are logged STALE; any candle older than 6 minutes
  (`STALE_CANDLE_MAX_AGE_SEC`) is never fired; a setup whose candle close drifted
  >0.25% from the store spot (`SPOT_STALENESS_BAND_PCT`) is stale; and the
  candle-close AND the actual fire moment must both sit inside an entry window
  (09:30–11:00 or 13:30–14:45). Stale discards never consume the daily cap. The
  trade card prints ONE spot — the store's live spot.
- **Graded runner protocol**: every exit trigger is graded by the OI context at
  the trade's side: **OI-STRONG** (fresh writing against the position) → full
  exit; **OI-MODERATE** (neutral) → book 50%, runner at breakeven;
  **WALL-UNWINDING** (writers covering — the wall is dissolving) → trigger
  suppressed, targets re-armed from the current spot at the same 1R distance, and
  ONE re-entry granted for that direction (consumed on use). Hard triggers (VIX
  spike, 75-min time stop, +2R, 15:10 square-off, opposite signal) always exit
  fully. Every 🚨 carries its grade tag: `🚨 EXIT [OI-MODERATE] — …`.
- **Post-exit shadow**: every EXIT row stores the original plan (entry SL +
  original targets). At EOD the original plan is re-simulated on stored candles
  from the exit moment to 15:10 (frozen `PREMIUM_BETA` premium model,
  SL-first-within-candle pessimism) and reported per trade as **EXIT SAVED**
  (₹ protected) vs **EXIT COST** (profit missed), with running totals split by
  exit type: hard-SL / thesis / OI-strong / OI-moderate / time. After 60 trades
  this says exactly which exit rule to tune — measured, never guessed.

## Trend-Day Engine v2 — one brain (ORB + liquidity + sweep-fade + VWAP + structure)

- **Day-state engine** (`engines/day_state.py`), computed every cycle from the
  stored candles: VWAP (volume-weighted; typical-price cumulative average tagged
  **VWAP-proxy** when index volume is unavailable), EMA-20, the first 5-min close
  beyond PDH/PDL (**liquidity break**), ratcheting structure (higher lows /
  lower highs), and the classification: **TREND-UP** (PDH broken + closes holding
  + spot > VWAP), **TREND-DOWN** (mirror), **RANGE** otherwise. A structure-
  breaking close re-evaluates the state.
- **Entry permissions**: TREND state → entries 09:30–14:30 while structure holds;
  RANGE → sweep-fade at extremes only, breakout entries off. The clock governs
  only the FIRST attempt at PDH/PDL — after a liquidity break STRUCTURE governs.
  **No new entries after 14:30.** Square-off 15:10 unchanged.
- **Bonus scoring** (+20 max on top of the existing 100, threshold still ≥ 80):
  +10 level confluence (ORB high/low within 0.15% of PDH/PDL), +10 VWAP
  alignment (long above / short below).
- **Trend Pullback** (`engines/pullback.py`, family `PULLBACK`): trend days only
  — pullback to VWAP or the broken level with a 5-min reversal close off it.
  Scoring: level +20, rejection +20, OI no-fresh-writing +20, VWAP/structure
  hold +15, trend intact +15, volume +10 (max 100, fire ≥ 80). SL = level ± 5.
- **OI-smart exits** ("OI whispers, price decides"): OI flip ALONE books 50% and
  moves the runner trail to **max(broken level, last higher low)** — not premium
  breakeven. Full exit only on: flip + close back inside, second flip within
  30 min, structure break (close beyond the trail), or hard SL. +1R books 50%
  and the runner trails structure. Wall unwind re-arms targets + one re-entry.
  Time stop applies ONLY in RANGE state — trend runners may hold to 15:10.
- **Frozen risk rails**: max 2 trades/day (+1 re-entry per direction on wall
  unwind); daily loss cap 5% → halt; **NEW profit lock — day reaches +2R →
  banked, stop trading**; paper mode; freshness + sanity gates unchanged.
- **Journal**: every row tagged with setup (`ORB` / `LIQ-BREAK` / `SWEEP` /
  `PULLBACK`) and day state (`TREND-UP` / `TREND-DOWN` / `RANGE`); EOD splits
  stats **BY SETUP** and **BY DAY STATE** alongside the exit-shadow buckets.

## Setup

1. Python 3.11+ (developed on 3.12).

   ```bash
   cd C:\Users\anish\Desktop\trading
   python -m venv .venv
   .venv\Scripts\activate          # Git Bash: source .venv/Scripts/activate
   pip install -r requirements.txt
   ```

2. Credentials live in `config\.env` (gitignored): `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`, optional `CAPITAL`. Without Telegram the bot runs
   console-only and logs every send skip.

3. Tests (offline, no network):

   ```bash
   python -m pytest tests -q
   ```

4. Modes:

   ```bash
   python main.py                # --mode live (default): the scheduled day
   python main.py --mode now     # immediate end-to-end test run, ignores schedule
   python main.py --mode backtest# reserved for Phase C (prints a note, exits)
   python main.py --mode chain-test  # one raw chain poll: HTTP status, response
                                     # length, first 500 raw chars - handshake debug
   ```

# Daily Operation

## What arrives on your phone, and when (all IST)

| Time | Message | Content |
|------|---------|---------|
| 08:45 | 📊 **PRE-MARKET** | US closes, crude, USD/INR, VIX, FII/DII (with its data date), GIFT Nifty, previous-session closing PCR, expected gap vs PDC, preliminary regime verdict |
| ~09:15 | 🌤 **REGIME** | classification from the first spot print: gap band, PCR tilt, VIX; later 🌤 REGIME CHANGE if VIX spikes >8% intraday |
| 09:30-15:10 | 🎯 **SIGNAL** / 🚨 **EXIT** | trade alerts activate in **Phase B** (full reason stack + score + SL level; exit reason + P&L). Until then this window is data-only |
| any time | ⚠️ **DATA FEED** | a source failed 5x in a row — bot continues with that factor MISSING (one alert per outage) |
| any time | ⚠️ **ERROR** | the day loop crashed: short error + restart counter; full traceback goes to the log file |
| 15:10 | ⏹ **SQUARE-OFF** | only if a paper trade is still open (Phase B); otherwise silent, journaled |
| 15:40 | 📕 **EOD SUMMARY** | today's paper trades (direction, entry, exit, R, exit reason), day total (R and Rs), running totals (trades, win rate, expectancy, streak, max drawdown), today's regime, data-quality notes |
| 15:45 | 🔴 **SHUTDOWN** | confirmation of the clean exit (SQLite flushed, sessions closed) |

A normal Phase A day is 4 Telegram messages: 📊 08:45 → 🌤 09:15 → 📕 15:40 → 🔴 15:45.

## Built-in resilience

- **Self-restart watchdog**: crash → full traceback in the log, short ⚠️ ERROR on
  Telegram, 60 s wait, the day restarts *in place* — journal dedup means no double
  pre-market/EOD messages, and the regime state is recovered from the journal
  instead of mis-classifying from the current spot. Max **5 restarts/day**, then
  🔴 SHUTDOWN + exit(1).
- **Feed alarms**: option chain, India VIX, yfinance, FII/DII, GIFT Nifty are
  tracked individually; 5 consecutive failures → one ⚠️ DATA FEED alert (chain and
  VIX alarms include the last HTTP status), and the pipeline keeps running with
  that factor MISSING. Per-source OK/missing counts appear in the 📕 data-quality
  block. At 08:45 the chain is retried 5× (5 s apart); if it can't be fetched
  pre-open the PCR line reads "awaiting market open" instead of MISSING.
- **Silent guards**: on weekends and `config/holidays.json` dates the bot exits(0)
  immediately at startup with no Telegram output (holiday is journaled). Event-
  calendar days keep logging data but suppress trade/report alerts.

## Windows auto-start (Task Scheduler) — click-by-click

1. Press **Win + R**, type `taskschd.msc`, press Enter.
2. In the right-hand pane click **Create Task…** (not "Create Basic Task").
3. **General** tab: Name: `Nifty Alert Bot`. Select **Run only when user is logged
   on** (simplest; no password stored). (Alternative: *Run whether user is logged
   on or not* runs it headless — Windows will store your password.)
4. **Triggers** tab → **New…**: *Weekly*; tick **Monday–Friday**; start time
   **08:30**; tick Enabled. Under Advanced settings tick **Run task as soon as
   possible after a scheduled start is missed** (catch-up if the laptop was asleep
   at 08:30 — the bot handles a late start gracefully).
5. **Actions** tab → **New…**: Action *Start a program*; Program/script:
   `C:\Users\anish\Desktop\trading\start_bot.bat`; **Start in (optional)**:
   `C:\Users\anish\Desktop\trading`.
6. **Conditions** tab: **untick** *Start the task only if the computer is on AC
   power* and *Stop if the computer switches to battery power* (it's a laptop).
   Optionally tick *Wake the computer to run this task* if you sleep it overnight
   (this cannot power on a shut-down laptop).
7. **Settings** tab: tick *Allow task to be run on demand*; set *If the task is
   already running* → *Do not start a new instance* (the watchdog handles restarts
   in-process); leave *Stop the task if it runs longer than* at its default (72 h).
8. **OK**. Right-click the task → **Run** to prove the launch path works (before
   08:45 the bot just waits; to exercise every message type immediately run
   `python main.py --mode now` once).

`start_bot.bat` (project root) changes to its own folder, prefers the project
venv, sets UTF-8 for redirected output, and appends to `logs\bot_<date>.log`
(locale-safe date, because `%DATE%` can contain slashes on some Windows locales —
that's the only change from the plain `bot_%DATE%.log` name). The trigger uses the
laptop's local clock, so keep the machine on IST.

## Linux / macOS (cron)

`crontab -e` (`%` must be escaped in cron):

```cron
30 8 * * 1-5 cd /path/to/trading && python3 main.py --mode live >> logs/bot_$(date +\%F).log 2>&1
```

cron does not catch up missed runs (use anacron if the machine may be off at
08:30). The bot itself still guards: weekends and holiday dates exit(0) silently.

## Data artifacts

- `data/journal.sqlite3` — `journal` (SIGNAL/EXIT/REGIME/SKIP with reasons JSON,
  `result_r`, `trade_id`, `pnl_rupees`), `oi_snapshots` (spot, PCR all-expiries +
  nearest, max-OI strikes, VIX), `oi_strike_snapshots` (per-strike OI/change-in-OI/
  LTP/IV/volume + snapshot deltas — what Phase C replays).
- `data/exports/YYYY-MM-DD_{journal,oi_snapshots,oi_strike_snapshots}.csv`
  (manual re-export: `python -m journal.store --date 2026-10-05`).
- `logs/trading_assistant.log` — rotating 5 MB × 5; `logs/bot_<date>.log` — the
  Task Scheduler redirect from `start_bot.bat`.

## Configuration

- **All numeric thresholds** — including the day-lifecycle times (08:45 / 09:15 /
  09:30 / 15:10 / 15:40 / 15:45), watchdog limits (5 restarts, 60 s, 5-failure
  alarm) and Telegram retries (3) — live in `settings.py` with the spec line
  quoted. Nothing is buried in module code.
- `config/holidays.json` — NSE 2026 trading holidays pre-populated; **verify
  against nseindia.com → Resources → Holiday List** each quarter.
- `config/event_calendar.json` — your NO-TRADE dates (silent: Telegram suppressed,
  journaled; data logging continues).
- `config/.env` — credentials only (gitignored, never hardcoded).

## Spec-left-open points — how they were resolved (one-line settings changes)

1. **Gap 0.3%-0.6% band**: no restriction specified → NORMAL with a journal note
   (`GAP_NORMAL_ABS` / `GAP_RESTRICTION_ABS`).
2. **"US close direction"** = sign of the S&P 500's last completed session.
3. **PCR / max-OI strikes**: computed across the merged rows of the nearest
   `NSE_CHAIN_MAX_EXPIRIES` (default 4) expiries. Originally the spec's "total PCR"
   summed all expiries in one payload — but NSE retired that API (see
   Troubleshooting); the replacement serves ONE expiry per call, so the merge scope
   is now this setting (set it higher to widen the PCR universe).
4. **"2-3 chain snapshots"** for OI confirmation → pinned to 3
   (`OI_CONFIRM_SNAPSHOTS`, used from Phase B).
5. **SL drop "25-30%"** and **time stop "60-75 min"** stored as bands; Phase B pins
   a value inside each with your sign-off.
6. **GIFT Nifty**: no documented public JSON API on nseix.com (verified Oct 2026);
   tries `settings.GIFT_NIFTY_SOURCES`, reports MISSING otherwise.
7. **EOD "Rs" totals** come from `pnl_rupees` on EXIT rows, which Phase B's paper
   accounting fills (position sizing is defined there); until then the EOD shows
   R totals and `Rs +0.00`.
8. **Snapshot window**: OI snapshots run 09:15-15:30 (Prompt 1's market-hours
   polling); the 09:30-15:10 window is where Phase B's signal/exit evaluation
   hooks in. The 08:45 pre-market fetch is sanctioned by Module 1.

## Troubleshooting

- **NSE HTTP 401/403 or non-JSON**: cookies expired or Akamai block — the session
  re-runs the browser-header handshake and retries with backoff automatically.
- **NSE HTTP 404 on the chain** (diagnosed Oct 2026): NSE retired
  `/api/option-chain-indices` — it returns a "Resource not found" page even with a
  perfect cookie handshake. The working contract (recovered from NSE's own
  option-chain page JS) is now used automatically:
  1. `GET /api/option-chain-contract-info?symbol=NIFTY` → `expiryDates`,
  2. `GET /api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=<e>` per expiry —
     a bare v3 call without `&expiry=` returns `{}`.
  The nearest `NSE_CHAIN_MAX_EXPIRIES` expiries are merged into one snapshot; the
  legacy endpoints are still probed as a last resort and the fetch method is sticky
  once found. If NSE changes it again, run `python main.py --mode chain-test`
  (prints URL + headers + body head for every step), grab the real request URL
  from Chrome DevTools → Network, and put it in `config/.env` as
  `NSE_CHAIN_ENDPOINT=...` — it is tried first and its `expiry=` value is
  auto-templated. Five consecutive market-hours failures raise one ⚠️ DATA FEED
  Telegram alert carrying the exact per-endpoint HTTP codes; the bot keeps running.
- **No Telegram messages**: check `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in
  `config\.env`; send failures retry 3x then log to file, never crash. If the
  token was ever pasted into a source file, revoke it via @BotFather (/revoke).
- **yfinance empty/rate-limited**: factors go MISSING; `premarket_report.py`
  rerun usually fixes it. PDH/PDL/PDC always use the last *completed* session.
- **Windows `zoneinfo` errors**: keep `tzdata` installed (in requirements).
- **Laptop was off at 08:30**: with "run as soon as possible after a missed start"
  ticked, the bot starts late and skips straight to whatever the clock says.

## File map

```
main.py                  daily-worker orchestration + watchdog + modes (Phase B glue)
premarket_report.py      Module 1 factor engine report (📊)
settings.py              every numeric threshold + lifecycle times (frozen)
utils.py                 IST clock, logging, config loaders, safe_print
watchdog.py              FeedHealth: 5-failure feed alarms + EOD quality counts
data_sources/nse_session.py     shared rate-limited NSE session (3 s, cookies, backoff)
data_sources/nse_chain.py       option chain fetch (contract-info -> v3 flow) + parser
data_sources/global_factors.py  yfinance factors (PDH/PDL/PDC, US, crude, USD/INR, VIX)
data_sources/nse_quote.py       NSE quote API: India VIX, FII/DII, GIFT Nifty
engines/candles.py       5-min OHLC: chart API + spot-sample fallback + backfill + ORB
engines/entry.py         frozen confluence scoring on candle close (Phase B)
engines/exit.py          a-i exit triggers + runner re-arm (Phase B)
engines/paper.py         PaperTrade: levels, 1R/2R touches, blended R accounting
engines/oi_engine.py     snapshot diffing, fresh-writing vs unwinding, OI windows
engines/regime.py        Module 2 classifier (pure logic) + crash-state recovery
journal/store.py         SQLite + dedup/state/trade recovery + CSV export
journal/stats.py         EOD accounting (trades, R/Rs, win rate, expectancy, DD)
alerts/messages.py       the 8 emoji message constructors + 🎯 trade card
alerts/telegram_bot.py   plain Bot API POST, retry 3x, never crashes
risk/manager.py          risk flags scaffold (wires into Phase C/D)
dashboard/app.py         Streamlit paper dashboard (60 s refresh)
tests/                   fixtures + parser/regime/stats/watchdog/sniper unit tests
start_bot.bat            Task Scheduler launcher
```
"# Nifty-Treding-sniper" 
