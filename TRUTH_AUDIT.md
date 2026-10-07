# TRUTH AUDIT: Signal Paths & Implementation vs Documentation

**Date**: October 7, 2026 | **Scope**: Verify actual code vs documented strategy

---

## FINDING #1: Only 2 of 4 Strategy Families Are Actually Implemented

### Specification (From SYSTEM_FLOW.md)
Three main strategies documented:
1. **BREAKOUT** - Break above ORB/PDH → BUY CALL; break below → BUY PUT
2. **SWEEP-FADE** - Price breaks but candle closes back inside → fade the move
3. **PULLBACK** - Pullback to VWAP/structure in TREND state → re-entry

### Documentation Claims (README.md, SYSTEM_FLOW.md)
- "Frozen score: +20 level break, +20 confirmation, +20 OI, +15 volume, +15 regime, +10 window"
- "3 setup families: BREAKOUT, SWEEP-FADE, PULLBACK" 
- "SWEEP-FADE shares daily cap (max 2 trades/day) with BREAKOUT"
- "Entry evaluation on BOTH directions every 5-min candle CLOSE inside 09:30-11:00"

### Actual Implementation (Code Audit)

#### Live Mode (main.py, DaySession)
The code path in live trading attempts to evaluate:
```python
# engines/entry.py
for ev in self.breakouts.evaluate(candle, ctx):      # ← used
    self._handle_evaluation(ev, family=fam, fire_fn=breakout_fire)

for ev in self.sweeps.evaluate(candle, ctx):         # ← used
    self._handle_evaluation(ev, family="SWEEP", fire_fn=sweep_fire)

for ev in self.pullbacks.evaluate(...):              # ← used (live only)
    self._handle_evaluation(ev, family="PULLBACK", fire_fn=pullback_fire)
```

**Live mode status**: Entry, Sweep, Pullback engines exist and are evaluated

#### Replay Mode (replay_mtf.py, _replay_day)
The actual replay engine creates trades ONLY from:

```python
# Line 200-218: MTF zones → trades
for zone in runtime.zones.values():
    if ready_confirmed_zone(zone, candle.end):
        family = ("LEVEL-FADE" if zone.zone_id.startswith("FADE-")
                  else "MTF-SCALP")
        open_trades.append(ReplayTrade(..., family=family, ...))

# Line 227-234: LEVEL-CONT from runtime events  
for event in events:
    family = event_family(event)
    if family == "LEVEL-CONT" and event.direction:
        open_trades.append(ReplayTrade(..., family=family, ...))

# Line 239-246: COIL-SNIPE from coil_engine
for coil_event in coil_engine.on_five_minute(...):
    if coil_event.kind == "ENTRY":
        open_trades.append(ReplayTrade(..., family="COIL-SNIPE", ...))
```

**ORB signals (entry engine results) are created but NOT added to trades:**

```python
# Line 247-277: ORB signals created separately
for candle in five_minute:
    for ev in entry_engine.evaluate(candle, prev, orb_ctx):
        if ev.fired and should_fire_price_only(ev.score):
            result.orb_signals.append({...})  # ← Added to separate list
            # NOT added to result.mtf_trades!

return result
```

### 7-Day Replay Results (Actual, Oct 7, 2026)
```
2026-09-29: 8 trades  (all LEVEL-FADE/MTF-SCALP)
2026-09-30: 3 trades  (all LEVEL-FADE/MTF-SCALP)
2026-10-01: 4 trades  (all LEVEL-FADE/MTF-SCALP)
2026-10-02: 0 trades  (weekend/holiday)
2026-10-05: 2 trades  (all LEVEL-FADE/MTF-SCALP)
2026-10-06: 5 trades  (4 MTF-SCALP, 1 LEVEL-FADE)
2026-10-07: 4 trades  (all LEVEL-FADE/MTF-SCALP)

TOTAL: 26 trades | Win 19.2% | Exp -1.01R | Max DD -28.33R
```

**Breakdown by family:**
```
LEVEL-FADE:   20 trades (76.9%)
MTF-SCALP:     6 trades (23.1%)
BREAKOUT:      0 trades (0%)     ← NOT in replay
PULLBACK:      0 trades (0%)     ← NOT in replay  
COIL-SNIPE:    0 trades (0%)     ← NOT in replay
LEVEL-CONT:    0 trades (0%)     ← NOT in replay
```

