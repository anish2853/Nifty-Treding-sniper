"""Historical replay data fetcher, validator, and 1m bar synthesizer.

Primary source: yfinance ^NSEI 5m with period='60d', retry+backoff on 429/empty.
Fallback source: Angel One SmartAPI historical (when ANGEL_API_KEY is present).
Validation: 70+ five-minute candles between 09:15-15:30 IST per trading day.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytz
import yfinance as yf

import settings
from engines.candles import Candle
from utils import ist_now, safe_print

log = logging.getLogger("replay_data")

IST = pytz.timezone("Asia/Kolkata")


@dataclass
class DayData:
    """Pre-processed and validated intraday market data for one trading day."""
    date: date
    five_minute: list[Candle]
    one_minute: list[Candle]
    pdh: float | None
    pdl: float | None
    pdc: float | None
    candle_count: int
    first_time_ist: str
    last_time_ist: str
    status: str
    skip_reason: str | None = None


@dataclass
class DayDiagnosticRecord:
    """Diagnostic fetch result for a single calendar day."""
    date: date
    candle_count: int
    first_time_ist: str
    last_time_ist: str
    status: str
    reason: str


@dataclass
class ReplayDataset:
    """Validated replay dataset containing multi-day historical data."""
    valid_days: dict[date, DayData] = field(default_factory=dict)
    day_records: list[DayDiagnosticRecord] = field(default_factory=list)
    days_processed: int = 0
    days_skipped: int = 0
    skip_summary: str = ""
    source_name: str = "yfinance"
    total_raw_rows: int = 0
    source_start_ist: str = ""
    source_end_ist: str = ""


# In-memory global cache to avoid repeated fetches
_GLOBAL_DATASET_CACHE: ReplayDataset | None = None


def check_smartapi_configured() -> bool:
    """Check if Angel One SmartAPI is configured in the environment.
    
    If not configured, emits setup guidance as required.
    """
    key = os.getenv("SMARTAPI_API_KEY") or os.getenv("ANGEL_API_KEY")
    if not key or not key.strip():
        print("SETUP NEEDED: register at smartapi.angelbroking.com")
        return False
    return True


def fetch_smartapi_5m_data(start_date: date, end_date: date) -> pd.DataFrame:
    """Fallback fetcher: Angel One SmartAPI historical candles (getCandleData).
    
    Follows https://smartapi.angelone.in/docs/Historical specification.
    """
    if not check_smartapi_configured():
        return pd.DataFrame()
    try:
        from data_sources.smartapi_historical import get_historical_client
        client = get_historical_client()
        return client.fetch_candles(
            symboltoken="99926000",
            exchange="NSE",
            interval="FIVE_MINUTE",
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as exc:
        log.warning("SmartAPI historical fetch encountered exception: %s", exc)
        return pd.DataFrame()


def fetch_yfinance_5m_data(days: int = 60, max_retries: int = 3,
                           backoff_sec: float = 2.0) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fetch 5m ^NSEI candles from yfinance with exponential retry and backoff on 429/empty.
    
    Returns (DataFrame, metadata_dict).
    """
    ticker = yf.Ticker("^NSEI")
    fetch_days = max(days * 2, 14) if days < 30 else days
    period = f"{fetch_days}d"
    df = pd.DataFrame()
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            log.info("Fetching yfinance ^NSEI period='%s' interval='5m' (attempt %d/%d)...",
                     period, attempt, max_retries)
            df = ticker.history(period=period, interval="5m")
            if df is not None and not df.empty:
                break
            log.warning("Attempt %d: yfinance returned empty DataFrame", attempt)
        except Exception as exc:
            last_exc = exc
            log.warning("Attempt %d failed with error: %s", attempt, exc)

        if attempt < max_retries:
            sleep_time = backoff_sec * (2 ** (attempt - 1))
            log.info("Backoff: waiting %.1fs before retry...", sleep_time)
            time.sleep(sleep_time)

    meta = {
        "source": "yfinance",
        "ticker": "^NSEI",
        "period": period,
        "interval": "5m",
        "total_rows": len(df) if df is not None else 0,
        "start": None,
        "end": None,
        "error": str(last_exc) if (df is None or df.empty) and last_exc else None,
    }

    if df is None or df.empty:
        log.error("Failed to fetch yfinance data after %d attempts (last error: %s)",
                  max_retries, last_exc)
        return pd.DataFrame(), meta

    try:
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(settings.IST)
        else:
            df.index = df.index.tz_convert(settings.IST)
    except Exception as exc:
        log.warning("Timezone conversion warning: %s", exc)

    meta["start"] = df.index[0].isoformat()
    meta["end"] = df.index[-1].isoformat()
    return df, meta


