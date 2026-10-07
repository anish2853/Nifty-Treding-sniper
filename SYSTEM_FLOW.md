# System Flow & Trading Strategy Guide

## Overview

This document explains how the trading bot works RIGHT NOW - the complete flow from data collection to trade execution, and the strategies it uses to generate trading signals.

---

## Daily Trading Timeline (IST)

```
08:45  ┌─ PRE-MARKET REPORT
       │  └─ Fetch global factors (US close, crude, USD/INR, VIX, FII/DII, GIFT Nifty)
       │     Print: 📊 PRE-MARKET message to Telegram
       │
09:15  ├─ MARKET OPEN
       │  ├─ Fetch first option chain snapshot
       │  ├─ Compute regime: GAP classification, PCR, VIX
       │  ├─ Log regime decision
       │  └─ Print: 🌤 REGIME message to Telegram
       │
09:30  ├─ ENTRY WINDOW OPENS
       │  ├─ Start 60-second main loop (runs every minute)
       │  ├─ Build 5-minute candles from NSE chart API
       │  ├─ Evaluate entry engine (breakout + sweep-fade)
       │  ├─ Fire signals if score >= 80 + gates pass
       │  └─ Print: 🎯 SIGNAL to Telegram when entry fires
       │
11:00  ├─ ENTRY WINDOW ENDS (but sweeps continue until 14:45)
       │  ├─ No new breakout entries after this time
       │  └─ Sweep-fade entries allowed until 14:45
       │
15:10  ├─ FORCED SQUARE-OFF
       │  ├─ Any open paper trade is exited at this time
       │  └─ Print: ⏹ SQUARE-OFF message
       │
15:30  ├─ MARKET CLOSE
       │  └─ Stop polling candles
       │
15:40  ├─ EOD SUMMARY
       │  ├─ Calculate daily stats (trades, win rate, P&L)
       │  ├─ Report by family, by state, cost impact
       │  └─ Print: 📕 EOD SUMMARY to Telegram
       │
15:45  └─ SHUTDOWN
          ├─ Flush SQLite journal
          ├─ Close all sessions
          └─ Exit cleanly (exit code 0)
```

---

## Main Loop (Every 60 Seconds, 09:30-15:30)