### Root Cause Analysis

**Why BREAKOUT/PULLBACK/COIL-SNIPE don't appear in replay:**

1. **Entry Engine (BREAKOUT) is instantiated with `store=None`**:
   ```python
   entry_engine = EntryEngine(store=None)
   orb_ctx = {...}  # minimal mock context
   ```
   The ORB signals ARE calculated but stored in `result.orb_signals` (a diagnostic list), NOT added to `result.mtf_trades`.

2. **Pullback engine not used in replay at all**:
   The replay engine doesn't instantiate `PullbackEngine`. No pullback evaluation occurs.

3. **Coil engine is instantiated BUT only works when:**
   - `coil_engine.on_five_minute()` generates `kind == "ENTRY"` events
   - Runtime budget allows (controlled by `BUDGET_TREND`/`BUDGET_RANGE`)
   - This depends on internal state from `coil_engine.update_coil()` + `on_minute()` events
   - In observed dates: generated 0 coil trades

4. **LEVEL-CONT (pullback continuations) require:**
   - Runtime events where `family == "LEVEL-CONT" and event.direction` is truthy
   - This depends on runtime state machines (LevelRuntime) that may not fire without live OI data

---

## FINDING #2: Entry Time Windows Are Still Hardcoded (Not Dynamic)

### Specification Claim
"Time window: 09:30-11:00 (frozen)"

### Documentation Claim (README.md)
"Threshold to fire: ≥ 80. Fire at score ≥ 80 + regime allows + caps (max 2/day, 1/direction)"

### Actual Code

#### Hard Entry Window (frozen as spec)
```python
# settings.py (line 37-39)
ENTRY_EVAL_START = dtime(9, 30)   # 09:30
ENTRY_WINDOW_END = dtime(11, 0)   # 11:00 (hardcoded)
SWEEP_WINDOW_2_START = dtime(13, 30)  # Second sweep window
SWEEP_WINDOW_2_END = dtime(14, 45)
LAST_ENTRY_TIME = dtime(14, 30)   # No NEW entries after this
```

#### Entry Engine Enforces Window
```python
# engines/entry.py
def evaluate(self, candle, prev, ctx):
    now = ctx.get("now", ist_now())
    if now.time() < settings.ENTRY_EVAL_START:
        return  # No entries before 09:30
    if candle.start.time() >= settings.ENTRY_WINDOW_END:
        return  # No new breakout entries after 11:00
```

**Status**: ✅ Window IS hardcoded, frozen (as documented)

---

## FINDING #3: Trade Cap is "Max 2/Day, 1/Direction" (Verified Correct)

### Specification vs Code

| Aspect | Doc Claim | Code Reality | Match |
|--------|-----------|--------------|-------|
| Daily cap | "2 trades/day" | `MAX_TRADES_PER_DAY = 2` (line 118) | ✅ YES |
| Per-direction | "1 per direction" | `MAX_TRADES_PER_DIRECTION = 1` (line 119) | ✅ YES |
| Enforcement | Caps checked before fire | `if self.store.has_signal(day, direction)` | ✅ YES |
| Shared cap | BREAKOUT + SWEEP-FADE share | Both use same `trade_id` prefix | ✅ YES |

**Status**: ✅ Cap implementation matches documentation

---

## FINDING #4: Live Mode vs Replay Mode Divergence

### Comparison Table: SPEC vs DOC vs CODE

| Signal Family | Spec | README | SYSTEM_FLOW | Live Code | Replay Code | 7-Day Results |
|---|---|---|---|---|---|---|
| **BREAKOUT** | ORB + level break | Documented fully | 3 strategies listed | Engine exists | ORB signals → separate list | 0 fires |
| **SWEEP-FADE** | Level rejection | Documented fully | Strategy explained | Engine exists | LEVEL-FADE/MTF-SCALP | 20 fires |
| **PULLBACK** | Trend continuation | Documented fully | Strategy explained | Engine exists | NOT used in replay | 0 fires |
| **COIL-SNIPE** | Consolidation | Brief mention | Not described | Engine exists | Engine used but 0 fires | 0 fires |
| **MTF-SCALP** | NOT in spec | NOT documented | NOT mentioned | NOT in live | Generated from level_runtime zones | 6 fires |
| **LEVEL-FADE** | NOT in spec | NOT documented | NOT mentioned | NOT in live | Generated from zone with "FADE-" prefix | 20 fires |