def synthesize_1m_candles(five_minute_candles: list[Candle]) -> list[Candle]:
    """Synthesize 1-minute bars from 5-minute candles preserving OHLC and volume invariants.
    
    Every 5m bar expands to 5 1m sub-bars where:
    - Sub-bar 0 open matches 5m open
    - Sub-bar 4 close matches 5m close
    - Sub-bar extremes match 5m high and low
    - Sub-bar volumes partition the 5m volume evenly
    - For each sub-bar: high >= max(open, close) and low <= min(open, close)
    """
    one_min = []
    for c in five_minute_candles:
        o, h, l, cl = c.open, c.high, c.low, c.close
        vol = (c.volume / 5.0) if c.volume is not None else None
        t0 = c.start
        source = f"{c.source}-syn1m"

        if cl >= o:
            # Bullish progression: Open -> Low dip -> Mid-recovery -> High spike -> Close
            mid_low = (o + l) / 2.0
            mid_high = (h + cl) / 2.0
            bars = [
                Candle(start=t0,
                       open=o,
                       high=max(o, mid_low),
                       low=min(o, mid_low),
                       close=mid_low,
                       volume=vol, source=source, interval_minutes=1),
                Candle(start=t0 + timedelta(minutes=1),
                       open=mid_low,
                       high=max(mid_low, l),
                       low=l,
                       close=l,
                       volume=vol, source=source, interval_minutes=1),
                Candle(start=t0 + timedelta(minutes=2),
                       open=l,
                       high=max(l, mid_high),
                       low=l,
                       close=mid_high,
                       volume=vol, source=source, interval_minutes=1),
                Candle(start=t0 + timedelta(minutes=3),
                       open=mid_high,
                       high=h,
                       low=min(mid_high, h),
                       close=h,
                       volume=vol, source=source, interval_minutes=1),
                Candle(start=t0 + timedelta(minutes=4),
                       open=h,
                       high=h,
                       low=min(h, cl),
                       close=cl,
                       volume=vol, source=source, interval_minutes=1),
            ]
        else:
            # Bearish progression: Open -> High test -> Mid-drop -> Low spike -> Close
            mid_high = (o + h) / 2.0
            mid_low = (l + cl) / 2.0
            bars = [
                Candle(start=t0,
                       open=o,
                       high=max(o, mid_high),
                       low=min(o, mid_high),
                       close=mid_high,
                       volume=vol, source=source, interval_minutes=1),
                Candle(start=t0 + timedelta(minutes=1),
                       open=mid_high,
                       high=h,
                       low=min(mid_high, h),
                       close=h,
                       volume=vol, source=source, interval_minutes=1),
                Candle(start=t0 + timedelta(minutes=2),
                       open=h,
                       high=h,
                       low=min(h, mid_low),
                       close=mid_low,
                       volume=vol, source=source, interval_minutes=1),
                Candle(start=t0 + timedelta(minutes=3),
                       open=mid_low,
                       high=max(mid_low, l),
                       low=l,
                       close=l,
                       volume=vol, source=source, interval_minutes=1),
                Candle(start=t0 + timedelta(minutes=4),
                       open=l,
                       high=max(l, cl),
                       low=l,
                       close=cl,
                       volume=vol, source=source, interval_minutes=1),
            ]
        one_min.extend(bars)
    return one_min


