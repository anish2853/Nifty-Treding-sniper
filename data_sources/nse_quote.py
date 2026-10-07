"""Best-effort NSE quote endpoints (sources 3-4 of the spec): India VIX quote-API
fallback, FII/DII activity, GIFT Nifty. Every function returns a status dict and
never raises; failures become status=MISSING with the reason attached."""
import json
import logging
import re
from datetime import datetime
from html.parser import HTMLParser

import settings
from data_sources.nse_session import NSESession
from utils import MISSING

log = logging.getLogger(__name__)

_AMOUNT_RE = re.compile(r"-?\d+(?:\.\d+)?")
_FIIDII_DATE_FMT = "%d-%b-%Y"


def _result(value=None, status="OK", error=None, **extra):
    out = {"value": value, "status": status, "error": error}
    out.update(extra)
    return out


class _NextDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._capturing = False
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self._capturing = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._capturing = False

    def handle_data(self, data):
        if self._capturing:
            self._parts.append(data)


class _SpotValueParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.value = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("id") == "spotValue":
            self.value = attributes.get("value")


def _next_data_payload(text):
    parser = _NextDataParser()
    parser.feed(text)
    if not parser._parts:
        return None
    try:
        return json.loads("".join(parser._parts))
    except ValueError:
        return None


def _moneycontrol_spot_value(text):
    parser = _SpotValueParser()
    parser.feed(text)
    return _index_price(parser.value)


def india_vix(session: NSESession) -> dict:
    """India VIX spot via Moneycontrol, with NSE allIndices context/fallback.
    Returns {value, prev_close, pct_change, status, error}."""
    text = session.get_text(settings.INDIA_VIX_URL)
    value = _moneycontrol_spot_value(text) if text else None
    nse_quote = _india_vix_from_all_indices(session)
    if value is not None:
        return _result(value=value,
                       prev_close=nse_quote.get("prev_close"),
                       pct_change=nse_quote.get("pct_change"),
                       source=settings.INDIA_VIX_URL)
    return nse_quote


def _india_vix_from_all_indices(session: NSESession) -> dict:
    raw = session.get_json(settings.NSE_BASE_URL + "/api/allIndices")
    if not raw or not isinstance(raw, dict):
        return _result(status=MISSING, error="no response from allIndices API")
    try:
        rows = raw.get("data") or []
        row = next((item for item in rows if isinstance(item, dict)
                    and str(item.get("index", "")).strip().upper() == "INDIA VIX"), {})
        value = row.get("last")
        if value is None:
            return _result(status=MISSING, error="allIndices response lacked India VIX 'last'")
        prev_close = row.get("previousClose")
        return _result(value=float(value),
                       prev_close=float(prev_close) if prev_close is not None else None,
                       pct_change=_num(row.get("pChange")))
    except (AttributeError, TypeError, ValueError) as exc:
        return _result(status=MISSING, error=f"allIndices parse failed: {exc}")


def fii_dii(session: NSESession) -> dict:
    """FII/DII net activity from fiidiiTradeReact.

    Payload (verified live 2026-10-05) is a LIST of dicts:
      [{'buyValue': '25420.04', 'category': 'DII', 'date': '01-Oct-2026',
        'netValue': '10041.84', 'sellValue': '15378.2'}, ...]
    The older {'category', 'date', 'value'} shape is still accepted. Returns the
    LATEST date's FII/FPI net and DII net (Rs crore) plus that data date, so the
    report always states which session the figures belong to.
    Returns {fii_net_cr, dii_net_cr, asof, status, error}."""
    url = settings.NSE_BASE_URL + "/api/fiidiiTradeReact"
    raw = session.get_json(url)
    if not isinstance(raw, list):
        return _result(status=MISSING, error="no response from fiidiiTradeReact")
    parsed = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        category = str(row.get("category", "")).upper()
        net_txt = row.get("netValue")
        if net_txt is None:
            net_txt = row.get("value")  # older payload shape
        if net_txt is None or not row.get("date"):
            continue
        match = _AMOUNT_RE.search(str(net_txt).replace(",", ""))
        if not match:
            continue
        try:
            date_key = datetime.strptime(row["date"], _FIIDII_DATE_FMT)
        except ValueError:
            continue  # unparseable date -> can't order sessions, skip
        parsed.append((date_key, {"category": category,
                                  "date": row["date"],
                                  "net": float(match.group())}))
    if not parsed:
        return _result(status=MISSING,
                       error=f"no FII/DII rows recognised: {str(raw)[:200]}")
    latest_key = max(date_key for date_key, _ in parsed)
    latest = [entry for date_key, entry in parsed if date_key == latest_key]
    fii = next((e["net"] for e in latest
                if "FII" in e["category"] or "FPI" in e["category"]), None)
    dii = next((e["net"] for e in latest if "DII" in e["category"]), None)
    if fii is None and dii is None:
        return _result(status=MISSING, error=f"no FII/DII nets for {latest[0]['date']}")
    return _result(fii_net_cr=fii, dii_net_cr=dii, asof=latest[0]["date"])


