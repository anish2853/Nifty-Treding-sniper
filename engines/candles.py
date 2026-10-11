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
    candles_5m, _ = yfinance_all_candles()
    return candles_5m


def yfinance_all_candles() -> tuple[list, list]:
    """Fetch both 5m and 1m candles for today from yfinance."""
    try:
        import yfinance as yf
        ticker = yf.Ticker("^NSEI")
        df_5m = ticker.history(period="1d", interval="5m")
        candles_5m = []
        for ts, row in df_5m.iterrows():
            try:
                start = ts.tz_convert(settings.IST)
            except (TypeError, AttributeError):
                continue
            volume = row.get("Volume")
            candles_5m.append(Candle(start=start, open=float(row["Open"]),
                                     high=float(row["High"]), low=float(row["Low"]),
                                     close=float(row["Close"]),
                                     volume=float(volume) if volume and volume > 0 else None,
                                     source="yfinance", interval_minutes=5))

        df_1m = ticker.history(period="1d", interval="1m")
        candles_1m = []
        for ts, row in df_1m.iterrows():
            try:
                start = ts.tz_convert(settings.IST)
            except (TypeError, AttributeError):
                continue
            volume = row.get("Volume")
            candles_1m.append(Candle(start=start, open=float(row["Open"]),
                                     high=float(row["High"]), low=float(row["Low"]),
                                     close=float(row["Close"]),
                                     volume=float(volume) if volume and volume > 0 else None,
                                     source="yfinance", interval_minutes=1))
        return candles_5m, candles_1m
    except Exception as exc:
        log.warning("yfinance candle backfill failed: %s", exc)
        return [], []


