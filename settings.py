"""Central configuration.

EVERY numeric threshold from the strategy spec lives here - nothing is buried in
module code. Values are transcribed exactly as specified; anything the spec leaves
open is marked with an ASSUMPTION comment so it is a one-line change to override.
"""
import os
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
EXPORTS_DIR = DATA_DIR / "exports"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "journal.sqlite3"
LOG_FILE = LOGS_DIR / "trading_assistant.log"

# Credentials come only from config/.env - never hardcoded.
load_dotenv(CONFIG_DIR / ".env")

TZ_NAME = "Asia/Kolkata"
IST = ZoneInfo(TZ_NAME)

# --- Credentials / account (from .env) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CAPITAL = float(os.getenv("CAPITAL", "100000"))  # risk accounting (Phase B/C)

# --- Symbol ---
NIFTY_SYMBOL = "NIFTY"

# --- Market hours (IST) ---
PREMARKET_FETCH_TIME = dtime(8, 45)   # Module 1: global factors fetch ~08:45 IST
PREOPEN_START = dtime(9, 0)           # cookie warm-up allowed from here
MARKET_OPEN = dtime(9, 15)            # session 09:15-15:30
MARKET_CLOSE = dtime(15, 30)
ORB_WINDOW_START = dtime(9, 15)       # Module 3: ORB = 09:15-09:30 candles (Phase B)
ORB_WINDOW_END = dtime(9, 30)
ENTRY_EVAL_START = dtime(9, 30)       # entry engine evaluates from 09:30 (Phase B)
ENTRY_WINDOW_END = dtime(11, 0)       # +10 pts time window 09:30-11:00 (Phase B)
SQUARE_OFF_TIME = dtime(15, 10)       # 15:10 forced square-off alert for open trades
EOD_SUMMARY_TIME = dtime(15, 40)      # 15:40 EOD paper-accounting summary -> Telegram
SHUTDOWN_TIME = dtime(15, 45)         # 15:45 flush SQLite, close, clean sys.exit(0)

# --- NSE data access / rate limits ---
NSE_BASE_URL = "https://www.nseindia.com"
# Keep current-ish Chrome; NSE's Akamai layer checks header realism, not version.
NSE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
NSE_POLL_INTERVAL_SEC = 180           # option chain poll: every 180 s during market hours
NSE_MIN_REQUEST_GAP_SEC = 3           # hard limit: >= 3 s between ANY NSE requests
NSE_CHAIN_RETRIES = 5                 # chain fetch (pre-market AND market hours):
NSE_CHAIN_RETRY_GAP_SEC = 5           #   up to 5 cycles, 5 s apart

# Chain fetch (Prompt-4: NSE retired /api/option-chain-indices - it 404s now). The
# working contract, recovered from NSE's own option-chain page JS, is:
#   GET /api/option-chain-contract-info?symbol=NIFTY  -> {"expiryDates": [...]}
#   GET /api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=<expiry>   (per expiry)
# The bot runs that flow automatically and merges the nearest N expiries into one
# snapshot. Override with your own URL via .env (tried FIRST; the expiry value is
# auto-templated, or write {expiry} explicitly; a URL without expiry is tried as a
# single full-chain call):
#   NSE_CHAIN_ENDPOINT=https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=06-Oct-2026
NSE_CHAIN_ENDPOINT = os.getenv("NSE_CHAIN_ENDPOINT", "").strip()
NSE_CHAIN_MAX_EXPIRIES = 2            # nearest + NEXT expiry only (spec: single-fetch cycle)
# Liquidity map: only strikes within +/-2% of the stored spot may appear
LIQUIDITY_STRIKE_BAND_PCT = 0.02
# Legacy static endpoints, still probed as a last resort (NSE could restore them):
NSE_CHAIN_LEGACY_ENDPOINTS = [
    NSE_BASE_URL + f"/api/option-chain-indices?symbol={NIFTY_SYMBOL}",
    NSE_BASE_URL + f"/api/option-chain-symbol?symbol={NIFTY_SYMBOL}",
]
NSE_HTTP_TIMEOUT_SEC = 10
NSE_MAX_RETRIES = 3
NSE_BACKOFF_BASE_SEC = 2              # exponential backoff between attempts: 2s, 4s, 8s

