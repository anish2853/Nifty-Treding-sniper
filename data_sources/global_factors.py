"""Global / previous-day factors via yfinance (source 2 of the spec).

^GSPC, ^IXIC, CL=F, INR=X, ^NSEI (PDH/PDL/PDC), ^INDIAVIX (prev close, with the NSE
quote API as fallback handled by the caller). Every factor degrades to MISSING on
failure. yfinance is imported lazily so the parser/tests never need it installed.
"""
import logging
from dataclasses import dataclass, field

import settings
from utils import MISSING, ist_today

log = logging.getLogger(__name__)

SYMBOLS = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "crude": "CL=F",
    "usdinr": "INR=X",
    "nifty": "^NSEI",
    "india_vix": "^INDIAVIX",
}


@dataclass
class Factor:
    name: str
    value: float | None = None
    pct_change: float | None = None
    status: str = MISSING
    error: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class GlobalFactors:
    sp500: Factor
    nasdaq: Factor
    crude: Factor
    usdinr: Factor
    nifty_pd: Factor          # extra: pdh / pdl / pdc
    india_vix_prev: Factor    # previous session close

    def us_direction(self) -> int:
        """ASSUMPTION (flagged in README): 'US close direction' = sign of the S&P 500's
        last completed session % change. Nasdaq is still shown separately in the report."""
        if self.sp500.pct_change is None:
            return 0
        return 1 if self.sp500.pct_change > 0 else (-1 if self.sp500.pct_change < 0 else 0)


def _daily_frame(symbol):
    """Daily bars with any partial intraday row for 'today' (IST) removed, so 'last'
    is always the last COMPLETED session."""
    import yfinance as yf

    df = yf.Ticker(symbol).history(period="10d", interval="1d", auto_adjust=False)
    if df is None or df.empty:
        return None
    df = df.copy()
    today = ist_today()
    try:
        keep = [ts.tz_convert(settings.IST).date() != today for ts in df.index]
        df = df[keep]
    except (TypeError, AttributeError):
        pass  # naive/odd index: use bars as-is rather than fail the whole factor
    return df if not df.empty else None


def _close_factor(name: str, symbol: str) -> Factor:
    try:
        df = _daily_frame(symbol)
        if df is None or len(df) < 2 or "Close" not in df.columns:
            return Factor(name, error="insufficient daily bars from yfinance")
        last = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2])
        return Factor(name, value=last,
                      pct_change=(last / prev - 1) if prev else None, status="OK")
    except Exception as exc:  # yfinance raises many shapes; degrade to MISSING
        log.warning("yfinance %s failed: %s", symbol, exc)
        return Factor(name, error=str(exc))


def _nifty_pd_factor() -> Factor:
    try:
        df = _daily_frame(SYMBOLS["nifty"])
        if df is None or df.empty:
            return Factor("nifty_pd", error="no daily bars from yfinance")
        last = df.iloc[-1]
        return Factor("nifty_pd", value=float(last["Close"]), status="OK",
                      extra={"pdh": float(last["High"]),
                             "pdl": float(last["Low"]),
                             "pdc": float(last["Close"])})
    except Exception as exc:
        log.warning("yfinance %s (PDH/PDL/PDC) failed: %s", SYMBOLS["nifty"], exc)
        return Factor("nifty_pd", error=str(exc))


def fetch_global_factors() -> GlobalFactors:
    return GlobalFactors(
        sp500=_close_factor("sp500", SYMBOLS["sp500"]),
        nasdaq=_close_factor("nasdaq", SYMBOLS["nasdaq"]),
        crude=_close_factor("crude", SYMBOLS["crude"]),
        usdinr=_close_factor("usdinr", SYMBOLS["usdinr"]),
        nifty_pd=_nifty_pd_factor(),
        india_vix_prev=_close_factor("india_vix_prev", SYMBOLS["india_vix"]),
    )