```
START 60-second cycle
  │
  ├─ FETCH DATA
  │  ├─ Poll NSE 5-min candle chart (chart-databyindex-dynamic)
  │  ├─ Poll option chain (contract-info + option-chain-v3)
  │  ├─ Fetch India VIX
  │  └─ Validate all data (symbol, strikes, PDC band) ──► REJECT if bad
  │
  ├─ UPDATE STATE
  │  ├─ Store new candles + chain + VIX (single source of truth)
  │  ├─ Rebuild liquidity map (top-3 OI strikes above/below spot)
  │  ├─ Recompute day state: TREND-UP / TREND-DOWN / RANGE
  │  └─ Update VWAP + EMA-20
  │
  ├─ EVALUATE ENTRIES (on every 5-min candle CLOSE)
  │  ├─ Entry Engine runs BOTH directions (LONG + SHORT)
  │  ├─ Score each direction (frozen weights below)
  │  │  │
  │  │  ├─ BREAKOUT Signal Path
  │  │  │  ├─ Score: level break (+20) + confirmation (+20) + OI (+20) + 
  │  │  │  │         volume (+15) + regime+VIX (+15) + time window (+10)
  │  │  │  ├─ FIRE if: score >= 80 AND regime allows AND caps OK (max 2/day, 1/dir)
  │  │  │  └─ Family tagged: BREAKOUT
  │  │  │
  │  │  └─ SWEEP-FADE Signal Path
  │  │     ├─ Score: sweep (+20) + rejection (+20) + OI accelerating (+20) + 
  │  │     │         volume spike (+15) + regime allows (+15) + window 09:30-11:00 or 13:30-14:45 (+10)
  │  │     ├─ FIRE if: score >= 80 AND regime allows AND caps OK (shared with BREAKOUT)
  │  │     ├─ Condition: price breaks level but 5-min candle closes back inside
  │  │     └─ Family tagged: SWEEP-FADE
  │  │
  │  └─ Blocked signals (score >= 55 but < 80): journaled as near-misses
  │
  ├─ MANAGE OPEN TRADES
  │  ├─ Check every open paper trade for exits (priority order):
  │  │  ├─ 1. Candle close beyond SL spot (hard SL)
  │  │  ├─ 2. Premium SL hit (LTP <= 75% of entry)
  │  │  ├─ 3. Thesis dead (close back inside level)
  │  │  ├─ 4. OI flip (writers covering - opposite polarity)
  │  │  ├─ 5. VIX spike > 8% above prior close
  │  │  ├─ 6. 75-minute time stop (RANGE state only; trend runners trail structure)
  │  │  ├─ 7. +1R touch (book 50%, move SL to breakeven, re-arm runner at +2R)
  │  │  ├─ 8. +2R touch (exit rest)
  │  │  ├─ 9. Opposite signal >= 80 (close out, reverse if wanted)
  │  │  └─ 10. 15:10 forced square-off
  │  │
  │  └─ Print: 🚨 EXIT when trade closes (reason + P&L)
  │
  ├─ LOG & JOURNAL
  │  ├─ SQLite journal: every SIGNAL + EXIT row with
  │  │  ├─ family (BREAKOUT / SWEEP-FADE / PULLBACK)
  │  │  ├─ day_state (TREND-UP / TREND-DOWN / RANGE)
  │  │  ├─ direction (LONG / SHORT)
  │  │  ├─ entry_time, entry_price, exit_time, exit_price
  │  │  ├─ result_r (R-value: blended premium points / max risk)
  │  │  ├─ pnl_rupees (actual rupees P&L)
  │  │  └─ reason (scoring details, gate rejections, exit triggers)
  │  │
  │  └─ Separate journal for Telegram alerts: non-LIVE modes can never transmit
  │
  └─ WAIT for next cycle (60 seconds)
```

---

## Trading Strategies (3 Families)

### Strategy 1: BREAKOUT (Primary)

**When it fires**:
- Market breaks above ORB high (or PDH) → BUY CALL
- Market breaks below ORB low (or PDL) → BUY PUT

**Entry Logic**:
```
Score = 0
  + 20  if candle CLOSES beyond ORB/PDH/PDL (level break)
  + 20  if PRIOR candle ALSO closed beyond (confirmation)
  + 20  if call OI (for LONG) or put OI (for SHORT) just above spot 
         is FALLING over last 3 snapshots (writers covering = weakness)
  + 15  if breakout candle volume > 10-candle average (conviction)
  + 15  if regime allows this direction + VIX stable (< 8% spike)
  + 10  if candle close inside 09:30-11:00 window (time decay works for us)
  ─────
  MAX = 100 (if all conditions met)

FIRE if: Score >= 80 AND (max 2 trades/day AND max 1/direction)
```

**Trade Card**:
```
Strike:        = Spot rounded to nearest 50 (e.g., spot 22,650 → strike 22,650)
Entry Premium: = Live LTP from chain (bought at market)
Entry Band:    = LTP to LTP+10 (entry flexibility)
SL (spot):     = ORB midpoint (the level we broke through)
SL (premium):  = 0.75 × Entry LTP (exits when premium decays to 75%)
Target 1 (50% book): = ORB midpoint ± 1R
  → When hit: BOOK 50%, move SL to breakeven, keep runner for Target 2
Target 2 (exit rest):  = ORB midpoint ± 2R
  → When hit: EXIT remaining 50%
Max Risk:      = 0.25 × Entry LTP × 75 (position size = 1 lot = 75)
Exit by:       = 15:10 (forced square-off)
```

**Exit Triggers (Priority Order)**:
1. Hard SL hit (candle close beyond SL spot)
2. Premium SL (LTP drops to 75% of entry)
3. Thesis dead (price closes back inside level)
4. OI flip (writers now writing fresh, not covering)
5. VIX spike (>8% above prior close)
6. Time stop (75 min; RANGE state only)
7. +1R touch (book 50%)
8. +2R touch (exit rest)
9. Opposite signal >= 80
10. 15:10 forced square-off

