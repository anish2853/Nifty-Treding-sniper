# 60-Day Replay Report

**MODE**: PRICE-ONLY VARIANT | threshold: 52/65
**Period**: 2026-10-07 to 2026-10-07
**Days Replayed**: 1 / 46 days

## Headline Results

| Metric | Value |
|--------|-------|
| Total Trades | 46 |
| Win Rate | 32.6% (15W / 31L) |
| Avg Win (R) | +1.51R |
| Avg Loss (R) | -2.41R |
| Expectancy (R) | -1.13R per trade |
| Expectancy (₹) | ₹-15628 (-340₹/trade after ₹150 cost) |
| Max Drawdown | -61.93R |
| Longest Streak | 12 losses |

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
| Trades | 40 |
| Win Rate | 35.0% |
| Avg Win | +1.54R |
| Avg Loss | -2.46R |
| Expectancy | -1.06R |
| Max Streak | 12 |
| Total R | -42.44R |

### MTF-SCALP

| Metric | Value |
|--------|-------|
| Trades | 6 |
| Win Rate | 16.7% |
| Avg Win | +1.19R |
| Avg Loss | -2.17R |
| Expectancy | -1.61R |
| Max Streak | 4 |
| Total R | -9.66R |

## By Day State

### None

| Metric | Value |
|--------|-------|
| Trades | 46 |
| Win Rate | 32.6% |
| Expectancy | -1.13R |
| Total R | -52.09R |

## By Direction (Symmetry Check)

### LONG

| Metric | Value |
|--------|-------|
| Trades | 11 |
| Win Rate | 54.5% |
| Expectancy | -0.03R |
| Total R | -0.36R |

### SHORT

| Metric | Value |
|--------|-------|
| Trades | 35 |
| Win Rate | 25.7% |
| Expectancy | -1.48R |
| Total R | -51.73R |

## By Month

### 2026-09

| Metric | Value |
|--------|-------|
| Trades | 11 |
| Win Rate | 18.2% |
| Expectancy | -1.53R |
| Total R | -16.80R |

### 2026-10

| Metric | Value |
|--------|-------|
| Trades | 35 |
| Win Rate | 37.1% |
| Expectancy | -1.01R |
| Total R | -35.30R |

## Cost Impact

- **Cost per trade**: ₹150
- **Total cost**: ₹6900
- **Impact on expectancy**: ~₹150₹/trade (already deducted in results above)

## Notes

- All results are post-cost (₹150 per trade deducted)
- R-value is in standard position size units (1R = stop-loss distance)
- Price-only variant: no OI or volume components (not available in historical replay)
- Separate replay journal: `data/replay_journal.sqlite3`