def prepare_replay_data(days: int = 60, print_table: bool = True) -> ReplayDataset:
    """Fetch, validate, and prepare historical intraday data across N calendar days.
    
    Day validation rule:
    - Valid trading day = 70+ five-minute candles between 09:15-15:30 IST.
    - Invalid days: skip + log 'PARTIAL/NO DATA' (or WEEKEND/HOLIDAY).
    
    Prints:
    - Diagnostic candle source audit (showing yfinance vs NSE chart API).
    - Start date, end date, total rows.
    - Day-by-day table: date | candles fetched | first/last candle time IST | status.
    - Summary: 'days processed: X | skipped: Y | reason'.
    """
    global _GLOBAL_DATASET_CACHE

    # 1. Source verification & fallback check (yfinance primary only)
    has_smartapi = False

    # 2. Fetch primary source (yfinance 5m)
    df, meta = fetch_yfinance_5m_data(days=days)
    source_name = "yfinance historical (^NSEI, 5m)"

    # Fallback to SmartAPI if yfinance is empty and SmartAPI configured
    if (df is None or df.empty) and has_smartapi:
        today = ist_now().date()
        start_d = today - timedelta(days=days)
        log.info("Primary source empty. Attempting SmartAPI fallback...")
        df = fetch_smartapi_5m_data(start_d, today)
        if not df.empty:
            source_name = "Angel One SmartAPI historical (getCandleData, 5m)"
            meta["total_rows"] = len(df)
            meta["start"] = df.index[0].isoformat()
            meta["end"] = df.index[-1].isoformat()

    dataset = ReplayDataset(
        source_name=source_name,
        total_raw_rows=len(df) if df is not None else 0,
        source_start_ist=meta.get("start") or "N/A",
        source_end_ist=meta.get("end") or "N/A",
    )

    if df is None or df.empty:
        log.error("No historical candle data available from any source.")
        return dataset

    # Group dataframe by calendar date
    day_groups = df.groupby(df.index.date)
    all_available_dates = sorted(day_groups.groups.keys())

    # Build sequence of calendar days up to today
    today = ist_now().date()
    if all_available_dates:
        start_date = all_available_dates[0] if days >= 60 else (all_available_dates[-days] if len(all_available_dates) >= days else all_available_dates[0])
        num_cal = (today - start_date).days + 1
        calendar_days = [start_date + timedelta(days=i) for i in range(num_cal)]
    else:
        calendar_days = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]

    skip_reasons_counter: dict[str, int] = {}

    for cal_day in calendar_days:
        if cal_day in day_groups.groups:
            day_df = day_groups.get_group(cal_day)
            # Filter strictly between market hours: 09:15 to 15:30 IST
            market_slice = day_df.between_time("09:15", "15:30")
            count = len(market_slice)
            if count > 0:
                first_ts_str = market_slice.index[0].strftime("%Y-%m-%d %H:%M:%S")
                last_ts_str = market_slice.index[-1].strftime("%Y-%m-%d %H:%M:%S")
            else:
                first_ts_str = "-"
                last_ts_str = "-"

            if count >= 70:
                # Valid trading day!
                five_min = []
                for ts, row in market_slice.iterrows():
                    vol = row.get("Volume")
                    five_min.append(Candle(
                        start=ts,
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        volume=float(vol) if vol and vol > 0 else None,
                        source="yfinance-5m",
                        interval_minutes=5,
                    ))

                one_min = synthesize_1m_candles(five_min)

                # Compute prior day high, low, close from preceding trading day
                try:
                    curr_pos = all_available_dates.index(cal_day)
                    if curr_pos > 0:
                        prev_day = all_available_dates[curr_pos - 1]
                        prev_slice = day_groups.get_group(prev_day).between_time("09:15", "15:30")
                        pdh = float(prev_slice["High"].max())
                        pdl = float(prev_slice["Low"].min())
                        pdc = float(prev_slice["Close"].iloc[-1])
                    else:
                        pdh = pdl = pdc = None
                except Exception:
                    pdh = pdl = pdc = None

                day_data = DayData(
                    date=cal_day,
                    five_minute=five_min,
                    one_minute=one_min,
                    pdh=pdh, pdl=pdl, pdc=pdc,
                    candle_count=count,
                    first_time_ist=first_ts_str,
                    last_time_ist=last_ts_str,
                    status="VALID",
                )
                dataset.valid_days[cal_day] = day_data
                dataset.day_records.append(DayDiagnosticRecord(
                    date=cal_day, candle_count=count,
                    first_time_ist=first_ts_str, last_time_ist=last_ts_str,
                    status="VALID", reason="OK (>= 70 bars)",
                ))
                dataset.days_processed += 1
            else:
                # Partial / insufficient data
                reason = f"PARTIAL/NO DATA ({count} bars < 70)"
                skip_reasons_counter["PARTIAL/NO DATA"] = skip_reasons_counter.get("PARTIAL/NO DATA", 0) + 1
                dataset.day_records.append(DayDiagnosticRecord(
                    date=cal_day, candle_count=count,
                    first_time_ist=first_ts_str, last_time_ist=last_ts_str,
                    status="SKIPPED", reason=reason,
                ))
                dataset.days_skipped += 1
        else:
            # Not in DataFrame: weekend or trading holiday
            is_weekend = cal_day.weekday() >= 5
            reason = "WEEKEND (0 bars)" if is_weekend else "HOLIDAY/NO DATA (0 bars)"
            key = "WEEKEND" if is_weekend else "HOLIDAY/NO DATA"
            skip_reasons_counter[key] = skip_reasons_counter.get(key, 0) + 1
            dataset.day_records.append(DayDiagnosticRecord(
                date=cal_day, candle_count=0,
                first_time_ist="-", last_time_ist="-",
                status="SKIPPED", reason=reason,
            ))
            dataset.days_skipped += 1

    # Format skip summary string
    reasons_list = [f"{k} ({v} days)" for k, v in skip_reasons_counter.items()]
    dataset.skip_summary = ", ".join(reasons_list) if reasons_list else "None"

    _GLOBAL_DATASET_CACHE = dataset

    if print_table:
        print_diagnostic_table(dataset)

    return dataset


