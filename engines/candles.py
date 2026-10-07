"""Module 1b - 5-minute candle engine (Prompt B).

Primary source: NSE live-index chart API (same rate-limited session as the chain):
  /api/chart-databyindex-dynamic?index=NIFTY%2050&type=index   (verified working)
  /api/chart-databyindex?index=NIFTY%2050&indices=true         (legacy, empty shell)
Ticks are second-resolution [ts_ms, price, flag]; 'PO' marks pre-open and is
excluded. Timestamp quirk (verified): NSE encodes IST wall time as a UTC epoch, so
09:00:00 IST arrives as fromtimestamp(ts, UTC) == 09:00:00.

Fallback when the chart API yields nothing: spot samples (chart-adjacent sources,
yfinance 1m, chain underlying) bucketed into 5-min windows - candle close = last
sample in the window. Mid-day start: the first refresh backfills the whole day so
ORB (09:15-09:30 H/L) is immediately available.

Only CLOSED candles (start + 5 min <= now) are returned for evaluation.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import settings
from utils import ist_now

log = logging.getLogger(__name__)

CANDLE_MINUTES = 5


@dataclass
class Candle:
    start: datetime            # IST, window start (09:15, 09:20, ...)
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    source: str = "chart"      # chart | sample | yfinance
    interval_minutes: int = CANDLE_MINUTES
    tick_count: int | None = None

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.interval_minutes)


def _ts_to_ist(ms: float) -> datetime:
    """NSE chart quirk: IST wall time encoded as UTC epoch (09:00 IST -> ts that
    reads 09:00 in UTC). Convert to a proper IST datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=settings.IST)


