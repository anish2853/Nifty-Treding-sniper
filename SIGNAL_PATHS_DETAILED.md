# Complete Signal Path Analysis - Where Every Family Actually Lives

## Overview

This document traces every signal family from trigger → trade creation → statistics reporting in both LIVE and REPLAY modes.

---

## LIVE MODE Signal Paths (From main.py DaySession)

### Path 1: BREAKOUT (Entry Engine)
```
Flow: Candle close → Entry evaluation → Score ≥ 80 → Fire 🎯 SIGNAL

Code Location:
  • Trigger: main.py line 568, DaySession._eval_candle()
  • Engine: engines/entry.py, EntryEngine.evaluate()
  • Evaluation Logic:
    ├─ Level break: close beyond ORB/PDH/PDL? (+20)
    ├─ Confirmation: prior candle also beyond? (+20)
    ├─ OI: fresh writing at 3 nearest strikes falling? (+20)
    ├─ Volume: > 10-candle average? (+15)
    ├─ Regime: allows direction? (+15)
    └─ Time window: 09:30-11:00? (+10)
    
  • Gate checks:
    ├─ Score ≥ 80/100 (threshold)
    ├─ Max 2 trades/day (cap_ok gate)
    ├─ Max 1/direction (direction cap)
    ├─ Room to run ≥ 25 pts
    ├─ Cooldown elapsed (20 min trend / 30 min range)
    ├─ Freshness gate (candle ≤ 6 min old)
    └─ VIX stable (< 8% spike)
    
  • Fire Action: DaySession.fire() → PaperTrade created
  • Journal: store.journal_event("SIGNAL", family="BREAKOUT", ...)

Status in Code:
  ✅ Exists in live path
  ✅ Fully implemented with all gates
  ❌ NOT used in replay (_replay_day doesn't call EntryEngine)
```

### Path 2: SWEEP-FADE (Sweep Engine)
```
Flow: Candle close → Sweep evaluation → Score ≥ 80 → Fire 🎯 SIGNAL

Code Location:
  • Trigger: main.py line 570-574, DaySession._eval_candle()
  • Engine: engines/sweep.py, SweepEngine.evaluate()
  • Evaluation Logic:
    ├─ Sweep: price broke level (intrabar high/low)? (+20)
    ├─ Rejection: 5-min candle closed back inside >25%? (+20)
    ├─ OI: writers accelerating (fresh writing against move)? (+20)
    ├─ Volume: spike on breakout? (+15)
    ├─ Regime: allows counter-direction? (+15)
    └─ Time window: 09:30-11:00 OR 13:30-14:45? (+10)
    
  • Gate checks: Same as BREAKOUT (shared daily cap)
  
  • SL Spot: swept level ± 5 points
  
  • Fire Action: DaySession.fire() → PaperTrade created
  • Journal: store.journal_event("SIGNAL", family="SWEEP-FADE", ...)

Status in Code:
  ✅ Exists in live path
  ✅ Fully implemented
  ❌ NOT used in replay
```

### Path 3: PULLBACK (Pullback Engine)
```
Flow: Trend state + VWAP pullback → Rejection candle → Score ≥ 80 → Fire 🎯

Code Location:
  • Trigger: main.py line 576-581, DaySession._eval_candle()
  • Engine: engines/pullback.py, PullbackEngine.evaluate()
  • Evaluation Logic (TREND state only):
    ├─ Level: pullback to VWAP or structure? (+20)
    ├─ Rejection: candle close away from pullback level? (+20)
    ├─ OI: no fresh writing (defending)? (+20)
    ├─ VWAP: alignment intact (+15)
    ├─ Structure: trend still confirmed (+15)
    └─ Time window: 09:30-11:00? (+10)
    
  • Requirements:
    ├─ Day state MUST be TREND-UP or TREND-DOWN
    ├─ Price must be at VWAP or major support/resistance
    ├─ Candle must reject (bounce off) the level
    
  • Fire Action: DaySession.fire() → PaperTrade created
  • Journal: store.journal_event("SIGNAL", family="PULLBACK", ...)

Status in Code:
  ✅ Exists in live path
  ✅ Fully implemented
  ❌ NOT used in replay (_replay_day doesn't instantiate PullbackEngine)
```

