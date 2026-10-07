"""Tests for the NSE quote parsers and the chain fetch flow.

Covers: the new fiidiiTradeReact payload format, the option-chain-v3 fetch flow
(contract-info -> per-expiry v3 calls, merged), the sticky method cache, the .env
endpoint override, and the v3 row shape in the parser.
"""
import copy
import json
from datetime import date
from pathlib import Path

import pytest

import settings
from data_sources.nse_chain import (fetch_option_chain_with_retries, parse_option_chain,
                                    _contract_info_url, _v3_url)
import data_sources.nse_chain as chain_mod
from data_sources.nse_quote import fii_dii, gift_nifty, india_vix

FIXTURE = Path(__file__).parent / "fixtures" / "nse_chain_sample.json"
TODAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def _reset_sticky_mode():
    """The working-method cache is module-global; start every test clean."""
    previous = chain_mod._working_mode
    chain_mod._working_mode = None
    yield
    chain_mod._working_mode = previous


@pytest.fixture(autouse=True)
def _two_expiries(monkeypatch):
    monkeypatch.setattr(settings, "NSE_CHAIN_MAX_EXPIRIES", 2)


# --- FII/DII -----------------------------------------------------------------

class FakeQuoteSession:
    """Duck-typed stand-in exposing just what fii_dii() needs."""

    def __init__(self, payload):
        self.payload = payload

    def get_json(self, url):
        return self.payload


# Payload shape verified live on 2026-10-05, plus an older-session row to prove
# latest-date selection, in both FII and DII categories.
NEW_FORMAT = [
    {"buyValue": "25420.04", "category": "DII", "date": "01-Oct-2026",
     "netValue": "10041.84", "sellValue": "15378.2"},
    {"buyValue": "31200.10", "category": "FII/FPI", "date": "01-Oct-2026",
     "netValue": "-2215.45", "sellValue": "33415.55"},
    {"buyValue": "1000.00", "category": "FII/FPI", "date": "30-Sep-2026",
     "netValue": "999.99", "sellValue": "0.01"},
]

OLD_FORMAT = [
    {"category": "FII/FPI *", "date": "01-Oct-2026", "value": "-3264.90"},
    {"category": "DII **", "date": "01-Oct-2026", "value": "4120.35"},
]


def test_fii_dii_new_payload_latest_date():
    result = fii_dii(FakeQuoteSession(NEW_FORMAT))
    assert result["status"] == "OK"
    assert result["fii_net_cr"] == pytest.approx(-2215.45)
    assert result["dii_net_cr"] == pytest.approx(10041.84)
    assert result["asof"] == "01-Oct-2026"  # not the 30-Sep row


def test_fii_dii_old_payload_still_accepted():
    result = fii_dii(FakeQuoteSession(OLD_FORMAT))
    assert result["status"] == "OK"
    assert result["fii_net_cr"] == pytest.approx(-3264.90)
    assert result["dii_net_cr"] == pytest.approx(4120.35)
    assert result["asof"] == "01-Oct-2026"


def test_fii_dii_garbage_payload_is_missing():
    result = fii_dii(FakeQuoteSession({"unexpected": "dict"}))
    assert result["status"] == "MISSING"
    assert result["error"]


def test_fii_dii_unparseable_dates_is_missing():
    result = fii_dii(FakeQuoteSession([{"category": "DII", "date": "??",
                                        "netValue": "10.0"}]))
    assert result["status"] == "MISSING"


def test_india_vix_uses_moneycontrol_spot_and_nse_context():
    class Session:
        urls = []

        def get_text(self, url):
            self.urls.append(url)
            return '<input type="hidden" id="spotValue" value="15.12">'

        def get_json(self, url):
            self.urls.append(url)
            return {"data": [{"index": "NIFTY 50", "last": 25000},
                             {"index": "INDIA VIX", "last": "15.1",
                              "previousClose": "14.46", "pChange": "4.43"}]}

    session = Session()
    result = india_vix(session)

    assert session.urls == [settings.INDIA_VIX_URL,
                            settings.NSE_BASE_URL + "/api/allIndices"]
    assert result["value"] == 15.12
    assert result["prev_close"] == 14.46
    assert result["pct_change"] == 4.43
    assert result["source"] == settings.INDIA_VIX_URL


def test_india_vix_falls_back_to_all_indices():
    class Session:
        def get_text(self, _url):
            return None

        def get_json(self, _url):
            return {"data": [{"index": "INDIA VIX", "last": "15.1",
                              "previousClose": "14.46", "pChange": "4.43"}]}

    result = india_vix(Session())

    assert result["value"] == 15.1
    assert result["prev_close"] == 14.46


def test_gift_nifty_parses_moneycontrol_next_data():
    payload = {
        "props": {
            "pageProps": {
                "consumptionData": {
                    "stockData": {
                        "stkexchg": "GIFT NIFTY",
                        "lastprice": "22,558.00",
                    }
                }
            }
        }
    }

    class Session:
        urls = []

        def get_text(self, url):
            self.urls.append(url)
            return ('<script id="__NEXT_DATA__" type="application/json">'
                    + json.dumps(payload) + '</script>')

    session = Session()
    result = gift_nifty(session)

    assert session.urls == [settings.GIFT_NIFTY_SOURCES[0]]
    assert result["value"] == 22558.0
    assert result["source"] == settings.GIFT_NIFTY_SOURCES[0]