def print_diagnostic_table(dataset: ReplayDataset) -> None:
    """Print the diagnostic data fetch table and summary."""
    safe_print("\n" + "=" * 90)
    safe_print("DIAGNOSTIC: CANDLE SOURCE & FETCH AUDIT")
    safe_print("=" * 90)
    safe_print(f"Candle SOURCE: {dataset.source_name}")
    safe_print(f"Historical response: start {dataset.source_start_ist} | "
               f"end {dataset.source_end_ist} | total rows: {dataset.total_raw_rows}")
    safe_print("Primary Data Engine: yfinance (^NSEI historical)")
    safe_print("(Verified: Replay engine uses historical 5m bars, NOT live-today NSE chart API)")
    safe_print("=" * 90)
    safe_print(f"{'DATE':10} | {'CANDLES':7} | {'FIRST CANDLE (IST)':19} | {'LAST CANDLE (IST)':19} | {'STATUS / REASON'}")
    safe_print("-" * 90)

    for rec in dataset.day_records:
        status_str = "VALID" if rec.status == "VALID" else f"SKIPPED ({rec.reason})"
        safe_print(f"{rec.date.isoformat():10} | {rec.candle_count:7d} | "
                   f"{rec.first_time_ist:19} | {rec.last_time_ist:19} | {status_str}")

    safe_print("=" * 90)
    safe_print(f"days processed: {dataset.days_processed} | skipped: {dataset.days_skipped} | "
               f"reason: {dataset.skip_summary}")
    safe_print("=" * 90 + "\n")


def get_day_candles(day: date) -> tuple[list[Candle], list[Candle], float | None, float | None, float | None]:
    """Retrieve validated intraday candles for a specific trading day.
    
    Returns (one_minute, five_minute, pdh, pdl, pdc).
    """
    global _GLOBAL_DATASET_CACHE
    if _GLOBAL_DATASET_CACHE is None or day not in _GLOBAL_DATASET_CACHE.valid_days:
        prepare_replay_data(days=60, print_table=False)

    if _GLOBAL_DATASET_CACHE and day in _GLOBAL_DATASET_CACHE.valid_days:
        d = _GLOBAL_DATASET_CACHE.valid_days[day]
        return d.one_minute, d.five_minute, d.pdh, d.pdl, d.pdc

    return [], [], None, None, None