**Example Trade**:
```
Spot: 22,650 | PDH: 22,660 | PDL: 22,550 | ORB mid: 22,605

Signal fires: "LONG CALL 22,650"
Entry: 10:15 @ ₹165 (market price)
SL (spot): 22,605 | SL (prem): ₹124 (75% × 165)

Target 1: 22,650 + 45 = 22,695 (1R = 45 pts spot)
  → Premium ≈ 165 + (0.5 × 45) = 187
  → Book 50% when premium hits 187
  → SL moves to entry (165)

Exit: 11:30 @ ₹193
Result: (193-165) / (165×0.25) = 28/41 = +0.68R
P&L: ₹(193-165) × 75 = ₹2,100
```

---

### Strategy 2: SWEEP-FADE (Counter-trend Reversal)

**When it fires**:
- Price PUSHES above PDH (or key level) but 5-min candle CLOSES back inside
- → BUY PUT (fade the push)
- Price PUSHES below PDL (or key level) but 5-min candle CLOSES back inside
- → BUY CALL (fade the push)

**Entry Logic**:
```
Score = 0
  + 20  if price broke through level (sweep)
  + 20  if 5-min candle close back INSIDE by >25% of range (rejection)
  + 20  if writers are now WRITING fresh (OI rising AGAINST the break direction)
         = mean-reversion signal (wall is defending)
  + 15  if breakout candle volume spike
  + 15  if regime allows counter-direction entry (range-biased)
  + 10  if inside 09:30-11:00 OR 13:30-14:45 window (two timing slots)
  ─────
  MAX = 100

FIRE if: Score >= 80 AND (max 2 trades/day, shared with BREAKOUT)
```

**Why It Works**:
- When price breaks a level (PDH/PDL), short-term traders exit their winners
- But the level holds (writers defending = OI rising AGAINST the break)
- 5-min candle closes back inside = trap reversal
- We enter the opposite direction = fade the move

**Example Trade**:
```
Time: 10:45
Spot: 22,665 | PDH: 22,660
Action: Price briefly goes to 22,668 (above PDH)
5-min candle: Opens 22,662, closes 22,642 (back inside, 20 pt range)

Signal: "SHORT CALL 22,650" (fade the upside push)
Entry: 10:47 @ ₹155
SL: 22,668 + 5 = 22,673 (level + 5 pt buffer)

Target 1: 22,650 - 25 = 22,625 (1R = 25 pts on fade)
Exit: 11:15 @ ₹172
Result: (155-172) / (155×0.25) = -17/39 = -0.44R
P&L: ₹(155-172) × 75 = -₹1,275 (loss)
```

---

### Strategy 3: PULLBACK (Trend Continuation)

**When it fires**:
- Market in TREND-UP state
- Price pulls back to VWAP or the last broken level
- 5-min candle closes back OFF it (reversal candle)
- → BUY CALL (resuming uptrend)

**Entry Logic**:
```
Score = 0
  + 20  if pullback to VWAP or structure level
  + 20  if rejection candle (close back away from level)
  + 20  if OI = no fresh writing (writers neutral, not threatening)
  + 15  if VWAP/structure still intact (trend hasn't broken)
  + 15  if trend still confirmed (higher lows, positive EMA)
  + 10  if inside 09:30-11:00 window
  ─────
  MAX = 100

FIRE if: Score >= 80 AND (trend day state only)
```

**Why It Works**:
- Pullbacks are normal in trends (rest, then resume)
- VWAP is dynamic support/resistance in trending markets
- Entry near VWAP with trend intact = high probability resumption

---

## Scoring System (Frozen Weights)

All three strategies use these FROZEN scoring components:

```
Component                Weight  Type
─────────────────────────────────────────────────────
Level Break              +20     Price closed beyond ORB/PDH/PDL
Confirmation             +20     Prior candle also beyond level
OI Component             +20     Writers covering (BREAKOUT) OR writing fresh (SWEEP-FADE)
Volume Component         +15     Candle volume > 10-candle average
Regime + VIX             +15     Regime allows + VIX < 8% spike
Time Window              +10     Inside valid entry hours
─────────────────────────────────────────────────────
MAX AVAILABLE            100

THRESHOLD TO FIRE        >= 80
JOURNALED NEAR-MISS      55-79 (tracked but not alerted)
BLOCKED (score >= 80)    Gate rejection (state / wall / cap / room)
```

---

## Day State Engine (Controls Which Strategies Fire)

Every cycle, day state is re-computed from:

```
Components:
  ├─ ORB (09:15-09:30): first hour range high/low
  ├─ PDH/PDL: prior day high/low
  ├─ VWAP: volume-weighted average price (cumulative)
  ├─ EMA-20: exponential moving average on 5-min closes
  ├─ Structure: higher lows / lower highs
  └─ Spot: current price
```

**State Transitions**:

```
RANGE (Default)
  └─ Entered at market open
  └─ Means: no clear breakout direction yet
  └─ Allowed entries: SWEEP-FADE only (fading extremes)
  └─ Time stop applies: 75-minute exits

TREND-UP
  └─ Triggered: spot > VWAP AND > EMA-20 AND higher lows forming
  └─ Means: uptrend developing
  └─ Allowed entries: BREAKOUT above levels + PULLBACK to VWAP
  └─ No time stop: runner trails structure (can hold to 15:10)
  └─ Exit on: structure break (lower low below prior low)

TREND-DOWN
  └─ Triggered: spot < VWAP AND < EMA-20 AND lower highs forming
  └─ Means: downtrend developing
  └─ Allowed entries: BREAKOUT below levels + PULLBACK to VWAP
  └─ No time stop: runner trails structure
  └─ Exit on: structure break (higher high above prior high)

RANGE (back)
  └─ Triggered: structure broken without new lows/highs forming
  └─ Re-enters default: only sweeps allowed again
  └─ Time stop re-applies
```

**Example Day**:
```
09:15  State = RANGE (default)
09:45  Price > VWAP + higher lows forming → State = TREND-UP
10:30  Still TREND-UP
12:15  Price breaks structure (lower low) → State = RANGE
12:45  Price reforms higher low + > VWAP → State = TREND-UP
15:10  Forced square-off regardless of state
```

---

## Liquidity Map (Intraday Support/Resistance)

Computed every cycle from option chain:

```
Top 3 PUT OI Strikes (Support levels S1-S3)
    ├─ S1: Highest PUT OI (strongest support)
    ├─ S2: 2nd highest
    └─ S3: 3rd highest

Top 3 CALL OI Strikes (Resistance levels R1-R3)
    ├─ R1: Highest CALL OI (strongest resistance)
    ├─ R2: 2nd highest
    └─ R3: 3rd highest

Each level shows:
    ├─ Change-in-OI: ↑ (writing fresh) / ↓ (covering) / → (flat)
    ├─ Distance from spot
    └─ Confluence: if within 0.15% of PDH/PDL = STRONG level
```

**Used by**:
- BREAKOUT: higher OI wall ahead = check room-to-run (need 25+ pts)
- SWEEP-FADE: OI wall defending the level = fake breakout
- Risk gates: prevent entries at weak liquidity

---

## Risk Management (Hard Gates)

Every potential trade must pass these gates to fire:

```
✓ Score >= 80 (locked)
✓ Regime allows this direction (locked)
✓ VIX < 8% spike (locked)
✓ No event day (locked)
✓ Max 2 trades/day (enforced)
✓ Max 1/direction (enforced)
✓ Room-to-run >= 25 pts to nearest wall (dynamic)
✓ Cooldown: 20 min (trend) / 30 min (range) since last entry (enforced)
✓ 2 consecutive full losses (-1R) → shutdown for day (circuit breaker)
✓ Day reaches +2R → profit lock, stop trading (circuit breaker)
✓ Daily loss > 5% of capital → shutdown (risk manager)
✓ Candle freshness: <= 6 min old, spot drift < 0.25% (staleness gate)
✓ Entry window: 09:30-11:00 (breakout/pullback) OR 13:30-14:45 (sweep-fade)
```