### Path 4: COIL-SNIPE (Coil Engine)
```
Flow: Consolidation detected → Breakout of coil → Fire 🎯

Code Location:
  • Trigger: main.py line 583-589, DaySession._eval_candle()
  • Engine: engines/coil_snipe.py, CoilSnipeEngine
  • Evaluation Logic:
    ├─ Consolidation: 8+ consecutive 5-min bars in narrow range? (+5)
    ├─ EMA crosses: repeated touches/crosses of EMA-9 (+5)
    ├─ Range: ≤ 65 points and ≤ 0.3% of spot (+5)
    ├─ Boundary tests: 2+ touches at both high and low (+5)
    ├─ Breakout: candle close beyond coil boundary (+30)
    └─ No divergence trap (recent trap within 6 candles)
    
  • Budget: 4 trades/day (TREND) or 2 trades/day (RANGE)
  • Cooldown: 20 min (trend) or 30 min (range)
  
  • Fire Action: DaySession.fire() → PaperTrade created
  • Journal: store.journal_event("SIGNAL", family="COIL-SNIPE", ...)

Status in Code:
  ✅ Exists in live path
  ✅ Fully implemented
  ❌ Rarely fires in practice (coil formations uncommon)
  ❌ NOT effectively used in replay (0 fires in 7-day test)
```

### Path 5: LEVEL-WATCH & MTF Zones (Level Runtime) 
```
Flow: Zone creation → Confirmation → Fire 🎯

Code Location:
  • Trigger: main.py line 669-697 (live MTF handling)
  • Engine: engines/level_runtime.py + engines/mtf_live.py
  • Zone Types:
    ├─ Support/Resistance zones (around OI walls)
    ├─ Pullback zones (VWAP + structure)
    └─ Fade zones (mean-reversion traps)
    
  • Zone States:
    ├─ WAITING: created, not yet confirmed
    ├─ CONFIRMED: 1-min confirmation bar met criteria
    ├─ STALE: zone expired (15-20 min no confirmation)
    └─ EXPIRED: zone outside entry window
    
  • Fire Conditions:
    ├─ ready_confirmed_zone() → zone.status == CONFIRMED
    ├─ Confirmation time met (usually 1 candle)
    ├─ Room to run ≥ 25 pts
    ├─ Range width ≥ 45 pts (if RANGE state)
    ├─ NOT in cooldown period
    
  • Fire Action: DaySession._fire_mtf() → PaperTrade created
  • Journal: store.journal_event("SIGNAL", family="LEVEL-CONT", ...)

Status in Code:
  ✅ Exists in live path
  ✅ Complex state machine, fully implemented
  ? Rarely fires (high barrier for confirmation + room-to-run)
```

---

## REPLAY MODE Signal Paths (From engines/replay_mtf.py _replay_day)

### Path A: MTF Zones → LEVEL-FADE (Fade Zones)
```
Flow: Create zone → Confirm → Check runway + width → Fire LEVEL-FADE

Code Location: replay_mtf.py line 200-218
  
  1. Zone Creation (per 1-min candle):
     └─ runtime.minute_close(minute, prev, day_state) → returns events
        ├─ FADE-VWAP zones (pullback to VWAP)
        ├─ FADE-ORB-H zones (fade above ORB high)
        ├─ FADE-ORB-L zones (fade below ORB low)
        ├─ FADE-PDH zones (fade above PDH)
        └─ FADE-PDL zones (fade below PDL)

  2. Zone State Management:
     └─ runtime.five_minute_close(candle) → check confirmations
        ├─ WAITING → CONFIRMED (when confirmation bar closes)
        └─ CONFIRMED → STALE/EXPIRED (if time elapsed)

  3. Entry Decision (per 5-min candle):
     ```python
     for zone in runtime.zones.values():
         if ready_confirmed_zone(zone, candle.end):  # line 205
             runway, nearest = runtime.runway(zone.entry_spot, zone.direction)
             if runway < ROOM_TO_RUN_MIN_PTS:
                 skip_and_log(f"runway {runway:.0f}")
                 continue
             width = runtime.range_width(zone.entry_spot)
             if width < RANGE_WIDTH_MIN_PTS:
                 continue
             
             # zone_id.startswith("FADE-") identifies fade zones
             family = "LEVEL-FADE"  # line 212
             
             open_trades.append(ReplayTrade(
                 family="LEVEL-FADE",
                 direction=zone.direction,
                 entry_time=zone.confirm_time or candle.end,
                 entry=zone.entry_spot,
                 sl=zone.stop,
                 ...
             ))
     ```

  4. Exit Simulation (per 5-min candle):
     └─ _simulate_exit(trade, candle, square_off)
        ├─ If close ≤ SL → exit at SL, result_r = -1.0R
        ├─ If close ≥ TGT1 → exit at TGT1, result_r = +1.0R  
        ├─ If bar.end ≥ 15:10 → forced square-off
        └─ Calculate result_r = move / risk

  5. Final Count:
     └─ result.mtf_trades.append(trade)  # line 256

Result in 7-Day Replay:
  ✅ 20 LEVEL-FADE trades (77% of total)
  ✅ All exits simulated correctly
  ❌ NOT documented anywhere
```