# --- Module 1b: candle engine (Prompt B) ---
CANDLE_POLL_INTERVAL_SEC = 60         # chart API poll / spot-sample cadence
NSE_CHART_INDEX = "NIFTY 50"
# Live-index chart endpoints (legacy chart-databyindex returns an empty shell as of
# Oct 2026; the dynamic variant is what NSE's own index page calls - verified live,
# second-resolution ticks, 'PO' = pre-open flag, timestamps are IST wall time
# encoded as UTC epoch).
NSE_CHART_DYNAMIC_PATH = "/api/chart-databyindex-dynamic?index={index}&type=index"
NSE_CHART_LEGACY_PATH = "/api/chart-databyindex?index={index}&indices=true"

# --- Module 2: regime classifier ---
GAP_NORMAL_ABS = 0.003                # |gap| <= 0.3%  -> NORMAL (full playbook)
GAP_RESTRICTION_ABS = 0.006           # gap > +0.6% -> GAP-UP-EXTENSION; < -0.6% -> GAP-DOWN
VIX_SPIKE_PCT = 0.08                  # intraday India VIX > 8% above prev close -> flag
PCR_BULLISH_ABOVE = 1.2               # PCR > 1.2 bullish tilt
PCR_BEARISH_BELOW = 0.8               # PCR < 0.8 bearish tilt
PCR_MEAN_REVERSION_BELOW = 0.7        # PCR < 0.7 or > 1.4 -> mean-reversion regime
PCR_MEAN_REVERSION_ABOVE = 1.4        #   (suppresses breakout entries from Phase B on)

# --- Module 3: entry engine (Phase B — frozen, do NOT tune) ---
ENTRY_SCORE_FIRE = 80                 # fire alert only if score >= 80
ENTRY_SCORE_JOURNAL_MIN = 55          # 55-79: no alert, journal only
W_LEVEL_BREAK = 20                    # 5-min candle CLOSES above ORB high or PDH
W_CONFIRMATION = 20                   # next candle also closes above level
W_OI_CONFIRMATION = 20                # call OI just above spot falling (writers covering)
W_VOLUME = 15                         # breakout candle volume > 10-candle average
W_REGIME_VIX = 15                     # regime allows direction, VIX stable, not event day
W_TIME_WINDOW = 10                    # 09:30-11:00
VOLUME_AVG_WINDOW = 10                # 10-candle average
# ASSUMPTION: spec says "over last 2-3 chain snapshots" - pinned to 3 (most
# conservative reading). Change here if you want 2.
OI_CONFIRM_SNAPSHOTS = 3
# ASSUMPTION: "strikes just beyond spot" - the 3 nearest strikes beyond spot on the
# relevant side, nearest expiry (any rising change-in-OI among them = REJECT).
OI_SPOT_ADJACENT_COUNT = 3
STRIKE_ROUND_TO = 50                  # suggested strike = spot rounded to nearest 50
MAX_TRADES_PER_DAY = 2
MAX_TRADES_PER_DIRECTION = 1

# --- Trade card / paper accounting (Prompt B — frozen) ---
LOT_SIZE = 75                         # 1 lot = 75 qty
ENTRY_PRICE_BAND = 10                 # entry band: LTP .. LTP + 10
SL_PREMIUM_FRACTION = 0.75            # SL prem = 0.75 x LTP (exit when LTP <= this)
PREMIUM_BETA = 0.5                    # premium moves ~ 0.5 x spot points
RISK_PREM_FRACTION = 0.25             # max risk = 0.25 x LTP x LOT_SIZE
PARTIAL_BOOK_FRACTION = 0.5           # at +1R: BOOK 50%, SL to breakeven
TIME_STOP_MINUTES = 75                # Prompt B pins the 60-75 band at 75
# 1R is defined in SPOT terms: 1R = |entry_spot - SL_spot| (ORB midpoint);
# premium targets = entry LTP + PREMIUM_BETA x target points.