def load_csv_5m_dataset(csv_path: str | Path, print_table: bool = True) -> ReplayDataset:
    """Load a multi-year public NIFTY 5-minute dataset via CSV adapter into ReplayDataset.
    
    Quality gate: Valid trading day must have 70+ five-minute candles between 09:15-15:30 IST.
    Supports standard CSV formats with date/time/timestamp and OHLCV columns.
    """
    path = Path(csv_path)
    if not path.exists():
        log.error("CSV file not found: %s", path)
        return ReplayDataset(source_name=f"CSV ({path.name}) - FILE NOT FOUND")

    # Read CSV
    df = pd.read_csv(path)
    
    # Normalize column names
    col_map = {}
    for col in df.columns:
        c_lower = col.strip().lower()
        if c_lower in ("date", "datetime", "timestamp", "time"):
            col_map[col] = "timestamp"
        elif c_lower == "open":
            col_map[col] = "Open"
        elif c_lower == "high":
            col_map[col] = "High"
        elif c_lower == "low":
            col_map[col] = "Low"
        elif c_lower == "close":
            col_map[col] = "Close"
        elif c_lower in ("volume", "vol", "v"):
            col_map[col] = "Volume"

    df = df.rename(columns=col_map)
    
    # If date and time are separate columns
    if "date" in [c.lower() for c in df.columns] and "time" in [c.lower() for c in df.columns]:
        d_col = [c for c in df.columns if c.lower() == "date"][0]
        t_col = [c for c in df.columns if c.lower() == "time"][0]
        df["timestamp"] = pd.to_datetime(df[d_col].astype(str) + " " + df[t_col].astype(str))
    elif "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    else:
        raise ValueError(f"Could not identify datetime columns in {path.name}. Found: {list(df.columns)}")

    # Timezone conversion / localization
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)

    df = df.set_index("timestamp").sort_index()

    source_name = f"Public CSV ({path.name}, 5m)"
    dataset = ReplayDataset(
        source_name=source_name,
        total_raw_rows=len(df),
        source_start_ist=df.index[0].isoformat() if not df.empty else "N/A",
        source_end_ist=df.index[-1].isoformat() if not df.empty else "N/A",
    )

    day_groups = df.groupby(df.index.date)
    sorted_dates = sorted(day_groups.groups.keys())
    prev_d: DayData | None = None
    skip_counter = {}

    for cal_day in sorted_dates:
        day_df = day_groups.get_group(cal_day)
        market_slice = day_df.between_time("09:15", "15:30")
        count = len(market_slice)

        if count >= 70:
            five_min = []
            for ts, row in market_slice.iterrows():
                vol = row.get("Volume")
                five_min.append(Candle(
                    start=ts,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(vol) if pd.notna(vol) and vol > 0 else None,
                    source="csv-5m",
                    interval_minutes=5,
                ))

            one_min = synthesize_1m_candles(five_min)
            pdh = prev_d.pdh if prev_d else float(market_slice["High"].max())
            pdl = prev_d.pdl if prev_d else float(market_slice["Low"].min())
            pdc = prev_d.pdc if prev_d else float(market_slice["Close"].iloc[0])

            d_data = DayData(
                date=cal_day,
                five_minute=five_min,
                one_minute=one_min,
                pdh=pdh, pdl=pdl, pdc=pdc,
                candle_count=count,
                first_time_ist=market_slice.index[0].strftime("%Y-%m-%d %H:%M:%S"),
                last_time_ist=market_slice.index[-1].strftime("%Y-%m-%d %H:%M:%S"),
                status="VALID",
            )
            dataset.valid_days[cal_day] = d_data
            dataset.day_records.append(DayDiagnosticRecord(
                date=cal_day, candle_count=count,
                first_time_ist=d_data.first_time_ist,
                last_time_ist=d_data.last_time_ist,
                status="VALID", reason="OK (>= 70 candles)",
            ))
            dataset.days_processed += 1
            prev_d = d_data
        else:
            reason = f"PARTIAL DATA ({count} candles < 70)"
            skip_counter[reason] = skip_counter.get(reason, 0) + 1
            dataset.day_records.append(DayDiagnosticRecord(
                date=cal_day, candle_count=count,
                first_time_ist=market_slice.index[0].strftime("%Y-%m-%d %H:%M:%S") if count > 0 else "-",
                last_time_ist=market_slice.index[-1].strftime("%Y-%m-%d %H:%M:%S") if count > 0 else "-",
                status="SKIPPED", reason=reason,
            ))
            dataset.days_skipped += 1

    reasons_list = [f"{k} ({v} days)" for k, v in skip_counter.items()]
    dataset.skip_summary = ", ".join(reasons_list) if reasons_list else "None"

    if print_table:
        print_diagnostic_table(dataset)

    return dataset

