"""Hotfix tests: hard data sanity gates (store-fed), the >=2-trading-day expiry
rule, DATA line on the trade card, and refusal to fire on non-live data."""
from datetime import date, datetime

import pytest

import main
import settings
from alerts.telegram_bot import NullTelegramSender
from data_sources.market_data import MarketDataStore
from data_sources.nse_chain import select_trade_expiry
from engines.candles import Candle
from engines.regime import RegimeClassifier
from journal.store import JournalStore
from utils import ist_now

TODAY = datetime(2026, 10, 5, tzinfo=settings.IST)   # Monday, = the current weekly


@pytest.fixture()
def store(tmp_path):
    return JournalStore(db_path=tmp_path / "t.sqlite3")


def make_session(store, market=None, **overrides):
    market = market or MarketDataStore(session=None, journal=store, holidays=set())
    session = main.DaySession(store, NullTelegramSender(),
                              RegimeClassifier(event_dates=set()),
                              event_day=False,
                              today_iso=ist_now().date().isoformat(),
                              market=market, **overrides)
    return session


# --- expiry rule (hotfix #4) ---------------------------------------------------

def test_expiry_rule_expiry_day_rolls_to_next_week():
    label, days = select_trade_expiry(["05-Oct-2026", "12-Oct-2026"],
                                      date(2026, 10, 5))
    assert label == "12-Oct-2026" and days == 6


def test_expiry_rule_two_days_keeps_current_week():
    label, days = select_trade_expiry(["08-Oct-2026", "15-Oct-2026"],
                                      date(2026, 10, 6))
    assert label == "08-Oct-2026" and days == 3


def test_expiry_rule_one_day_rolls():
    label, days = select_trade_expiry(["09-Oct-2026", "16-Oct-2026"],
                                      date(2026, 10, 9))
    assert label == "16-Oct-2026" and days == 6


def test_expiry_rule_holiday_aware():
    label, days = select_trade_expiry(["01-Oct-2026", "08-Oct-2026"],
                                      date(2026, 9, 30),
                                      holiday_dates={"2026-10-01"})
    assert label == "08-Oct-2026"


def test_option_ltp_for_expiry_filter():
    row_near = type("R", (), {"strike": 22600, "expiry": "06-Oct-2026",
                              "ce_ltp": 10.0, "pe_ltp": 12.0})()
    row_next = type("R", (), {"strike": 22600, "expiry": "13-Oct-2026",
                              "ce_ltp": 40.0, "pe_ltp": 44.0})()
    snapshot = type("S", (), {"rows": [row_near, row_next],
                              "nearest_expiry": "06-Oct-2026"})()
    assert main.option_ltp_for(snapshot, 22600, "LONG") == 10.0
    assert main.option_ltp_for(snapshot, 22600, "LONG",
                               expiry="13-Oct-2026") == 40.0


# --- data gates (fed ONLY by the MarketDataStore) ---------------------------------

def test_data_gate_pdc_missing_blocks(store):
    session = make_session(store)          # store has no prev-day levels
    ok, reason = session._data_gate(22600.0)
    assert not ok and "PDC unavailable" in reason


def test_data_gate_spot_deviation_blocks(store):
    session = make_session(store)
    session.market.set_prev_day(22700.0, 22500.0, 22600.0)
    ok, reason = session._data_gate(23500.0)     # +3.98% > 3%
    assert not ok and "deviates" in reason
    ok, _ = session._data_gate(22620.0)          # +0.09% -> fine
    assert ok


def test_data_gate_non_live_source_blocks(store):
    session = make_session(store, data_source="FIXTURE/TEST")
    session.market.set_prev_day(22700.0, 22500.0, 22600.0)
    ok, reason = session._data_gate(22620.0)
    assert not ok and "FIXTURE/TEST" in reason


def test_ltp_plausibility_band():
    assert main.ltp_is_plausible(120.0, 22600.0)
    assert not main.ltp_is_plausible(0.3, 22600.0)       # below Rs 0.5 floor
    assert not main.ltp_is_plausible(2400.0, 22600.0)    # > 10% of spot
    assert not main.ltp_is_plausible(None, 22600.0)