# --- Phase B+: liquidity map + sweep-fade (frozen) ---
LIQUIDITY_TOP_N = 3                   # S1-S3 / R1-R3 (spec: top 3 OI strikes per side)
STRONG_LEVEL_BAND_PCT = 0.0015        # STRONG tag: OI level within 0.15% of PDH/PDL
SWEEP_REJECT_FRACTION = 0.25          # rejection: close back inside by >25% of range
SWEEP_SL_BUFFER_PTS = 5               # SL spot = sweep extreme +/- 5 pts
SWEEP_WINDOW_2_START = dtime(13, 30)  # sweep-fade second window
SWEEP_WINDOW_2_END = dtime(14, 45)

# --- Phase B+ hotfix: hard data sanity gates (frozen - bad data never fires) ---
SPOT_PDC_MAX_DEVIATION = 0.03         # spot within +/-3% of PDC, else suppress ALL signals
ATM_BAND_PCT = 0.01                   # traded ATM strike must be within +/-1% of spot
OPTION_LTP_MIN = 0.5                  # plausible option LTP floor (Rs)
OPTION_LTP_MAX_PCT_OF_SPOT = 0.10     # plausible option LTP ceiling: 10% of spot
TRADE_EXPIRY_MIN_TRADING_DAYS = 2     # current weekly ONLY if >=2 trading days remain

# --- Freshness gate (hotfix: signals fire ONLY on the latest closed candle) ---
STALE_CANDLE_MAX_AGE_SEC = 360        # candles older than 6 min: STALE, never fire
SPOT_STALENESS_BAND_PCT = 0.0025      # |store spot - candle close| > 0.25% -> stale
FIRE_SLO_SECONDS = 90                 # operational SLO: fire within 90 s of close
# (enforcement = 60 s loop + the 6-minute hard gate above)

# --- Trend-Day Engine v2 (frozen) ---
EMA_SPAN = 20                         # EMA-20 on 5-min closes
LAST_ENTRY_TIME = dtime(14, 30)       # NO new entries after 14:30 (any setup/state)
PROFIT_LOCK_R = 2.0                   # day total reaches +2R -> bank it, stop trading
# Day states: TREND-UP / TREND-DOWN / RANGE (engines/day_state.py).
# TREND: entries 09:30-14:30 while structure holds; RANGE: sweep-fade only.
# Time stop (75 min) applies ONLY in RANGE state; trend runners trail structure.

# --- Room-to-run gate + dynamic budget (Trend-Day v2.1, frozen) ---
ROOM_TO_RUN_MIN_PTS = 25              # runway to the nearest significant wall: >= 25 pts
WALL_OI_MIN = 100000                  # significant wall: OI > 100k
LEVEL_CONFLUENCE_PCT = 0.001          # strong wall aligns with structure within 0.1%
LEVEL_TEST_TOLERANCE_PTS = 5          # touch band used to identify a tested/held level
LEVEL_APPROACH_PTS = 25               # alert/decision radius around ladder levels
LEVEL_ZONE_MERGE_PTS = 10             # merge nearby prices into one watch zone
LEVEL_WATCH_COOLDOWN_SEC = 1800       # per-zone watch rate limit
LEVEL_WATCH_RETURN_PTS = 15           # must leave the zone by this much before re-alert
RANGE_WIDTH_MIN_PTS = 45              # RANGE is disabled when strong S-to-R span is smaller
DIVERGENCE_RSI_PERIOD = 14            # RSI on 5-minute closes
DIVERGENCE_SUPPRESS_CANDLES = 6       # continuation pause after a divergence trap