**Example Gate Rejection**:
```
Signal score: 82 (BREAKOUT, LONG)
Regime: ✓ (allows LONG)
VIX: ✓ (6.2% stable)
Event day: ✓ (normal)
Daily trades: 1/2 ✓
Per-direction: 0/1 ✓
Cooldown: 45 min since last trade ✓
Room-to-run: R1 (22,700) - spot (22,650) = 50 pts ✓ (>25)

All gates pass → FIRE 🎯
```

---

## Paper Accounting (How P&L is Calculated)

When a trade is entered:

```
Entry Premium (LTP):      ₹165 (what we paid)
Entry Spot:               22,650
SL Spot:                  22,605
Max Risk Points:          22,650 - 22,605 = 45 pts
Position Size:            75 contracts
Max Risk (₹):             45 × 75 = ₹3,375

1R (in rupees):           ₹3,375 / 75 = ₹45 per point
```

When trade exits:

```
Exit Premium (LTP):       ₹193 (what we sold at)
Profit (₹):               (193 - 165) × 75 = ₹2,100
Profit (pts):             (193 - 165) = 28 premium points
Profit (R):               28 × 75 / 3,375 = +0.68R

Result:  +0.68R  or  +₹2,100
```

If +1R target hit (book 50%):

```
+1R = spot + 1R points = 22,650 + 45 = 22,695
Premium at +1R ≈ 165 + (0.5 × 45) = 187

Book 50% at 187:
  → 50% profit locked: (187 - 165) × 75 / 2 = ₹825
  → Runner 50%: SL moves to entry (165), target still +2R
  
If runner exits at +2R (premium ≈ 210):
  → Additional profit: (210 - 165) × 75 / 2 = ₹1,687
  → Total trade: ₹825 + ₹1,687 = ₹2,512 = +0.75R
```

---

## Journal & Historical Data

Every signal and exit is logged to SQLite:

```
Table: journal
Columns:
  ├─ timestamp (when signal fired)
  ├─ family (BREAKOUT / SWEEP-FADE / PULLBACK)
  ├─ day_state (TREND-UP / TREND-DOWN / RANGE)
  ├─ direction (LONG / SHORT)
  ├─ strike
  ├─ entry_time, entry_price (premium)
  ├─ exit_time, exit_price
  ├─ exit_reason (SL / TGT1 / TGT2 / time / OI_flip / opposite_signal / square-off)
  ├─ result_r (R-value)
  ├─ pnl_rupees
  ├─ score (raw entry score: 55-100)
  ├─ reasons_json (full scoring breakdown)
  └─ notes (additional context)
```

