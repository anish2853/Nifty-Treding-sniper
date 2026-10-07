"""MarketDataStore - the SINGLE SOURCE OF TRUTH for market data (hotfix).

One refresh() per 60 s cycle fetches ONCE: 5-min candles (chart API), the option
chain (nearest + next expiry), and India VIX - then validates and stores values
+ timestamps + source. PDC/PDH/PDL come from the pre-market yfinance fetch,
stored ONCE and reused all day (3-retry rule at startup; if still unavailable the
caller declares NO-TRADE DAY - a silent bot beats a wrong bot).

EVERY consumer - regime, liquidity map, entry, exit, sanity gates, trade cards,
EOD - reads ONLY from this store. Chain validation on every fetch: symbol must be
NIFTY, every strike a multiple of 50, and underlyingValue within +/-3% of the
stored PDC. The liquidity map only sees strikes within +/-2% of the stored spot.
"""
import logging
from dataclasses import dataclass
from datetime import datetime

import settings
from data_sources.nse_chain import (fetch_option_chain_with_retries,
                                    parse_option_chain, select_trade_expiry)
from data_sources.nse_quote import india_vix
from engines.candles import CandleEngine
from watchdog import FeedHealth
from utils import ist_now

log = logging.getLogger(__name__)


@dataclass
class StoredValue:
    value: object
    asof: str                    # ISO IST timestamp of the fetch
    source: str