# --- Coil-Snipe consolidation engine ---
COIL_MIN_CANDLES = 8                  # at least 8 consecutive 5-minute bars
COIL_MAX_POINTS = 65                  # absolute maximum consolidation width
COIL_MAX_SPOT_FRACTION = 0.003        # or 0.3% of spot, whichever is smaller
COIL_TOUCHES_PER_BOUNDARY = 2         # minimum tests at both range boundaries
COIL_TOUCH_TOLERANCE_PTS = 4          # absolute boundary touch tolerance
COIL_TOUCH_TOLERANCE_FRACTION = 0.10  # or 10% of coil width
COIL_EMA9_MIN_CROSSES = 2             # repeated close/EMA-9 crossings
COIL_SEARCH_CANDLES = 32              # maximum suffix lookback for coil/nesting
COIL_LAST_ENTRY_TIME = dtime(14, 30)  # 40 min before 15:10 forced square-off
BUDGET_TREND = 4                      # entries on TREND days
BUDGET_RANGE = 2                      # entries on RANGE days (unknown state -> range)
COOLDOWN_TREND_SEC = 1200             # 20 min between entries on trend days
COOLDOWN_RANGE_SEC = 1800             # 30 min on range days
CONSECUTIVE_LOSS_HALT = 2             # 2 consecutive full losses -> shutdown for day
FULL_LOSS_R = -0.99                   # a "full loss" = -1R or worse

# --- MTF scalp (1m zone + 5m confirmation) ---
MTF_MAX_RISK_POINTS = 25              # max entry-to-stop distance after confirmation drift
MTF_RISK_UNITS = 0.5                  # paper size multiplier; daily entry budget unchanged
MTF_BOOK_FRACTION = 0.8               # first target / OI flip books 80%, runner keeps 20%
MTF_TARGET_R = 1.5                    # alternate first-book target when no nearer liquidity zone

# --- Module 5: risk manager (Phase B) ---
DAILY_LOSS_CAP_PCT = 0.05             # 5% of capital -> SHUTDOWN FOR DAY
CONSECUTIVE_LOSS_PAUSE = 3            # 3 consecutive losing trades ...
PAUSE_DAYS = 2                        # ... -> 2-day pause flag

# --- Journal / logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# --- Config data files (user-editable) ---
HOLIDAYS_FILE = CONFIG_DIR / "holidays.json"        # {"dates": ["YYYY-MM-DD", ...]}
EVENT_CALENDAR_FILE = CONFIG_DIR / "event_calendar.json"

# --- GIFT Nifty (best effort per spec; never blocks the pipeline) ---
# No documented public JSON API exists on nseix.com (checked Oct 2026). Sources are
# tried in order: JSON responses are deep-searched for a GIFT NIFTY price, HTML is
# regex-scraped. Total failure stays SILENT (MISSING, logged only) - and it never
# feeds the 09:15 regime, which always uses the actual chain spot vs PDC.
GIFT_NIFTY_SOURCES = [
    "https://www.moneycontrol.com/live-index/gift-nifty?symbol=in;gsx",
    "https://www.nseix.com/",
    "https://www.nseix.com/market-data/live-indices",
]
GIFT_NIFTY_HTTP_TIMEOUT_SEC = 10

# --- India VIX ---
INDIA_VIX_URL = "https://www.moneycontrol.com/indian-indices/India-VIX-36.html"

# --- Telegram ---
TELEGRAM_TIMEOUT_SEC = 10
TELEGRAM_RETRIES = 3                  # retry 3x, then log and continue (never crash)

# --- Watchdog / resilience (daily-worker spec) ---
WATCHDOG_RESTART_LIMIT = 5            # max in-process restarts per day, then shutdown
WATCHDOG_RESTART_WAIT_SEC = 60        # pause between a crash and the restart
FEED_FAILURE_ALERT_THRESHOLD = 5      # consecutive failures before a feed alarm