### Path B: MTF Zones → MTF-SCALP (Confirmation Zones)
```
Flow: Create zone → Confirm → Check runway + width → Fire MTF-SCALP

Code Location: replay_mtf.py line 200-218 (same logic, different zone type)

  1. Zone Creation:
     └─ runtime.five_minute_close(candle) → creates continuation zones
        ├─ LONG-* zones (confirmation for breakout continuation)
        ├─ SHORT-* zones (confirmation for breakdown continuation)
        ├─ Tied to ORB high/low or PDH/PDL
        └─ Require 1-min confirmation candle

  2. Zone State: Same WAITING → CONFIRMED → STALE/EXPIRED

  3. Entry Decision (per 5-min candle):
     ```python
     for zone in runtime.zones.values():
         if ready_confirmed_zone(zone, candle.end):
             # ... runway + width checks same as LEVEL-FADE ...
             
             # zone_id does NOT start with "FADE-"
             family = "MTF-SCALP"  # line 212 (else clause)
             
             open_trades.append(ReplayTrade(
                 family="MTF-SCALP",
                 direction=zone.direction,
                 ...
             ))
     ```

  4. Exit Simulation: Same as LEVEL-FADE

Result in 7-Day Replay:
  ✅ 6 MTF-SCALP trades (23% of total)
  ✅ All exits simulated correctly
  ❌ NOT documented anywhere
```

### Path C: ORB Signals (Entry Engine - EXCLUDED from count)
```
Flow: Evaluate entry engine → Store in separate list (NOT in trades)

Code Location: replay_mtf.py line 247-277

  1. Setup:
     └─ entry_engine = EntryEngine(store=None)  # No journal!
     └─ orb_ctx = {...}  # Minimal mock context

  2. Evaluation (per 5-min candle):
     ```python
     for candle in five_minute:
         history.append(candle)
         orb_ctx["orb"] = orb_levels(history)
         orb_ctx["spot"] = candle.close
         
         for ev in entry_engine.evaluate(candle, prev, orb_ctx):
             if ev.fired and should_fire_price_only(ev.score):  # price-only threshold
                 result.orb_signals.append({
                     "time": candle.end.isoformat(),
                     "direction": ev.direction,
                     "level": ev.level_name,
                     "close": candle.close,
                     "sl": orb_ctx["orb"]["mid"],
                     "score": ev.score,
                 })
     ```

  3. NOT Added to Trades:
     └─ result.mtf_trades ONLY gets zones, NOT orb_signals
     └─ orb_signals stored separately in DayReplay.orb_signals

  4. Usage:
     └─ replay_compare.py prints as "[WARN] ORB SIGNALS (ADDITIONAL)"
     └─ Never counted in replay_60day.py statistics
     └─ Exists only for diagnostics ("what would entry have fired?")

Status in 7-Day Replay:
  ? Unknown count (not reported, only diagnostic)
  ❌ Excluded from trade statistics
  ❌ No exit simulation for ORB signals
  ❌ No R calculation
```

