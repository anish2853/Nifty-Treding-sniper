# 60-Day Replay Report

**MODE**: PRICE-ONLY VARIANT | threshold: 52/65
**Period**: 2026-09-29 to 2026-10-07
**Days Replayed**: 6 / 72 days

## Headline Results

| Metric | Value |
|--------|-------|
| Total Trades | 72 |
| Win Rate | 27.8% (20W / 52L) |
| Avg Win (R) | +1.47R |
| Avg Loss (R) | -2.32R |
| Expectancy (R) | -1.27R per trade |
| Expectancy (₹) | ₹-27385 (-380₹/trade after ₹150 cost) |
| Max Drawdown | -102.76R |
| Longest Streak | 16 losses |

## Gates

[✗] expectancy positive after costs
[✗] max DD < 20%
[✗] longest streak <= 7
[✓] trend periods net positive
[✓] range periods net positive

**Result**: ✗ SOME GATES FAIL

## By Setup Family

### LEVEL-FADE

| Metric | Value |
|--------|-------|
| Trades | 60 |
| Win Rate | 30.0% |
| Avg Win | +1.50R |
| Avg Loss | -2.36R |
| Expectancy | -1.20R |
| Max Streak | 18 |
| Total R | -71.96R |

### MTF-SCALP

| Metric | Value |
|--------|-------|
| Trades | 12 |
| Win Rate | 16.7% |
| Avg Win | +1.19R |
| Avg Loss | -2.17R |
| Expectancy | -1.61R |
| Max Streak | 8 |
| Total R | -19.32R |

## By Day State

### None

| Metric | Value |
|--------|-------|
| Trades | 72 |
| Win Rate | 27.8% |
| Expectancy | -1.27R |
| Total R | -91.28R |

## By Direction (Symmetry Check)

### LONG

| Metric | Value |
|--------|-------|
| Trades | 17 |
| Win Rate | 41.2% |
| Expectancy | -0.50R |
| Total R | -8.48R |

### SHORT

| Metric | Value |
|--------|-------|
| Trades | 55 |
| Win Rate | 23.6% |
| Expectancy | -1.51R |
| Total R | -82.81R |

## By Month

### 2026-09

| Metric | Value |
|--------|-------|
| Trades | 22 |
| Win Rate | 18.2% |
| Expectancy | -1.53R |
| Total R | -33.59R |

### 2026-10

| Metric | Value |
|--------|-------|
| Trades | 50 |
| Win Rate | 32.0% |
| Expectancy | -1.15R |
| Total R | -57.69R |

## Cost Impact

- **Cost per trade**: ₹150
- **Total cost**: ₹10800
- **Impact on expectancy**: ~₹150₹/trade (already deducted in results above)

## Notes

- All results are post-cost (₹150 per trade deducted)
- R-value is in standard position size units (1R = stop-loss distance)
- Price-only variant: no OI or volume components (not available in historical replay)
- Separate replay journal: `data/replay_journal.sqlite3`
