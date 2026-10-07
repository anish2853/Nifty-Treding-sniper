"""MarketDataStore tests: single fetch pass, chain validation, the +/-2%
liquidity strike band, provenance/footer, and the literal purge."""
import json
import re
from pathlib import Path

import pytest

import settings
from alerts.telegram_bot import NullTelegramSender
from data_sources.market_data import MarketDataStore
from data_sources.nse_chain import select_trade_expiry
from engines.regime import RegimeClassifier
from journal.store import JournalStore
from utils import ist_now

FIXTURE = Path(__file__).parent / "fixtures" / "nse_chain_sample.json"


@pytest.fixture()
def store(tmp_path):
    return JournalStore(db_path=tmp_path / "t.sqlite3")


class DummySession:
    """Truthy stand-in so the store's fetch guards engage; the fetchers themselves
    are monkeypatched, and the candle engine harmlessly gets None payloads."""

    last_status = None

    def warm_up(self):
        return True

    def get_json_once(self, url):
        return None

    def get_text(self, url):
        return None


def make_market(store, session=None):
    return MarketDataStore(session=session or DummySession(), journal=store,
                           holidays=set())


def test_chain_validation_symbol_mismatch(store):
    market = make_market(store)
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["records"]["data"][1]["CE"]["underlying"] = "BANKNIFTY"
    ok, reason = market._validate_chain_raw(raw)
    assert not ok and "BANKNIFTY" in reason


def test_chain_validation_strike_grid(store):
    market = make_market(store)
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["records"]["data"][1]["strikePrice"] = 24837      # not a multiple of 50
    ok, reason = market._validate_chain_raw(raw)
    assert not ok and "multiple of 50" in reason


def test_chain_validation_underlying_vs_pdc(store):
    market = make_market(store)
    market.set_prev_day(25000.0, 24800.0, 24900.0)
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))  # underlyingValue 24950.15
    ok, _ = market._validate_chain_raw(raw)
    assert ok                                              # +0.2% -> fine
    raw["records"]["underlyingValue"] = 26000.0            # +4.4% -> rejected
    ok, reason = market._validate_chain_raw(raw)
    assert not ok and "deviates" in reason


def test_one_refresh_one_consistent_set(store, monkeypatch):
    """Spec #1/#7: one refresh fills spot, chain, VIX with the SAME timestamp and
    provenance; the footer shows the stored set."""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    monkeypatch.setattr("data_sources.market_data.fetch_option_chain_with_retries",
                        lambda session: (raw, 200, [("/api/option-chain-v3", 200)]))
    monkeypatch.setattr("data_sources.market_data.india_vix",
                        lambda session: {"value": 14.18, "prev_close": 14.02,
                                         "status": "OK"})
    market = make_market(store)
    market.set_prev_day(25000.0, 24800.0, 24900.0)
    status = market.refresh()
    assert status["chain_ok"] and status["vix_ok"]
    assert market.spot.value == pytest.approx(24950.15)
    assert market.spot.asof == market.last_refresh
    assert market.vix.value["value"] == pytest.approx(14.18)
    assert market.vix.asof == market.last_refresh
    assert market.chain_source == "live NSE v3 flow"
    # the frozen expiry rule must be applied relative to the REAL clock: the
    # trade expiry always has >= 2 trading days remaining (logic itself is
    # pinned date-deterministically in test_sanity)
    expected, expected_days = select_trade_expiry(
        market.chain.expiry_dates, ist_now().date(), set())
    assert market.trade_expiry == expected
    assert market.trade_expiry_days == expected_days >= 2
    footer = market.data_footer()
    assert "spot 24,950.15" in footer and "PDC 24,900.00" in footer
    assert market.last_refresh[11:19] in footer


def test_liquidity_strikes_within_band_only(store, monkeypatch):
    """Spec #4: the map must not show strikes beyond +/-2% of the stored spot."""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # push one far-dated strike far out of range: 13-Oct row at 25000 stays, but we
    # add a 26000 call (out of band for spot ~24950) that must be filtered
    raw["records"]["data"].append({
        "strikePrice": 26000, "expiryDate": "06-Oct-2026",
        "CE": {"openInterest": 9000000, "changeinOpenInterest": 5000,
               "lastPrice": 5.0, "impliedVolatility": 12.0,
               "totalTradedVolume": 1000}})
    monkeypatch.setattr("data_sources.market_data.fetch_option_chain_with_retries",
                        lambda session: (raw, 200, []))
    monkeypatch.setattr("data_sources.market_data.india_vix",
                        lambda session: {"value": 14.0, "prev_close": 14.0,
                                         "status": "OK"})
    market = make_market(store)
    market.set_prev_day(25000.0, 24800.0, 24900.0)
    market.refresh()
    assert market.liquidity is not None
    strikes = [lv.strike for lv in market.liquidity.levels]
    assert 26000 not in strikes
    assert all(abs(s - 24950.15) <= 24950.15 * settings.LIQUIDITY_STRIKE_BAND_PCT
               for s in strikes)


def test_null_sender_never_transmits():
    sender = NullTelegramSender()
    assert sender.send("anything") is False
    assert sender.sent == ["anything"]
    assert not sender.configured


def test_no_hardcoded_market_values_in_production():
    """Spec #3: purge proof - production modules must not carry fixture-era
    literals (Nifty-scale prices, the old synthetic spot 105, etc.)."""
    forbidden = re.compile(r"\b(22600|22650|22638|24900|24950|24700|25200|22555|"
                           r"23500|24800|25000)\b")
    roots = ["main.py", "premarket_report.py", "utils.py", "watchdog.py",
             "data_sources", "engines", "alerts", "journal", "dashboard"]
    offenders = []
    for root in roots:
        path = Path(root)
        files = path.glob("*.py") if path.is_dir() else [path]
        for file in files:
            text = file.read_text(encoding="utf-8")
            for match in forbidden.finditer(text):
                offenders.append(f"{file}:{match.group()}")
    assert offenders == [], f"hardcoded market values found: {offenders}"