def test_process_candle_close_suppresses_without_pdc(store):
    """No prev-day levels -> the gate kills evaluation before any engine runs,
    journals one SKIP and alerts DATA MISMATCH (deduped)."""
    session = make_session(store)
    brk = Candle(start=TODAY.replace(hour=9, minute=35), open=104, high=113,
                 low=103, close=105)
    session.process_candle_close(brk)
    session.process_candle_close(brk)   # second attempt: alert must be deduped
    rows = store.conn.execute(
        "SELECT type, notes FROM journal ORDER BY id").fetchall()
    assert any(t == "SKIP" and "data mismatch" in n for t, n in rows)
    assert len([r for r in rows if "data mismatch" in r[1]]) == 1
    assert store.conn.execute(
        "SELECT COUNT(*) FROM journal WHERE type='SIGNAL'").fetchone()[0] == 0


def test_fire_refuses_implausible_ltp_and_bad_atm(store, monkeypatch):
    """Direct fire-path gates: wrong-scale ATM and out-of-band LTP never journal.
    Clock frozen inside the entry window so the FIRE-TIME gate passes."""
    fake_now = TODAY.replace(hour=9, minute=41)
    monkeypatch.setattr(main, "ist_now", lambda: fake_now)
    session = make_session(store, data_source="live-chain")
    session.market.set_prev_day(22700.0, 22500.0, 22600.0)
    session.market.trade_expiry = "12-Oct-2026"

    class FakeRow:
        strike = 22600
        expiry = "12-Oct-2026"
        ce_ltp = 0.2                    # implausible (below Rs 0.5)
        pe_ltp = 2400.0                 # implausible (> 10% of spot)
    session.market.chain = type("S", (), {"rows": [FakeRow],
                                          "nearest_expiry": "06-Oct-2026",
                                          "underlying": 22600.0})()
    ev = type("E", (), {"direction": "LONG", "candle": Candle(
        start=TODAY.replace(hour=9, minute=35), open=22590, high=22610,
        low=22580, close=22600), "score": 85, "level": 22580.0,
        "level_name": "PDH", "reasons": []})()

    trade = session.fire(ev, "BREAKOUT", sl_spot=22570.0,
                         broken_level=22580.0, level_name="PDH")
    assert trade is None
    notes = store.conn.execute(
        "SELECT notes FROM journal WHERE type='SKIP'").fetchall()
    assert any("implausible" in n[0] for n in notes)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM journal WHERE type='SIGNAL'").fetchone()[0] == 0


def test_notify_choke_point_blocks_non_live(store):
    """The Telegram choke point never transmits for a non-live data source."""
    session = make_session(store, data_source="FIXTURE/TEST")
    session._notify("would-be leak")
    assert session.tg.sent == []


def test_data_mismatch_message_format():
    from alerts import messages
    assert messages.data_mismatch("spot 105.00 deviates +99% from PDC") == \
        "⚠️ DATA MISMATCH — signal suppressed (spot 105.00 deviates +99% from PDC)"


def test_trade_card_prints_data_source():
    from alerts import messages
    from engines.paper import PaperTrade
    trade = PaperTrade(trade_id="T01", direction="LONG", strike=22600,
                       expiry="12-Oct-2026", entry_spot=22600.0, entry_prem=120.0,
                       sl_spot=22550.0, broken_level=22580.0, level_name="PDH",
                       entry_time=TODAY.replace(hour=9, minute=35), score=85,
                       candle_start=TODAY.replace(hour=9, minute=35))
    card = messages.trade_card(signal_no=1, direction="LONG", trade=trade,
                               spot=22600.0, now_txt="09:36:00",
                               data_source="live-chain | spot 22,612 | "
                                           "PDC 22,555 | store 10:19:12 IST")
    assert "DATA: live-chain | spot 22,612 | PDC 22,555 | store 10:19:12 IST" in card
    assert "🎯 SIGNAL #1 [BREAKOUT] — BUY CE 22600 (weekly expiry 12-Oct-2026)" in card