### Path D: COIL-SNIPE (Coil Engine - 0 fires)
```
Flow: Consolidation detected → Breakout → Create trade

Code Location: replay_mtf.py line 239-246

  1. Zone Creation:
     └─ coil_engine.on_minute(minute) → collect consolidation events
     └─ coil_engine.on_five_minute(candle, grid, div_suppressed) → entry events

  2. Entry Decision:
     ```python
     for coil_event in coil_engine.on_five_minute(...):
         if coil_event.kind == "ENTRY" and coil_event.entry is not None:
             open_trades.append(ReplayTrade(
                 family="COIL-SNIPE",
                 direction=coil_event.direction,
                 entry=coil_event.entry,
                 sl=coil_event.stop,
                 ...
             ))
     ```

Status in 7-Day Replay:
  ✅ Code exists
  ✅ Engine called
  ❌ 0 trades generated (consolidations rare in sample data)
  ✅ Would work if consolidations found
```

### Path E: LEVEL-CONT (Pullback-like from Runtime - 0 fires)
```
Flow: Runtime events → LEVEL-CONT family → Create trade

Code Location: replay_mtf.py line 227-234

  1. Event Processing:
     ```python
     for event in events:  # events = runtime.five_minute_close(candle)
         family = event_family(event)
         if family == "LEVEL-CONT" and event.direction:
             # Similar to COIL-SNIPE
             open_trades.append(ReplayTrade(
                 family=family,  # "LEVEL-CONT"
                 direction=event.direction,
                 entry=candle.close,
                 sl=event.price,
                 ...
             ))
     ```

Status in 7-Day Replay:
  ✅ Code exists
  ❌ 0 events generated with family == "LEVEL-CONT"
  ? May depend on specific price structures
```

---

## Summary: What Actually Fires

### LIVE MODE (DaySession._eval_candle)
```
Per 5-min candle close:
  ├─ Entry.evaluate() → BREAKOUT (score ≥ 80)
  ├─ Sweep.evaluate() → SWEEP-FADE (score ≥ 80)
  ├─ Pullback.evaluate() → PULLBACK (score ≥ 80, TREND only)
  ├─ Coil.on_five_minute() → COIL-SNIPE
  ├─ LevelRuntime zones → LEVEL-CONT
  └─ (MTF zones not exported)

Expected: 4-5 families firing
Actual (unverified): Unknown (not tested in this audit)
```

### REPLAY MODE (_replay_day, 7-day verified)
```
Per 5-min candle close:
  ├─ LevelRuntime zones:
  │  ├─ FADE-* → LEVEL-FADE (20 fires)
  │  ├─ OTHER-* → MTF-SCALP (6 fires)
  │  ├─ Coil zones → COIL-SNIPE (0 fires)
  │  └─ LEVEL-CONT events (0 fires)
  └─ Entry engine:
     └─ ORB signals (NOT counted, ~10-15 estimated)

Actual: 26 trades | 20 LEVEL-FADE, 6 MTF-SCALP, 0 others
```

---

## The Critical Divergence

| Aspect | LIVE | REPLAY |
|--------|------|--------|
| Primary engine | Entry/Sweep/Pullback | LevelRuntime zones |
| Secondary engine | LevelRuntime zones | Entry (diagnostic) |
| Families documented | BREAKOUT, SWEEP, PULLBACK | (none - uses undoc LEVEL-FADE, MTF-SCALP) |
| Scoring system | Entry: ≥80/100 | Zone: ready_confirmed only |
| Gate: Room-to-run | Checked | Checked |
| Gate: Range width | Not applicable | Checked |
| ORB evaluation | Entry engine | Diagnostic only |

---

## Conclusion

**Live and Replay trade completely different signal sources:**
- **Live**: Entry/Sweep/Pullback engines with detailed scoring
- **Replay**: LevelRuntime zone system with confirmation-only logic

This is the **root cause** of why:
- Replay fires 0 BREAKOUT (entry engine not used)
- Replay fires 0 PULLBACK (engine not called)
- Replay fires 77% LEVEL-FADE (zone system)
- Documentation doesn't mention LEVEL-FADE or MTF-SCALP

This is **NOT a bug** (both systems work), but it's **a documentation/design decision that's not explained**.