class MarketDataStore:
    def __init__(self, session=None, journal=None, holidays=None):
        self._session = session
        self._journal = journal              # single WRITE path for snapshots
        self.holidays = holidays or set()
        self.candle_engine = CandleEngine(session=session, store=journal)
        self.candles: list = []              # today's closed candles (engine-owned)
        self.spot: StoredValue | None = None
        self.chain = None                    # parsed ChainSnapshot | None
        self.chain_source = "not fetched"
        self.chain_tried: list = []
        self.vix: StoredValue | None = None  # value: {"value", "prev_close"}
        self.prev_day: StoredValue | None = None  # value: {"pdc", "pdh", "pdl"}
        self.liquidity = None
        self.trade_expiry = None
        self.trade_expiry_days = None
        self.last_refresh: str | None = None
        self.health = FeedHealth(settings.FEED_FAILURE_ALERT_THRESHOLD)

    @property
    def journal(self):
        """The SQLite journal this store persists snapshots into (read-only uses:
        engine OI-window queries)."""
        return self._journal

    # -- previous-day levels: set once (pre-market), reused all day -------------
    def set_prev_day(self, pdh, pdl, pdc, source="premarket-yfinance") -> None:
        if pdc is None:
            return
        self.prev_day = StoredValue({"pdc": pdc, "pdh": pdh, "pdl": pdl},
                                    ist_now().isoformat(timespec="seconds"),
                                    source)
        log.info("store: prev-day levels set (source=%s): PDC=%s PDH=%s PDL=%s",
                 source, pdc, pdh, pdl)

    def ensure_prev_day(self, retries: int = 3) -> bool:
        """Startup guarantee (spec #5): up to `retries` attempts to fetch the
        previous-day levels; False -> caller declares NO-TRADE DAY (silent)."""
        if self.prev_day is not None:
            return True
        from data_sources.global_factors import fetch_global_factors
        for attempt in range(1, retries + 1):
            try:
                factors = fetch_global_factors()
                if factors.nifty_pd.status == "OK":
                    extra = factors.nifty_pd.extra
                    self.set_prev_day(extra.get("pdh"), extra.get("pdl"),
                                      extra.get("pdc"))
                    return True
                log.warning("prev-day fetch attempt %d/%d failed: %s",
                            attempt, retries, factors.nifty_pd.error)
            except Exception as exc:
                log.warning("prev-day fetch attempt %d/%d errored: %s",
                            attempt, retries, exc)
        return False

    @property
    def pdc(self):
        return self.prev_day.value.get("pdc") if self.prev_day else None

    @property
    def pdh(self):
        return self.prev_day.value.get("pdh") if self.prev_day else None

    @property
    def pdl(self):
        return self.prev_day.value.get("pdl") if self.prev_day else None

    # -- chain validation (spec #4) ----------------------------------------------
    def _validate_chain_raw(self, raw) -> tuple:
        records = raw.get("records") if isinstance(raw, dict) else None
        if not isinstance(records, dict):
            return False, "payload missing records"
        data = records.get("data") or []
        for entry in data[:50]:
            for side in ("CE", "PE"):
                block = entry.get(side) or {}
                underlying = block.get("underlying")
                if underlying and underlying != settings.NIFTY_SYMBOL:
                    return False, (f"symbol mismatch: {underlying} "
                                   f"(expected {settings.NIFTY_SYMBOL})")
        strikes = [e.get("strikePrice") for e in data
                   if isinstance(e, dict) and e.get("strikePrice") is not None]
        if strikes and any(int(s) % settings.STRIKE_ROUND_TO != 0 for s in strikes):
            return False, "strike grid is not a multiple of 50"
        value = records.get("underlyingValue")
        if value is None:
            return False, "payload missing underlyingValue"
        if self.pdc:
            deviation = abs(value / self.pdc - 1)
            if deviation > settings.SPOT_PDC_MAX_DEVIATION:
                return False, (f"underlyingValue {value} deviates "
                               f"{deviation:+.2%} from stored PDC {self.pdc}")
        return True, ""

    # -- the ONE fetch pass (spec #1) ----------------------------------------------
    def refresh(self) -> dict:
        """Candles + chain (nearest + next expiry) + India VIX, fetched once,
        validated, stored with timestamps + source. Returns a status dict with
        the freshly closed candles for the entry engine."""
        now = ist_now()
        iso = now.isoformat(timespec="seconds")
        status = {"fresh_candles": [], "fresh_minutes": [],
              "chain_ok": False, "vix_ok": False}

        # 1) candles (chart API, sample fallback inside the engine)
        fresh = []
        try:
            fresh = self.candle_engine.refresh(now=now)
        except Exception as exc:
            log.warning("candle refresh failed: %s", exc)
        self.candles = self.candle_engine.candles
        status["fresh_candles"] = fresh
        status["fresh_minutes"] = self.candle_engine.fresh_minutes
        status["candles"] = len(self.candles)
        self.health.record("nse_chart", self.candle_engine.chart_ok
                           or bool(self.candles))

        # 2) option chain - validated BEFORE anything consumes it
        raw, _http, tried = (fetch_option_chain_with_retries(self._session)
                             if self._session else (None, None, []))
        self.chain_tried = tried
        snapshot = None
        if raw is not None:
            ok, reason = self._validate_chain_raw(raw)
            if ok:
                from data_sources.nse_chain import parse_option_chain
                try:
                    snapshot = parse_option_chain(raw)
                except ValueError as exc:
                    log.warning("chain parse failed: %s", exc)
            else:
                log.warning("chain REJECTED by validation: %s", reason)
        self.health.record("option_chain", snapshot is not None)
        if snapshot is not None:
            self.chain = snapshot
            self.chain_source = "live NSE v3 flow"
            self.spot = StoredValue(snapshot.underlying, iso, "chain-underlying")
            from data_sources.nse_chain import select_trade_expiry
            self.trade_expiry, self.trade_expiry_days = select_trade_expiry(
                snapshot.expiry_dates, now.date(), self.holidays)
            status["chain_ok"] = True
        status["chain_rejected"] = raw is not None and snapshot is None

        # 3) India VIX
        vix_info = india_vix(self._session) if self._session else None
        vix_ok = bool(vix_info and vix_info.get("value") is not None)
        self.health.record("india_vix", vix_ok)
        if vix_ok:
            self.vix = StoredValue({"value": vix_info.get("value"),
                                    "prev_close": vix_info.get("prev_close")},
                                   iso, "nse-quote-api")
        status["vix_ok"] = vix_ok

        # 4) spot fallback from candles when the chain is unavailable
        if self.spot is None and self.candles:
            self.spot = StoredValue(self.candles[-1].close, iso, "chart-close")

        # 5) derived liquidity map: only strikes within +/-2% of stored spot
        self.liquidity = None
        if self.chain and self.spot and self.chain.rows:
            from engines.liquidity import compute_liquidity_map
            band = settings.LIQUIDITY_STRIKE_BAND_PCT
            near = [r for r in self.chain.rows
                    if r.strike is not None and self.spot.value is not None
                    and abs(r.strike - self.spot.value) <= self.spot.value * band]
            self.liquidity = compute_liquidity_map(
                near, self.spot.value, self.chain.nearest_expiry,
                pdh=self.pdh, pdl=self.pdl, ts=snapshot.raw_ts if snapshot else None,
                pcr_total=self.chain.pcr_total)

        self.last_refresh = iso
        return status

    # -- consumer reads -------------------------------------------------------------
    def spot_value(self):
        return self.spot.value if self.spot else None

    def data_footer(self) -> str:
        """Spec #6: every module's output shows the data it used."""
        spot = f"{self.spot.value:,.2f}" if self.spot and self.spot.value is not None \
            else "MISSING"
        pdc = f"{self.pdc:,.2f}" if self.pdc else "MISSING"
        ts = (self.last_refresh or ist_now().isoformat(timespec="seconds"))[11:19]
        return f"spot {spot} | PDC {pdc} | store {ts} IST"