def parse_chart_payload(raw, interval_minutes: int = CANDLE_MINUTES) -> list:
    """grapthData ticks -> closed-interval-ready 5-min buckets (open first, high
    max, low min, close last). Pre-open 'PO' ticks excluded."""
    ticks = (raw or {}).get("grapthData") or []
    buckets: dict = {}
    for point in ticks:
        try:
            ts, price = point[0], float(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        flag = point[2] if len(point) > 2 else ""
        if flag == "PO":
            continue
        tick_ts = _ts_to_ist(ts)
        start = tick_ts.replace(minute=(tick_ts.minute // interval_minutes) * interval_minutes,
                                second=0, microsecond=0)
        bucket = buckets.get(start)
        if bucket is None:
            buckets[start] = [price, price, price, price, 1]
        else:
            bucket[1] = max(bucket[1], price)
            bucket[2] = min(bucket[2], price)
            bucket[3] = price
            bucket[4] += 1
    return [Candle(start=start, open=v[0], high=v[1], low=v[2], close=v[3],
                   source="chart", interval_minutes=interval_minutes,
                   tick_count=v[4])
            for start, v in sorted(buckets.items())]


def orb_levels(candles: list, orb_start=None, orb_end=None):
    """ORB = high/low of the 09:15-09:30 five-minute candles (starts 09:15, 09:20,
    09:25). Returns {'high', 'low', 'mid', 'candles'} or None if incomplete."""
    orb_start = orb_start or settings.ORB_WINDOW_START
    orb_end = orb_end or settings.ORB_WINDOW_END
    window = [c for c in candles if orb_start <= c.start.time() < orb_end]
    if not window:
        return None
    high = max(c.high for c in window)
    low = min(c.low for c in window)
    return {"high": high, "low": low, "mid": (high + low) / 2, "candles": len(window)}


def yfinance_candles() -> list:
    """Backfill fallback: today's ^NSEI 5m bars from yfinance (true UTC stamps).
    Volume is usually 0/None for the index - kept as None then."""
    try:
        import yfinance as yf
        df = yf.Ticker("^NSEI").history(period="1d", interval="5m")
        candles = []
        for ts, row in df.iterrows():
            try:
                start = ts.tz_convert(settings.IST)
            except (TypeError, AttributeError):
                continue
            volume = row.get("Volume")
            candles.append(Candle(start=start, open=float(row["Open"]),
                                  high=float(row["High"]), low=float(row["Low"]),
                                  close=float(row["Close"]),
                                  volume=float(volume) if volume and volume > 0 else None,
                                  source="yfinance"))
        return candles
    except Exception as exc:
        log.warning("yfinance candle backfill failed: %s", exc)
        return []


class CandleEngine:
    """Holds today's merged closed candles; refresh() polls the chart API every
    CANDLE_POLL_INTERVAL_SEC and returns candles newly closed since last time."""

    def __init__(self, session=None, store=None):
        self._session = session
        self._store = store
        self.candles: list = []          # merged closed candles, ascending
        self.minute_candles: list = []  # closed 1-minute bars for the MTF engine
        self.fresh_minutes: list = []
        self._samples: list = []         # (datetime, price) fallback spot samples
        self._last_start: datetime | None = None
        self._last_minute_start: datetime | None = None
        self.last_status = None
        self.chart_ok = False

    # -- sources ------------------------------------------------------------
    def _fetch_chart(self) -> list:
        if self._session is None:
            return []
        for path in (settings.NSE_CHART_DYNAMIC_PATH, settings.NSE_CHART_LEGACY_PATH):
            url = settings.NSE_BASE_URL + path.format(index=quote(settings.NSE_CHART_INDEX))
            raw = self._session.get_json_once(url)
            self.last_status = self._session.last_status
            candles = parse_chart_payload(raw)
            if candles:
                self.minute_candles = parse_chart_payload(raw, interval_minutes=1)
                if not self.chart_ok:
                    log.info("Candle source locked in: %s (%d candles)",
                             endpoint_label(path), len(candles))
                self.chart_ok = True
                return candles
        self.chart_ok = False
        return []

    def add_spot_sample(self, price, ts: datetime | None = None) -> None:
        """Fallback source: one spot observation (chain underlying, yfinance, ...)."""
        if price is None:
            return
        self._samples.append((ts or ist_now(), float(price)))
        if len(self._samples) > 2000:
            self._samples = self._samples[-1000:]

    def _candles_from_samples(self, now: datetime,
                              interval_minutes: int = CANDLE_MINUTES) -> list:
        buckets: dict = {}
        for ts, price in self._samples:
            start = ts.replace(minute=(ts.minute // interval_minutes) * interval_minutes,
                               second=0, microsecond=0)
            bucket = buckets.get(start)
            if bucket is None:
                buckets[start] = [price, price, price, price]
            else:
                bucket[1] = max(bucket[1], price)
                bucket[2] = min(bucket[2], price)
                bucket[3] = price
        return [Candle(start=start, open=v[0], high=v[1], low=v[2], close=v[3],
                  source="sample", interval_minutes=interval_minutes)
              for start, v in sorted(buckets.items())
              if start + timedelta(minutes=interval_minutes) <= now]

    # -- main refresh ---------------------------------------------------------
    def refresh(self, now: datetime | None = None) -> list:
        """Poll sources, merge, persist; return candles newly CLOSED since the
        previous refresh (oldest first). On the first call of the day this is the
        whole backfill (mid-day start supported)."""
        now = now or ist_now()
        chart = [c for c in self._fetch_chart() if c.end <= now]
        merged: dict = {c.start: c for c in chart}
        for candle in self._candles_from_samples(now):
            merged.setdefault(candle.start, candle)
        self.candles = sorted(merged.values(), key=lambda c: c.start)

        fresh = [c for c in self.candles
                 if self._last_start is None or c.start > self._last_start]
        if self.candles:
            self._last_start = max(c.start for c in self.candles)
        if self._store is not None and fresh:
            try:
                self._store.upsert_candles(fresh)
            except Exception:
                log.exception("Candle persistence failed")

        minute_merged = {c.start: c for c in self.minute_candles if c.end <= now}
        for candle in self._candles_from_samples(now, interval_minutes=1):
            minute_merged.setdefault(candle.start, candle)
        self.minute_candles = sorted(minute_merged.values(), key=lambda c: c.start)
        self.fresh_minutes = [c for c in self.minute_candles
                              if self._last_minute_start is None
                              or c.start > self._last_minute_start]
        if self.minute_candles:
            self._last_minute_start = self.minute_candles[-1].start
        return fresh

    def opening_spot(self):
        """Day's opening print: first 09:15 candle open if present, else the first
        candle's open. Used for the STORED opening gap on mid-day starts."""
        for candle in self.candles:
            if candle.start.time() == settings.ORB_WINDOW_START:
                return candle.open
        return self.candles[0].open if self.candles else None

    def prev_candle(self, candle: Candle):
        """The candle immediately before `candle`, or None."""
        index = self.candles.index(candle) if candle in self.candles else len(self.candles) - 1
        return self.candles[index - 1] if index > 0 else None

    def recent_volumes(self, candle: Candle, window: int):
        """Volumes of the `window` candles before `candle` (None-padded source)."""
        try:
            index = self.candles.index(candle)
        except ValueError:
            return []
        return [c.volume for c in self.candles[max(0, index - window):index]
                if c.volume is not None]


def endpoint_label(path: str) -> str:
    return path.split("?")[0]