# --- GIFT Nifty (best effort; no documented public JSON API on nseix.com) -------
_NUM_RE = re.compile(r"\d{2,6}(?:\.\d+)?")
_NAME_KEYS = ("name", "symbol", "index", "instrument", "description", "stkexchg")
_PRICE_KEYS = ("last", "lastprice", "ltp", "price", "close", "value",
               "last_traded_price", "lasttradedprice")


def gift_nifty(session: NSESession) -> dict:
    """Best-effort GIFT Nifty level from settings.GIFT_NIFTY_SOURCES (tried in order):
    JSON payloads are deep-searched for a GIFT NIFTY entry, HTML is regex-scraped.
    MISSING on total failure - never blocks the pipeline (spec Module 1)."""
    for url in settings.GIFT_NIFTY_SOURCES:
        text = session.get_text(url)
        if not text:
            continue
        if text.lstrip()[:1] in "[{":
            try:
                found = _deep_find_gift_price(json.loads(text))
                if found is not None:
                    return _result(value=found, source=url)
            except ValueError:
                pass
        else:
            payload = _next_data_payload(text)
            if payload is not None:
                found = _deep_find_gift_price(payload)
                if found is not None:
                    return _result(value=found, source=url)
        scraped = _scrape_gift_from_html(text)
        if scraped is not None:
            return _result(value=scraped, source=url)
    return _result(status=MISSING,
                   error="all GIFT NIFTY sources failed; add a working URL to "
                         "settings.GIFT_NIFTY_SOURCES or leave as MISSING")


def _deep_find_gift_price(node, depth: int = 0):
    """Recursively look for a dict whose name-ish field mentions GIFT NIFTY and that
    carries one of the common price keys."""
    if depth > 8:
        return None
    if isinstance(node, dict):
        name_field = " ".join(str(v) for k, v in node.items()
                              if k.lower() in _NAME_KEYS and isinstance(v, str))
        haystack = name_field or " ".join(str(v) for v in node.values() if isinstance(v, str))
        if "gift" in haystack.lower() and "nifty" in haystack.lower():
            for key, value in node.items():
                if key.lower() in _PRICE_KEYS:
                    price = _index_price(value)
                    if price is not None:
                        return price
        for child in node.values():
            found = _deep_find_gift_price(child, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _deep_find_gift_price(child, depth + 1)
            if found is not None:
                return found
    return None


def _index_price(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _scrape_gift_from_html(text: str):
    """Regex scrape: 'GIFT NIFTY' followed (within a short window) by a plausible
    index-level number. Fragile by nature; MISSING is an acceptable outcome."""
    for match in re.finditer(r"GIFT\s*NIFTY(?:(?!GIFT\s*NIFTY).){0,600}",
                             text, re.IGNORECASE | re.DOTALL):
        for token in _NUM_RE.findall(match.group(0)):
            value = float(token)
            if 10_000 <= value <= 40_000:
                return value
    return None


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