### Key Divergence: **MTF-SCALP and LEVEL-FADE Are Replay-Only**

These families:
- ✅ Exist only in `_replay_day()` 
- ✅ Come from `level_runtime.zones` (MTF zone manager)
- ✅ Are NOT in live trading code path
- ❌ Are NOT documented anywhere
- ❌ Are NOT in SYSTEM_FLOW.md

---

## FINDING #5: The Complete Signal Path Audit

### What Actually Fires in Live Mode
```
1. 60-second main loop (DaySession.loop)
   ├─ Evaluate candle (5-min close)
   ├─ Entry Engine
   │  ├─ BREAKOUT: ORB levels + scoring → score ≥ 80 → fire 🎯
   │  ├─ SWEEP: ORB levels + scoring → score ≥ 80 → fire 🎯
   │  └─ PULLBACK: VWAP/structure in TREND → score ≥ 80 → fire 🎯
   ├─ Level Runtime (MTF zones)
   │  ├─ Create fade/confirmation zones
   │  └─ Fire when confirmed (separate path)
   ├─ Coil Engine
   │  └─ Fire on consolidation breakout
   └─ Second-Stage Engine
      └─ Fire on divergence/retest

2. Exits
   └─ Check 10-priority triggers, execute

3. Telegram
   └─ Send 🎯 SIGNAL, 🚨 EXIT alerts
```

### What Actually Fires in Replay Mode
```
1. For each trading day
   ├─ Fetch 1-min + 5-min candles (yfinance)
   ├─ For each 5-min candle
   │  ├─ Update DayStateEngine (TREND/RANGE)
   │  ├─ Update LevelRuntime (zones)
   │  │  └─ On 1-min minute_close: create/update zones
   │  │     ├─ FADE-* zones (mean-reversion)
   │  │     └─ LONG-*/SHORT-* zones (continuation)
   │  ├─ Update CoilEngine (consolidation)
   │  ├─ Check ready_confirmed_zone()
   │  │  ├─ If zone starts with "FADE-" → family = LEVEL-FADE
   │  │  └─ Else → family = MTF-SCALP
   │  │  → Add trade to open_trades
   │  ├─ Check LEVEL-CONT events (pullback-like)
   │  └─ Check COIL-SNIPE events
   │
   └─ Separately: evaluate ORB signals (entry_engine)
      └─ Create orb_signals list (NOT added to trades!)

2. Simulate exits on each candle
   └─ SL/TGT1 logic → mark exit_time, exit_reason, result_r

3. Result
   └─ mtf_trades = [LEVEL-FADE, MTF-SCALP, COIL-SNIPE, LEVEL-CONT]
   └─ orb_signals = [ORB breakout diagnostics - NOT counted!]
```

---

## FINDING #6: Why ORB Signals Are Excluded from Replay Reports

### Code Evidence

**Line 1: ORB signals created** (replay_mtf.py:247-277)
```python
for candle in five_minute:
    orb_ctx["orb"] = orb_levels(history)
    orb_ctx["spot"] = candle.close
    for ev in entry_engine.evaluate(candle, prev, orb_ctx):
        if ev.fired and should_fire_price_only(ev.score):
            result.orb_signals.append({
                "time": candle.end.isoformat(timespec="seconds"),
                "direction": ev.direction,
                "level": ev.level_name,
                "close": candle.close,
                "sl": sl,
                "score": ev.score,
            })
```

**Line 2: Only mtf_trades counted in replay_60day** (replay_60day.py:75-100)
```python
for trade in day_replay.mtf_trades:  # ← Only mtf_trades loop!
    result_r = trade.result_r or 0.0
    session.total_trades += 1
    session.total_r += result_r
    # ... accumulate stats ...
```

**Line 3: orb_signals shown separately but not counted** (replay_compare.py:110-117)
```python
if day_replay.orb_signals:
    print("\n[WARN]  ORB SIGNALS (ADDITIONAL)")
    for sig in day_replay.orb_signals[:10]:
        print(f"  ORB {sig['direction']} {sig['level']} ...")
```