**Used for**:
- EOD accounting
- Performance analysis by family / state / direction
- Replay backtesting (60-day mode)
- Post-exit shadow (what WOULD have happened if we didn't exit)

---

## Real-World Example (Full Day Flow)

**Oct 6, 2026**:

```
08:45  📊 PRE-MARKET: US -0.8%, crude $78, USD/INR 83.2, VIX 16.2, FII sell
       └─ Telegram receives report

09:15  🌤 REGIME: GAP +0.15% (NORMAL), PCR 1.05 (neutral), VIX stable
       └─ State = RANGE (default)
       └─ Expected: mixed signals, favor fades

09:30  ENTRY WINDOW OPENS
       ├─ Candle 1 (09:30): Spot 22,640, no break
       ├─ Candle 2 (09:35): Spot 22,655, breaks ORB high (22,650)
       │  └─ Score = 82 (level +20, confirmation +20, OI falling +20, vol +15, regime +15, window -8 min, too early)
       │     → Near-miss (journaled, not fired yet)
       │
       ├─ Candle 3 (09:40): Spot 22,668, still > ORB high, closes 22,665
       │  └─ Score = 88 (level +20, confirmation +20, OI still falling +20, vol +15, regime +15, window +8 min)
       │  └─ FIRE: 🎯 SIGNAL: BUY CALL 22,650 @ ₹162 (entry premium)
       │     ├─ SL: 22,605 (ORB mid) / ₹122 (prem SL)
       │     ├─ TGT1: 22,695 / ₹188
       │     ├─ TGT2: 22,740 / ₹214
       │     └─ Family: BREAKOUT | State: RANGE
       │
       ├─ Candle 4-8: Spot 22,680, premium 175, still holding
       │
       ├─ Candle 9 (10:55): Spot 22,620, breaks ORB low (22,630)
       │  └─ Score = 91 (all components ++) for SHORT direction
       │  └─ FIRE: 🎯 SIGNAL: BUY PUT 22,650 @ ₹148 (entry premium)
       │  └─ Family: SWEEP-FADE (price broke but candle closed back inside)
       │  └─ Daily trades: 2/2 (cap reached)
       │
       ├─ Candle 10-20: Both trades open, managing risk
       │
       ├─ Candle 21 (12:00): First trade (CALL) hits TGT1 @ 188
       │  └─ 🚨 EXIT: Book 50% @ 188
       │     ├─ Profit: (188-162) × 75 / 2 = ₹975
       │     ├─ Runner: SL 162, TGT2 still 214
       │     └─ Result: +0.33R (half locked)
       │
       ├─ Candle 22-40: Second trade (PUT) still open, runner holding CALL
       │
       ├─ Candle 41 (13:45): State changes to TREND-DOWN (lower highs)
       │  └─ (No new entries - daily cap reached)
       │
       ├─ Candle 50 (14:15): Second trade (PUT) hits SL @ 122 premium
       │  └─ 🚨 EXIT: SL hit on PUT
       │     ├─ Loss: (148-122) × 75 = ₹1,950
       │     └─ Result: -0.52R
       │
       ├─ Candle 55 (14:40): CALL runner still open @ 205 premium
       │
       ├─ Candle 60 (15:05): State back to RANGE
       │  └─ (Running out of time)
       │
       └─ 15:10 ⏹ SQUARE-OFF
          └─ CALL runner exits at 205
             ├─ Profit: (205-162) × 75 / 2 = ₹1,612
             ├─ Result: +0.43R (second half)
             └─ Total trade 1: +0.33R + 0.43R = +0.76R

15:40  📕 EOD SUMMARY
       ├─ Trades: 2
       ├─ Results: +0.76R, -0.52R
       ├─ Win Rate: 50% (1W / 1L)
       ├─ Total R: +0.24R
       ├─ Total ₹: ₹(975 + 1,612 - 1,950) = ₹637
       ├─ By Family: BREAKOUT +0.76R (1 trade) | SWEEP-FADE -0.52R (1 trade)
       ├─ By State: RANGE +0.24R
       ├─ Data Quality: ✓ Chain 100% | ✓ VIX 100% | ✓ Candles 100%
       └─ Telegram receives full summary

15:45  🔴 SHUTDOWN
       └─ SQLite journal flushed, all sessions closed
```

---

## Summary: Why This Works

1. **Frozen Scoring** → No curve-fitting, consistent rule application
2. **Day State Logic** → Adapts to market regime (trend vs range)
3. **Multi-family Setup** → BREAKOUT (trend driver), SWEEP-FADE (mean reversion), PULLBACK (continuation)
4. **Risk Controls** → Caps (2/day, 1/dir), cooldowns, circuit breakers, room-to-run gate
5. **Graded Exits** → Priority order prevents whipsaw, +1R booking locks profit
6. **Single Source of Truth** → All modules read from one validated store
7. **Telegram-Only UI** → No order placement, pure alerts + journal
8. **Replay Engine** → Test strategies on 60 days of history (price-only, cost-deducted)

All of this runs **end-to-end**, **no manual intervention**, **every weekday**, scheduled via Windows Task Scheduler.

---

**Current Status**: 🎯 Live trading (Phase B) + 📊 Replay backtesting (Phase C)

**Next Phase**: 🤖 SmartAPI execution (Phase D) - manual approval gated