# --- Chain fetch flow ----------------------------------------------------------

class FakeFlowSession:
    """Duck-typed NSE session: first matching (substring, payload) handler wins;
    payload None means HTTP failure. Unmatched URLs 404."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []
        self.warmups = 0
        self.last_status = None

    def get_json_once(self, url):
        self.calls.append(url)
        for substring, payload in self.handlers:
            if substring in url:
                if payload is None:
                    self.last_status = 503
                    return None
                self.last_status = 200
                return copy.deepcopy(payload)
        self.last_status = 404
        return None

    def warm_up(self):
        self.warmups += 1
        return True


def _v3_part(expiry_label: str) -> dict:
    """The old-style fixture transformed into option-chain-v3 row shape for one
    expiry: row-level 'expiryDates' string, CE/PE blocks with DD-MM-YYYY dates."""
    raw = copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))
    for entry in raw["records"]["data"]:
        entry["expiryDates"] = expiry_label
        entry.pop("expiryDate", None)
        for side in ("CE", "PE"):
            if side in entry:
                entry[side]["expiryDate"] = "06-10-2026"
    raw["records"]["expiryDates"] = ["06-Oct-2026", "13-Oct-2026"]
    return raw


PART_06 = _v3_part("06-Oct-2026")
PART_13 = _v3_part("13-Oct-2026")
for entry in PART_13["records"]["data"]:
    entry["expiryDates"] = "13-Oct-2026"
CONTRACT = {"expiryDates": ["06-Oct-2026", "13-Oct-2026", "19-Oct-2026", "27-Oct-2026"]}


def test_v3_flow_fetches_and_merges_expiries():
    session = FakeFlowSession([("contract-info", CONTRACT),
                               ("expiry=06-Oct-2026", PART_06),
                               ("expiry=13-Oct-2026", PART_13)])
    raw, status, tried = fetch_option_chain_with_retries(session, retries=2, gap_sec=0)
    assert status == 200
    labels = [label for label, _ in tried]
    assert chain_mod.endpoint_label(_contract_info_url()) == labels[0]
    assert any("expiry=06-Oct-2026" in label for label in labels)
    assert any("expiry=13-Oct-2026" in label for label in labels)

    snapshot = parse_option_chain(raw, today=TODAY)
    assert snapshot.underlying == pytest.approx(24950.15)
    # rows from BOTH expiries merged (8 + 8); OI doubles so the ratio is unchanged
    assert len(snapshot.rows) == 16
    assert snapshot.pcr_total == pytest.approx(2_045_000 / 1_600_000)
    assert snapshot.nearest_expiry == "06-Oct-2026"
    # NOTE: _v3_part relabels the whole synthetic fixture to ONE expiry, so the
    # nearest-expiry slice equals the full part; per-expiry splitting of mixed
    # expiries is pinned by the original-fixture parser tests.
    assert snapshot.pcr_nearest == pytest.approx(2_045_000 / 1_600_000)
    assert chain_mod._working_mode == "v3"


def test_sticky_v3_mode_skips_legacy_and_env():
    session = FakeFlowSession([("contract-info", CONTRACT),
                               ("expiry=06-Oct-2026", PART_06),
                               ("expiry=13-Oct-2026", PART_13)])
    chain_mod._working_mode = "v3"
    raw, status, _tried = fetch_option_chain_with_retries(session, gap_sec=0)
    assert raw is not None and status == 200
    # exactly contract-info + 2 per-expiry calls, no legacy probing
    assert len(session.calls) == 3
    assert all("option-chain-indices" not in u and "option-chain-symbol" not in u
               for u in session.calls)


def test_all_methods_fail_reports_statuses_and_retries():
    # contract-info fails -> v3 dead; legacy endpoints 404 -> raw None
    session = FakeFlowSession([("contract-info", None)])
    raw, status, tried = fetch_option_chain_with_retries(session, retries=3, gap_sec=0)
    assert raw is None
    assert status == 404  # last attempt was the second legacy endpoint
    # per cycle: contract-info (failed) + 2 legacy = 3 calls, x 3 retries
    assert len(session.calls) == 9
    assert all(code in (503, 404) for _, code in tried)


def test_env_override_template(monkeypatch):
    monkeypatch.setattr(
        settings, "NSE_CHAIN_ENDPOINT",
        "https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=01-Jan-2027")
    session = FakeFlowSession([("contract-info", CONTRACT),
                               ("expiry=06-Oct-2026", PART_06),
                               ("expiry=13-Oct-2026", PART_13)])
    raw, status, tried = fetch_option_chain_with_retries(session, retries=2, gap_sec=0)
    assert raw is not None and status == 200
    # pasted URL had its expiry value auto-templated to the real nearest expiry
    assert chain_mod.endpoint_label(_v3_url("06-Oct-2026")) == tried[1][0]
    assert chain_mod._working_mode == "env"


def test_legacy_endpoint_rescued_when_restored():
    fixture_raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    session = FakeFlowSession([("option-chain-indices", fixture_raw)])
    raw, status, tried = fetch_option_chain_with_retries(session, retries=2, gap_sec=0)
    assert raw is not None and status == 200
    assert "option-chain-indices" in tried[-1][0]
    assert chain_mod._working_mode == "legacy"