### Why?

**Hypothesis 1 (Most Likely)**: ORB signals are a diagnostic artifact from running the entry_engine with a minimal mock context (`store=None`). They're not actually executed in replay because:
- No paper trade is created for them
- No exit simulation is run
- No R calculation done
- Only collected for debugging ("why didn't this signal fire?")

**Hypothesis 2**: Intentional separation - ORB is "potential" but MTF trades are "actual executed". This would make sense if the replay is designed to show what the MTF level_runtime system would actually trade (levels + zones) vs what a simple ORB breakout would catch.

### Consequence

**The 26 trades reported in 7-day replay are ONLY MTF/COIL/LEVEL-CONT trades**, NOT BREAKOUT trades.

---

## FINDING #7: Divergence Summary Table

| Component | Spec Says | Doc Says | Code Does | Replay Fires | Live Fires |
|---|---|---|---|---|---|
| Entry families | 3: BREAKOUT, SWEEP, PULLBACK | Same | Entry + Sweep + Pullback + MTF + Coil | 2: MTF fam | 3: Entry fam |
| Time window | 09:30-11:00 hardcoded | Same | Hardcoded ✅ | Hardcoded ✅ | Hardcoded ✅ |
| Daily cap | 2 trades max | Same | 2 max ✅ | Enforced via MTF | Enforced ✅ |
| Per-direction | 1 max | Same | 1 max ✅ | Enforced | Enforced ✅ |
| Scoring | +20/20/20/15/15/10 = 100 max | Same | Matches ✅ | Price-only: 65 max | Full scoring ✅ |
| Sweep-fade fam | Documented | Documented | Engine exists | 20 LEVEL-FADE | Used ✅ |
| ORB breakout | Frozen scoring | Documented | Engine exists | ORB signals = separate list | Fired as signals |
| Pullback fam | Documented | Documented | Engine exists | NOT called | Used ✅ |
| MTF-Scalp | NOT in spec | NOT documented | Exists in replay | 6 fires | NOT in live |
| LEVEL-FADE | NOT in spec | NOT documented | Exists in replay | 20 fires | NOT in live |

### Red Flags

🚩 **MTF-SCALP and LEVEL-FADE** exist in replay but are not documented anywhere in SPEC or DOC

🚩 **ORB signals** generated but not counted in replay statistics

🚩 **Pullback engine** called in live but never called in replay

🚩 **Documentation claims "3 families" but replay generates "2 families"**

---

## CONCLUSION: The Truth

### What Actually Exists in Code
1. **Live Trading**: Entry (BREAKOUT) + Sweep + Pullback + MTF zones + Coil engines
2. **Replay Trading**: MTF zones only (LEVEL-FADE, MTF-SCALP) + Coil (not firing)

### What 7-Day Replay Actually Shows
- 26 trades total
- 20 LEVEL-FADE (fade-zones, mean-reversion style)
- 6 MTF-SCALP (confirmation zones, breakout style)
- 0 BREAKOUT (ORB signals separated, not counted)
- 0 PULLBACK (not called in replay)
- 0 COIL-SNIPE (engine active but 0 entries triggered)

### Discrepancy with Documentation
- **README.md** says "Phase B: Sniper engine: entry/exit engines, trade cards, paper accounting" ✅ correct for LIVE
- **SYSTEM_FLOW.md** lists "3 strategies: BREAKOUT, SWEEP-FADE, PULLBACK" ✅ correct for LIVE but NOT for REPLAY
- **Replay reports** show "20 LEVEL-FADE, 6 MTF-SCALP" 🚩 not documented anywhere

### Questions for User

1. **Is replay supposed to match live signal families?** If yes, why are MTF zones the only replay source?
2. **Should ORB signals count in replay stats?** Currently they're excluded.
3. **Is LEVEL-FADE a documented strategy or an implementation artifact?** It fires 77% of replay trades but isn't mentioned in SYSTEM_FLOW.
4. **Why does replay not call PullbackEngine?** Is pullback only for TREND days?

---

**Status**: 🔴 **DOCUMENTATION INCOMPLETE** - Live and replay modes trade different signal families, not explained in docs.

