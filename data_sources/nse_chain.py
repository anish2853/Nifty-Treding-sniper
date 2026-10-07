"""NSE Nifty option chain: fetch + parse.

Fetch (Prompt-4 diagnosis: NSE retired /api/option-chain-indices - it 404s):
  1. handshake on /option-chain (session module),
  2. GET /api/option-chain-contract-info?symbol=NIFTY -> expiryDates,
  3. GET /api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=<e> per expiry
     (nearest NSE_CHAIN_MAX_EXPIRIES merged into one payload),
  4. legacy static endpoints and an .env NSE_CHAIN_ENDPOINT override as fallbacks.

parse_option_chain is strict (raises ValueError on structurally broken payloads) so
unit tests can pin exact behaviour; the fetch layer is graceful (None + diagnostics).

PCR note: "total PCR (sum put OI / sum call OI)" is computed over the merged rows
(all expiries fetched this cycle - with the v3 API that is the nearest
NSE_CHAIN_MAX_EXPIRIES expiries, not all 18 listed). Nearest-expiry PCR and max-OI
strikes on both scopes are stored alongside.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import quote

import settings
from data_sources.nse_session import NSESession

log = logging.getLogger(__name__)

EXPIRY_FMT = "%d-%b-%Y"
CHAIN_TS_FMT = "%d-%b-%Y %H:%M:%S"

_V3_INFO_PATH = "/api/option-chain-contract-info?symbol="
_V3_CHAIN_PATH = "/api/option-chain-v3?type=Indices&symbol="

# Runtime-sticky fetch method: 'env' | 'v3' | 'legacy'. Found once, reused all day.
_working_mode = None


@dataclass
class StrikeRow:
    expiry: str
    expiry_date: date | None
    strike: int
    ce_oi: float | None = None
    ce_change_oi: float | None = None
    ce_ltp: float | None = None
    ce_iv: float | None = None
    ce_volume: float | None = None
    pe_oi: float | None = None
    pe_change_oi: float | None = None
    pe_ltp: float | None = None
    pe_iv: float | None = None
    pe_volume: float | None = None


@dataclass
class ChainSnapshot:
    underlying: float | None
    chain_ts: datetime | None
    raw_ts: str | None
    expiry_dates: list
    rows: list
    nearest_expiry: str | None
    total_call_oi: float
    total_put_oi: float
    pcr_total: float | None
    pcr_nearest: float | None
    max_call_oi_strike: int | None
    max_put_oi_strike: int | None
    max_call_oi_strike_nearest: int | None
    max_put_oi_strike_nearest: int | None


# --- Fetch flow (Prompt-4: ordered methods, sticky, per-expiry merge) ------------

def endpoint_label(url: str) -> str:
    """Compact URL for logs/alarm text: '/api/option-chain-v3?type=...&expiry=...'."""
    return url.split("nseindia.com", 1)[-1] if "nseindia.com" in url else url


def _looks_like_chain(raw) -> bool:
    records = raw.get("records") if isinstance(raw, dict) else None
    return isinstance(records, dict) and isinstance(records.get("data"), list)


def _contract_info_url() -> str:
    return settings.NSE_BASE_URL + _V3_INFO_PATH + quote(settings.NIFTY_SYMBOL)


def _v3_url(expiry: str) -> str:
    return (settings.NSE_BASE_URL + _V3_CHAIN_PATH + quote(settings.NIFTY_SYMBOL)
            + "&expiry=" + quote(expiry))


def _merge_parts(parts, expiries):
    """Merge per-expiry v3 payloads into one old-style payload (first part's
    underlying/timestamp/filtered, all rows combined, full expiry list)."""
    merged = parts[0]
    merged["records"]["data"] = [row for part in parts
                                 for row in (part["records"].get("data") or [])]
    merged["records"]["expiryDates"] = expiries
    return merged


def _fetch_expiries(session: NSESession, expiries: list, template_builder, tried: list):
    """Shared loop for the v3 + env methods: one URL per nearest expiry, collect
    every chain-shaped payload, merge into one payload. template_builder(expiry) -> URL."""
    parts = []
    for expiry in expiries[:settings.NSE_CHAIN_MAX_EXPIRIES]:
        url = template_builder(expiry)
        part = session.get_json_once(url)
        tried.append((endpoint_label(url), session.last_status))
        if _looks_like_chain(part):
            parts.append(part)
    if not parts:
        return None
    return _merge_parts(parts, expiries)


def _fetch_v3(session: NSESession):
    """Working NSE contract: contract-info -> one v3 call per nearest expiry,
    merged into one payload. Returns (raw | None, tried [(label, status)])."""
    tried = []
    info = session.get_json_once(_contract_info_url())
    tried.append((endpoint_label(_contract_info_url()), session.last_status))
    expiries = info.get("expiryDates") if isinstance(info, dict) else None
    if not expiries:
        return None, tried
    raw = _fetch_expiries(session, expiries, _v3_url, tried)
    return raw, tried


def _templated_expiry(template: str) -> str:
    """Replace the concrete expiry value in a pasted URL with an {expiry} slot."""
    import re
    return re.sub(r"expiry=[^&]+", "expiry={expiry}", template)


def _fetch_env(session: NSESession):
    """.env NSE_CHAIN_ENDPOINT override, tried first so a pasted-from-Chrome URL
    swaps in seconds. expiry=... values are auto-templated (or write {expiry})."""
    template = settings.NSE_CHAIN_ENDPOINT
    tried = []
    if "{expiry}" in template or "expiry=" in template:
        pattern = template if "{expiry}" in template else _templated_expiry(template)
        info = session.get_json_once(_contract_info_url())
        tried.append((endpoint_label(_contract_info_url()), session.last_status))
        expiries = info.get("expiryDates") if isinstance(info, dict) else None
        if not expiries:
            return None, tried
        raw = _fetch_expiries(
            session, expiries, lambda expiry: pattern.format(expiry=quote(expiry)),
            tried)
        return raw, tried
    raw = session.get_json_once(template)
    tried.append((endpoint_label(template), session.last_status))
    return (raw if _looks_like_chain(raw) else None), tried


def _fetch_legacy(session: NSESession):
    """Last resort: the old static endpoints, in case NSE restores them."""
    tried = []
    for url in settings.NSE_CHAIN_LEGACY_ENDPOINTS:
        raw = session.get_json_once(url)
        tried.append((endpoint_label(url), session.last_status))
        if _looks_like_chain(raw):
            return raw, tried
    return None, tried


def _fetch_chain_once(session: NSESession):
    """One fetch attempt over the best-known method (sticky), falling back through
    env override -> v3 flow -> legacy endpoints. Returns (raw | None, tried)."""
    global _working_mode
    methods = (_working_mode,) if _working_mode else ("env", "v3", "legacy")
    tried = []
    for method in methods:
        if method == "env" and not settings.NSE_CHAIN_ENDPOINT:
            continue
        raw, method_tried = {"env": _fetch_env, "v3": _fetch_v3,
                             "legacy": _fetch_legacy}[method](session)
        tried.extend(method_tried)
        if raw is not None:
            if _working_mode != method:
                log.info("Chain fetch method locked in: %s", method)
            _working_mode = method
            return raw, tried
    return None, tried


def fetch_option_chain_with_retries(session: NSESession, retries: int | None = None,
                                    gap_sec: int | None = None):
    """Robust chain fetch used by BOTH the 08:45 pre-market read and the market-hours
    poll (Prompt-4 #2/#4/#6): handshake first, then up to `retries` cycles
    `gap_sec` apart, cookies refreshed between cycles. Returns (raw_json | None,
    last_http_status, tried as [(label, status)] from the final attempt)."""
    import time
    retries = settings.NSE_CHAIN_RETRIES if retries is None else retries
    gap_sec = settings.NSE_CHAIN_RETRY_GAP_SEC if gap_sec is None else gap_sec

    session.warm_up()  # handshake before ANY api call
    last_status = None
    tried = []
    for cycle in range(1, retries + 1):
        raw, tried = _fetch_chain_once(session)
        last_status = session.last_status
        if raw is not None:
            return raw, session.last_status, tried
        if cycle < retries:
            session.warm_up()
            time.sleep(gap_sec)
    return None, last_status, tried


def select_trade_expiry(expiry_dates, today, holiday_dates=None,
                        min_trading_days: int | None = None):
    """Frozen rule (Phase B+ hotfix #4): trade the CURRENT weekly only if >= 2
    trading days remain (today counts); otherwise roll to the NEXT expiry.
    Trading days = weekdays minus the configured holiday list.
    Returns (expiry_label | None, trading_days_remaining)."""
    from datetime import timedelta
    holiday_dates = holiday_dates or set()
    min_days = settings.TRADE_EXPIRY_MIN_TRADING_DAYS if min_trading_days is None \
        else min_trading_days

    def trading_days(label: str) -> int:
        expiry = _parse_expiry(label)
        if expiry is None:
            return 0
        count, cursor = 0, today
        while cursor <= expiry:
            if cursor.weekday() < 5 and cursor.isoformat() not in holiday_dates:
                count += 1
            cursor += timedelta(days=1)
        return count

    ordered = sorted((e for e in (expiry_dates or []) if _parse_expiry(e) is not None),
                     key=_parse_expiry)
    for label in ordered:
        days = trading_days(label)
        if days >= min_days:
            return label, days
    return (ordered[-1], trading_days(ordered[-1])) if ordered else (None, 0)


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _parse_expiry(text):
    try:
        return datetime.strptime(text, EXPIRY_FMT).date()
    except (TypeError, ValueError):
        return None


def parse_option_chain(raw: dict, today: date | None = None) -> ChainSnapshot:
    if not isinstance(raw, dict):
        raise ValueError("chain payload is not a dict")
    records = raw.get("records")
    if not isinstance(records, dict):
        raise ValueError("chain payload missing 'records'")
    data = records.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("chain payload missing 'records.data'")

    today = today or datetime.now(tz=settings.IST).date()

    rows: list[StrikeRow] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        strike = entry.get("strikePrice")
        # Expiry identity: legacy rows carry 'expiryDate'; option-chain-v3 rows carry
        # a string in 'expiryDates' (the CE/PE blocks use a DD-MM-YYYY variant, so
        # they are only a last-resort label).
        expiry = entry.get("expiryDate")
        if expiry is None and isinstance(entry.get("expiryDates"), str):
            expiry = entry.get("expiryDates")
        ce, pe = entry.get("CE") or {}, entry.get("PE") or {}
        if expiry is None:
            expiry = ce.get("expiryDate") or pe.get("expiryDate")
        if strike is None or expiry is None:
            continue
        rows.append(StrikeRow(
            expiry=expiry, expiry_date=_parse_expiry(expiry), strike=int(strike),
            ce_oi=_num(ce.get("openInterest")),
            ce_change_oi=_num(ce.get("changeinOpenInterest")),
            ce_ltp=_num(ce.get("lastPrice")),
            ce_iv=_num(ce.get("impliedVolatility")),
            ce_volume=_num(ce.get("totalTradedVolume")),
            pe_oi=_num(pe.get("openInterest")),
            pe_change_oi=_num(pe.get("changeinOpenInterest")),
            pe_ltp=_num(pe.get("lastPrice")),
            pe_iv=_num(pe.get("impliedVolatility")),
            pe_volume=_num(pe.get("totalTradedVolume")),
        ))

    total_call = sum(r.ce_oi for r in rows if r.ce_oi is not None)
    total_put = sum(r.pe_oi for r in rows if r.pe_oi is not None)
    pcr_total = (total_put / total_call) if total_call > 0 else None

    # Nearest expiry = soonest expiry on/after today (earliest overall as fallback).
    expiries = {_parse_expiry(e) for e in records.get("expiryDates", [])}
    expiries.discard(None)
    upcoming = sorted(e for e in expiries if e >= today) or sorted(expiries)
    nearest_date = upcoming[0] if upcoming else None
    nearest_expiry = nearest_date.strftime(EXPIRY_FMT) if nearest_date else None

    nearest_rows = [r for r in rows if r.expiry == nearest_expiry]
    call_near = sum(r.ce_oi for r in nearest_rows if r.ce_oi is not None)
    put_near = sum(r.pe_oi for r in nearest_rows if r.pe_oi is not None)
    pcr_nearest = (put_near / call_near) if call_near > 0 else None

    def _max_strike(row_list, attr):
        candidates = [r for r in row_list if getattr(r, attr) is not None]
        return max(candidates, key=lambda r: getattr(r, attr)).strike if candidates else None

    raw_ts = records.get("timestamp")
    chain_ts = None
    if raw_ts:
        try:
            chain_ts = datetime.strptime(raw_ts, CHAIN_TS_FMT).replace(tzinfo=settings.IST)
        except ValueError:
            log.warning("Could not parse chain timestamp %r", raw_ts)

    return ChainSnapshot(
        underlying=_num(records.get("underlyingValue")),
        chain_ts=chain_ts,
        raw_ts=raw_ts,
        expiry_dates=list(records.get("expiryDates", [])),
        rows=rows,
        nearest_expiry=nearest_expiry,
        total_call_oi=total_call,
        total_put_oi=total_put,
        pcr_total=pcr_total,
        pcr_nearest=pcr_nearest,
        max_call_oi_strike=_max_strike(rows, "ce_oi"),
        max_put_oi_strike=_max_strike(rows, "pe_oi"),
        max_call_oi_strike_nearest=_max_strike(nearest_rows, "ce_oi"),
        max_put_oi_strike_nearest=_max_strike(nearest_rows, "pe_oi"),
    )