class CandleEngine:
    """Holds today's merged closed candles; refresh() polls the chart API every
    CANDLE_POLL_INTERVAL_SEC and returns candles newly closed since last time."""

    def __init__(self, session=None, store=None):
        self._session = session
        self._store = store
        self.candles: list = []          # merged closed candles, ascending
        self.minute_candles: list = []  # closed 1-minute bars for the MTF engine
        self.fresh_minutes: list = []
        self._samples: list = []         # (datetime, price, source) fallback spot samples
        self._last_start: datetime | None = None
        self._last_minute_start: datetime | None = None
        self.last_status = None
        self.chart_ok = False
        self.chart_fail_count: int = 0
        self.last_active_source: str = "chart"

    # -- sources ------------------------------------------------------------
    def _fetch_chart(self) -> list:
        if self._session is None:
            return []

        # Session handshake check: ensure cookies exist before API call
        if hasattr(self._session, "_session") and not self._session._session.cookies:
            log.info("[CANDLE FETCH] Session cookies missing - performing handshake first")
            self._session.warm_up()

        for path in (settings.NSE_CHART_DYNAMIC_PATH, settings.NSE_CHART_LEGACY_PATH):
            url = settings.NSE_BASE_URL + path.format(index=quote(settings.NSE_CHART_INDEX))
            raw = self._session.get_json_once(url)
            self.last_status = self._session.last_status
            resp_len = len(str(raw)) if raw else 0
            log.info("[CANDLE FETCH] %s -> HTTP %s (payload: %d chars)",
                     endpoint_label(path), self.last_status, resp_len)

            # 401/403/404 or empty response: session handshake broken - refresh cookies & retry
            if self.last_status in (401, 403, 404, 429) or (self.last_status == 200 and not raw):
                log.warning("[CANDLE FETCH] HTTP %s on %s - session handshake broken, refreshing cookies",
                            self.last_status, endpoint_label(path))
                if hasattr(self._session, "recreate_session"):
                    self._session.recreate_session()
                else:
                    self._session.warm_up()
                time.sleep(1.0)
                raw = self._session.get_json_once(url)
                self.last_status = self._session.last_status
                resp_len = len(str(raw)) if raw else 0
                log.info("[CANDLE FETCH RETRY] %s -> HTTP %s (payload: %d chars)",
                         endpoint_label(path), self.last_status, resp_len)

            candles = parse_chart_payload(raw)
            if candles:
                self.minute_candles = parse_chart_payload(raw, interval_minutes=1)
                log.info("[CANDLE PARSE] %s -> %d 5m candles, %d 1m candles parsed",
                         endpoint_label(path), len(candles), len(self.minute_candles))
                if not self.chart_ok:
                    log.info("Candle source locked in: %s (%d candles)",
                             endpoint_label(path), len(candles))
                self.chart_ok = True
                return candles

        self.chart_ok = False
        return []

    def add_spot_sample(self, price, ts: datetime | None = None, source: str = "chain") -> None:
        """Add spot observation from chain underlying or other reliable live feed."""
        if price is None or price <= 0:
            return
        t = ts or ist_now()
        self._samples.append((t, float(price), source))
        if len(self._samples) > 3000:
            self._samples = self._samples[-1500:]

    def _candles_from_samples(self, now: datetime,
                              interval_minutes: int = CANDLE_MINUTES) -> list:
        return self._synthesize_candles_from_samples(now, interval_minutes=interval_minutes)

    def _synthesize_candles_from_samples(
        self, now: datetime, interval_minutes: int = CANDLE_MINUTES
    ) -> list[Candle]:
        """Synthesize 5m or 1m OHLC candles from chain spot poll samples (Phase 13.5).
        
        Tags synthesized candles with source='SYNTH'.
        """
        buckets: dict[datetime, list[float]] = {}
        for item in self._samples:
            ts = item[0]
            price = item[1]
            bucket_start = ts.replace(
                minute=(ts.minute // interval_minutes) * interval_minutes,
                second=0,
                microsecond=0,
            )
            if bucket_start.date() != now.date():
                continue
            if bucket_start not in buckets:
                buckets[bucket_start] = []
            buckets[bucket_start].append(price)

        synth_candles: list[Candle] = []
        for start, prices in sorted(buckets.items()):
            interval_end = start + timedelta(minutes=interval_minutes)
            if interval_end <= now and prices:
                synth_candles.append(
                    Candle(
                        start=start,
                        open=prices[0],
                        high=max(prices),
                        low=min(prices),
                        close=prices[-1],
                        volume=0.0,
                        source="SYNTH",
                        interval_minutes=interval_minutes,
                    )
                )
        return synth_candles

    # -- main refresh ---------------------------------------------------------
    def refresh(self, now: datetime | None = None) -> list:
        """Poll sources, merge, persist; return candles newly CLOSED since the
        previous refresh (oldest first). On the first call of the day this is the
        whole backfill (mid-day start supported)."""
        now = now or ist_now()
        chart = [c for c in self._fetch_chart() if c.end <= now]
        chart_lag = max(0.0, (now - chart[-1].end).total_seconds() / 60.0) if chart else 999.0

        # Track chart failure / stall count
        if not chart or chart_lag > 5.0:
            self.chart_fail_count += 1
            log.warning(
                "[CANDLE ENGINE] Chart API lagging (%.1fm) or empty (consecutive fail count: %d)",
                chart_lag, self.chart_fail_count,
            )
        else:
            self.chart_fail_count = 0

        # CHAIN-SPOT CANDLE SYNTHESIS FALLBACK (Phase 13.5):
        # Priority: chart API primary -> chain-synthesis fallback -> blindness only if BOTH die.
        synth_5m = self._synthesize_candles_from_samples(now, interval_minutes=5)
        synth_1m = self._synthesize_candles_from_samples(now, interval_minutes=1)

        # Merge 5-minute candles: start with today's existing candles, overlay official chart candles
        merged_5m: dict[datetime, Candle] = {
            c.start: c for c in self.candles if c.start.date() == now.date()
        }
        for c in chart:
            merged_5m[c.start] = c

        if self.chart_fail_count >= 2 or not chart or chart_lag > 5.0:
            for sc in synth_5m:
                if sc.start not in merged_5m:
                    merged_5m[sc.start] = sc
                    log.info("[CANDLE SYNTHESIS] Bridged missing 5m interval with SYNTH candle: %s (O:%.2f H:%.2f L:%.2f C:%.2f)",
                             sc.start.strftime("%H:%M"), sc.open, sc.high, sc.low, sc.close)
                elif merged_5m[sc.start].source != "chart":
                    merged_5m[sc.start] = sc
        else:
            for sc in synth_5m:
                if sc.start not in merged_5m:
                    merged_5m[sc.start] = sc

        self.candles = sorted(merged_5m.values(), key=lambda c: c.start)
        if self.candles:
            self.last_active_source = self.candles[-1].source

        fresh = [c for c in self.candles
                 if self._last_start is None or c.start > self._last_start]
        if self.candles:
            self._last_start = max(c.start for c in self.candles)

        # Store persistence: upsert all closed candles to ensure DB is complete
        if self._store is not None and self.candles:
            try:
                self._store.upsert_candles(self.candles)
                today_iso = now.date().isoformat()
                verified = self._store.fetch_candles(today_iso)
                log.info("[CANDLE STORE] Upserted %d candles to store; verified read-back: %d candles in DB",
                         len(self.candles), len(verified))
            except Exception:
                log.exception("[CANDLE STORE] Candle persistence failed")

        minute_merged: dict[datetime, Candle] = {c.start: c for c in self.minute_candles if c.end <= now}
        if self.chart_fail_count >= 2 or not self.minute_candles or chart_lag > 5.0:
            for sc in synth_1m:
                if sc.start not in minute_merged:
                    minute_merged[sc.start] = sc
                elif minute_merged[sc.start].source != "chart":
                    minute_merged[sc.start] = sc
        else:
            for sc in synth_1m:
                if sc.start not in minute_merged:
                    minute_merged[sc.start] = sc
        self.minute_candles = sorted(minute_merged.values(), key=lambda c: c.start)
        self.fresh_minutes = [c for c in self.minute_candles
                              if self._last_minute_start is None
                              or c.start > self._last_minute_start]
        if self.minute_candles:
            self._last_minute_start = self.minute_candles[-1].start
        return fresh

    def latest_candle_age_minutes(self, now: datetime | None = None) -> float | None:
        """Minutes since the latest closed 5-minute candle ended."""
        if not self.candles:
            return None
        now = now or ist_now()
        return max(0.0, (now - self.candles[-1].end).total_seconds() / 60.0)

    def is_blind(self, now: datetime | None = None) -> bool:
        """True if during market hours (09:15-15:30) and candle age > 5.5 min (or 0 candles)."""
        now = now or ist_now()
        if not (settings.MARKET_OPEN <= now.time() <= settings.MARKET_CLOSE):
            return False
        age = self.latest_candle_age_minutes(now)
        return age is None or age > 5.5

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
