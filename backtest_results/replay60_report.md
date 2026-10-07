# 60-Day Replay Report

**MODE**: PRICE-ONLY VARIANT | threshold: 52/65
**Period**: 2026-10-07 → 2026-10-07
**Days Replayed**: 1 / 34 days

## Headline Results

| Metric | Value |
|--------|-------|
| Total Trades | 34 |
| Win Rate | 26.5% (9W / 25L) |
| Avg Win (R) | +1.46R |
| Avg Loss (R) | -2.30R |
| Expectancy (R) | -1.30R per trade |
| Expectancy (₹) | ₹-13305 (-391₹/trade after ₹150 cost) |
| Max Drawdown | -49.27R |
| Longest Streak | 8 losses |

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
| Trades | 28 |
| Win Rate | 28.6% |
| Avg Win | +1.49R |
| Avg Loss | -2.33R |
| Expectancy | -1.24R |
| Max Streak | 9 |
| Total R | -34.69R |

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
| Trades | 34 |
| Win Rate | 26.5% |
| Expectancy | -1.30R |
| Total R | -44.35R |

## By Direction (Symmetry Check)

### LONG

| Metric | Value |
|--------|-------|
| Trades | 8 |
| Win Rate | 37.5% |
| Expectancy | -0.63R |
| Total R | -5.01R |

### SHORT

| Metric | Value |
|--------|-------|
| Trades | 26 |
| Win Rate | 23.1% |
| Expectancy | -1.51R |
| Total R | -39.34R |

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
| Trades | 23 |
| Win Rate | 30.4% |
| Expectancy | -1.20R |
| Total R | -27.55R |

## Cost Impact

- **Cost per trade**: ₹150
- **Total cost**: ₹5100
- **Impact on expectancy**: ~₹150₹/trade (already deducted in results above)

## Notes

- All results are post-cost (₹150 per trade deducted)
- R-value is in standard position size units (1R = stop-loss distance)
- Price-only variant: no OI or volume components (not available in historical replay)
- Separate replay journal: `data/replay_journal.sqlite3`
