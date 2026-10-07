"""Hotfix tests: freshness gate (latest candle only, backlog discard, age and
spot-consistency, fire-time window, single-spot card, cap untouched), the graded
runner protocol, and the post-exit shadow ledger."""
from datetime import datetime, timedelta

import pytest

import main
import settings
from alerts.telegram_bot import NullTelegramSender
from data_sources.market_data import MarketDataStore
from data_sources.nse_chain import parse_option_chain
from engines.candles import Candle
from engines.paper import PaperTrade
from engines.regime import RegimeClassifier
from journal import stats
from journal.store import JournalStore
from pathlib import Path
from utils import ist_now

TODAY = datetime(2026, 10, 6, tzinfo=settings.IST)
FIXTURE = Path(__file__).parent / "fixtures" / "nse_chain_sample.json"


def candle(hh, mm, close):
    start = TODAY.replace(hour=hh, minute=mm)
    return Candle(start=start, open=close, high=close + 2, low=close - 2,
                  close=close)


@pytest.fixture()
def store(tmp_path):
    return JournalStore(db_path=tmp_path / "t.sqlite3")


def make_session(store, **overrides):
    market = MarketDataStore(session=None, journal=store, holidays=set())
    market.set_prev_day(22700.0, 22500.0, 22600.0)
    session = main.DaySession(store, NullTelegramSender(),
                              RegimeClassifier(event_dates=set()),
                              event_day=False,
                              today_iso=ist_now().date().isoformat(),
                              market=market, **overrides)
    return session


def fresh_of(*candles):
    return {"fresh_candles": list(candles), "alarms": []}


# --- 1. freshness gate ----------------------------------------------------------

def test_backlog_discarded_on_first_cycle(store, monkeypatch):
    session = make_session(store)
    calls = []
    monkeypatch.setattr(session.entry, "evaluate",
                        lambda candle, prev, ctx: calls.append(candle) or [])
    monkeypatch.setattr(session.market, "refresh",
                        lambda: fresh_of(candle(9, 35, 100), candle(9, 40, 101),
                                         candle(9, 45, 102)))
    session.run_cycle()
    assert calls == []                                   # never signal candidates
    assert session.first_cycle is False
    assert store.has_journal_event(ist_now().date().isoformat(), "backlog discarded")


def test_only_latest_candle_evaluated_older_is_stale(store, monkeypatch):
    session = make_session(store)
    session.first_cycle = False
    calls = []
    monkeypatch.setattr(session.entry, "evaluate",
                        lambda candle, prev, ctx: calls.append(candle) or [])
    now = ist_now()
    old = Candle(start=now - timedelta(minutes=13), open=22600, high=22602,
                 low=22598, close=22600)                 # ended 8 min ago
    latest = Candle(start=now - timedelta(minutes=7), open=22605, high=22607,
                    low=22603, close=22605)              # ended 2 min ago
    session.market.spot = type("SV", (), {"value": 22605.0, "asof": "x",
                                          "source": "test"})()
    session.market.candles = []                          # real refresh sets this
    monkeypatch.setattr(session.market, "refresh", lambda: fresh_of(old, latest))
    session.run_cycle()
    assert calls == [latest]                             # ONLY the latest


def test_stale_age_gate_never_fires(store, monkeypatch):
    session = make_session(store)
    session.first_cycle = False
    calls = []
    monkeypatch.setattr(session.entry, "evaluate",
                        lambda candle, prev, ctx: calls.append(candle) or [])
    now = ist_now()
    stale = Candle(start=now - timedelta(minutes=12), open=22600, high=22602,
                   low=22598, close=22600)               # ended 7 min ago > 6 min
    session.market.spot = type("SV", (), {"value": 22600.0, "asof": "x",
                                          "source": "test"})()
    monkeypatch.setattr(session.market, "refresh", lambda: fresh_of(stale))
    session.run_cycle()
    assert calls == []                                   # STALE -> discarded


def test_spot_consistency_discards_drifted_setup(store, monkeypatch):
    session = make_session(store)
    session.first_cycle = False
    calls = []
    monkeypatch.setattr(session.entry, "evaluate",
                        lambda candle, prev, ctx: calls.append(candle) or [])
    now = ist_now()
    candle = Candle(start=now - timedelta(minutes=7), open=22600, high=22602,
                    low=22598, close=22600)
    session.market.spot = type("SV", (), {"value": 22800.0, "asof": "x",
                                          "source": "test"})()   # 0.88% drift
    monkeypatch.setattr(session.market, "refresh", lambda: fresh_of(candle))
    session.run_cycle()
    assert calls == []                                   # setup stale -> discarded


def test_fire_time_window_gate(store, monkeypatch):
    """Fired evaluation outside the entry windows (real clock 12:00) is
    suppressed at fire time and never journals a SIGNAL."""
    fake_now = TODAY.replace(hour=12, minute=0)
    monkeypatch.setattr(main, "ist_now", lambda: fake_now)
    session = make_session(store, data_source="live-chain")
    session.market.set_prev_day(22700.0, 22500.0, 22600.0)
    session.market.trade_expiry = "12-Oct-2026"
    row = type("R", (), {"strike": 22600, "expiry": "12-Oct-2026",
                         "ce_ltp": 120.0, "pe_ltp": 110.0})()
    session.market.chain = type("S", (), {"rows": [row],
                                          "nearest_expiry": "06-Oct-2026",
                                          "underlying": 22600.0})()
    session.market.spot = type("SV", (), {"value": 22600.0, "asof": "x",
                                          "source": "test"})()
    ev = type("E", (), {"direction": "LONG", "candle": candle(9, 35, 22600),
                        "score": 85, "level": 22580.0, "level_name": "PDH",
                        "reasons": []})()
    trade = session.fire(ev, "BREAKOUT", sl_spot=22550.0, broken_level=22580.0,
                         level_name="PDH")
    assert trade is None
    assert store.conn.execute(
        "SELECT COUNT(*) FROM journal WHERE type='SIGNAL'").fetchone()[0] == 0
    assert any("fire-time outside entry window" in n[0] for n in store.conn.execute(
        "SELECT notes FROM journal WHERE type='SKIP'").fetchall())


def test_card_prints_one_store_spot_only(store, monkeypatch):
    """The card shows the STORE spot; the candle close never appears as a spot."""
    fake_now = TODAY.replace(hour=9, minute=41)          # inside 09:30-11:00
    monkeypatch.setattr(main, "ist_now", lambda: fake_now)
    session = make_session(store, data_source="live-chain")
    session.market.set_prev_day(22700.0, 22500.0, 22500.0)   # PDC != candle close
    session.market.trade_expiry = "12-Oct-2026"
    row = type("R", (), {"strike": 22600, "expiry": "12-Oct-2026",
                         "ce_ltp": 120.0, "pe_ltp": 110.0})()
    session.market.chain = type("S", (), {"rows": [row],
                                          "nearest_expiry": "06-Oct-2026",
                                          "underlying": 22600.0})()
    session.market.spot = type("SV", (), {"value": 22610.0, "asof": "x",
                                          "source": "test"})()
    ev = type("E", (), {"direction": "LONG", "candle": candle(9, 35, 22600),
                        "score": 85, "level": 22580.0, "level_name": "PDH",
                        "reasons": []})()
    session.fire(ev, "BREAKOUT", sl_spot=22550.0, broken_level=22580.0,
                 level_name="PDH")
    card = session.tg.sent[-1]
    assert "spot 22,610.00" in card                      # the STORE spot
    assert "22,600.00" not in card                       # candle close not mixed in


def test_stale_discards_do_not_consume_cap(store, monkeypatch):
    session = make_session(store)
    session.first_cycle = False
    monkeypatch.setattr(session.entry, "evaluate", lambda candle, prev, ctx: [])
    now = ist_now()
    stale = Candle(start=now - timedelta(minutes=12), open=100, high=102, low=98,
                   close=100)
    session.market.spot = type("SV", (), {"value": 100.0, "asof": "x",
                                          "source": "test"})()
    monkeypatch.setattr(session.market, "refresh", lambda: fresh_of(stale))
    session.run_cycle()
    assert session.book.signals_today() == 0             # cap untouched
    ok, _ = session.book.caps_ok("LONG")
    assert ok


# --- 2. graded runner protocol ----------------------------------------------------

class FakeRow:
    def __init__(self, strike, ce_change=None, pe_change=None):
        self.strike = strike
        self.expiry = "06-Oct-2026"
        self.ce_oi = 500000 if ce_change is not None else None
        self.pe_oi = 500000 if pe_change is not None else None
        self.ce_change_oi = ce_change
        self.pe_change_oi = pe_change
        self.ce_ltp = 120.0
        self.pe_ltp = 110.0


def make_trade():
    trade = PaperTrade(trade_id="T01", direction="LONG", strike=22600,
                       expiry="12-Oct-2026", entry_spot=22600.0,
                       entry_prem=100.0, sl_spot=22500.0, broken_level=22580.0,
                       level_name="PDH",
                       entry_time=TODAY.replace(hour=9, minute=35), score=85)
    trade.capture_original_plan()
    trade.last_prem = 100.0
    trade.extreme_spot = 22600.0
    return trade


def seed_oi_history(store, strike, values):
    """values: change-in-OI per snapshot (oldest first)."""
    for i, value in enumerate(values, 1):
        store.conn.execute("INSERT INTO oi_snapshots (ts) VALUES (?)",
                           (f"2026-10-06T09:{i:02d}+05:30",))
        store.conn.execute(
            "INSERT INTO oi_strike_snapshots (snapshot_id, ts, expiry, strike, "
            "ce_change_oi) VALUES (?, ?, '06-Oct-2026', ?, ?)",
            (i, f"2026-10-06T09:{i:02d}", strike, value))
    store.conn.commit()


def test_grade_oi_strong_exits_fully(store, monkeypatch):
    session = make_session(store, data_source="live-chain")
    seed_oi_history(store, 22700, [1000, 3000, 5000])    # rising = wall building
    session.market.chain = type("S", (), {"rows": [FakeRow(22700, ce_change=5000)],
                                          "nearest_expiry": "06-Oct-2026",
                                          "underlying": 22600.0})()
    session.market.spot = type("SV", (), {"value": 22600.0, "asof": "x",
                                          "source": "test"})()
    trade = make_trade()
    session.book.add(trade)
    session._execute_exit(trade, ("SL_PREM", "premium SL"), spot=22600.0)
    assert session.book.trades == []                     # FULL exit
    reason = store.conn.execute(
        "SELECT exit_reason FROM journal WHERE type='EXIT'").fetchone()[0]
    assert "[OI-STRONG]" in reason
    assert any("[OI-STRONG]" in m for m in session.tg.sent)


def test_grade_oi_moderate_arms_runner(store):
    session = make_session(store, data_source="live-chain")
    session.market.chain = type("S", (), {"rows": [FakeRow(22700)],
                                          "nearest_expiry": "06-Oct-2026",
                                          "underlying": 22600.0})()  # no history
    session.market.spot = type("SV", (), {"value": 22600.0, "asof": "x",
                                          "source": "test"})()
    trade = make_trade()
    session.book.add(trade)
    session._execute_exit(trade, ("SL_PREM", "premium SL"), spot=22600.0)
    assert session.book.trades == [trade]                # NOT fully exited
    assert trade.runner_armed and trade.booked_prem == 100.0
    assert any("[OI-MODERATE]" in m and "runner at breakeven" in m
               for m in session.tg.sent)


def test_grade_wall_unwinding_rearms_and_grants_reentry(store, monkeypatch):
    fake_now = TODAY.replace(hour=9, minute=41)          # inside the entry window
    monkeypatch.setattr(main, "ist_now", lambda: fake_now)
    session = make_session(store, data_source="live-chain")
    seed_oi_history(store, 22700, [5000, 3000, 1000])    # falling = unwinding
    session.market.chain = type("S", (), {"rows": [FakeRow(22700, ce_change=1000),
                                                   FakeRow(22600, ce_change=800)],
                                          "nearest_expiry": "06-Oct-2026",
                                          "underlying": 22600.0})()
    session.market.trade_expiry = "06-Oct-2026"          # matches the fake rows
    session.market.spot = type("SV", (), {"value": 22600.0, "asof": "x",
                                          "source": "test"})()
    trade = make_trade()
    session.book.add(trade)
    session._execute_exit(trade, ("SL_PREM", "premium SL"), spot=22600.0)
    assert session.book.trades == [trade]                # trigger suppressed
    assert trade.tgt1_override is not None               # targets re-armed
    assert trade.reentry_granted and session.reentry_available["LONG"] is True
    assert any("[WALL-UNWINDING]" in m for m in session.tg.sent)
    # one re-entry per direction: cap + grant -> allowed, then consumed
    store.journal_event("SIGNAL", direction="LONG", strike=22600, spot=22600,
                        option_ltp=100.0, score=85, trade_id="T90",
                        family="BREAKOUT", reasons={})
    monkeypatch.setattr(session.book, "last_signal_ts",
                        lambda: (fake_now - timedelta(minutes=31)).isoformat())
    ok, note = session.caps_ok("LONG")
    assert ok and "re-entry granted" in note
    session.fire(type("E", (), {"direction": "LONG",
                                "candle": candle(9, 41, 22600), "score": 85,
                                "level": 22580.0, "level_name": "PDH",
                                "reasons": []})(),
                 "BREAKOUT", sl_spot=22550.0, broken_level=22580.0,
                 level_name="PDH")
    assert session.reentry_available["LONG"] is False     # grant consumed
    ok, note = session.caps_ok("LONG")
    assert not ok and "cap reached" in note


# --- 3. post-exit shadow ------------------------------------------------------------

def test_shadow_saved_when_exit_beats_original_plan(store):
    # EXIT at 10:05; the original SL is hit 5 minutes later -> exiting SAVED money
    trade = make_trade()
    trade.entry_spot, trade.entry_prem = 22600.0, 100.0
    store.journal_event("EXIT", direction="LONG", strike=22600, spot=22560,
                        option_ltp=95.0, result_r=-0.1, exit_reason="OI_FLIP "
                        "[OI-STRONG]: fresh writing", trade_id="T01",
                        pnl_rupees=-75.0, family="BREAKOUT",
                        reasons={"shadow": {
                            "direction": "LONG", "entry_spot": 22600.0,
                            "entry_prem": 100.0, "sl_spot": 22550.0,
                            "orig_tgt1": 22700.0, "orig_tgt2": 22800.0,
                            "grade": "OI-STRONG", "code": "OI_FLIP",
                            "exit_ts": "2026-10-06T10:05:00+05:30"}},
                        notes="exit T01")
    # after the exit: spot slides through the original SL
    store.upsert_candles([
        Candle(start=TODAY.replace(hour=10, minute=5), open=22560, high=22565,
               low=22500, close=22510)])
    report = stats.shadow_report(store.conn, ist_now().date().isoformat())
    t = report["trades"][0]
    assert t["bucket"] == "OI-strong"
    assert t["saved"] > 0 and t["cost"] == 0             # exit protected us


def test_shadow_cost_when_original_plan_wins(store):
    # EXIT at 10:05; the original plan would have hit TGT 2 -> we missed profit
    store.journal_event("EXIT", direction="LONG", strike=22600, spot=22600,
                        option_ltp=100.0, result_r=0.1, exit_reason="TIME_STOP "
                        "[OI-N/A]: time stop", trade_id="T02",
                        pnl_rupees=75.0, family="BREAKOUT",
                        reasons={"shadow": {
                            "direction": "LONG", "entry_spot": 22600.0,
                            "entry_prem": 100.0, "sl_spot": 22500.0,
                            "orig_tgt1": 22700.0, "orig_tgt2": 22800.0,
                            "grade": "OI-N/A", "code": "TIME_STOP",
                            "exit_ts": "2026-10-06T10:05:00+05:30"}},
                        notes="exit T02")
    store.upsert_candles([
        Candle(start=TODAY.replace(hour=10, minute=5), open=22600, high=22610,
               low=22590, close=22605),
        Candle(start=TODAY.replace(hour=10, minute=10), open=22605, high=22710,
               low=22600, close=22700),                   # TGT 1 hit (22700)
        Candle(start=TODAY.replace(hour=10, minute=15), open=22700, high=22850,
               low=22695, close=22800)])                  # TGT 2 hit (22800)
    report = stats.shadow_report(store.conn, ist_now().date().isoformat())
    t = report["trades"][0]
    assert t["bucket"] == "time"
    assert t["cost"] > 0 and t["saved"] == 0               # profit missed
    assert t["shadow_r"] > t["actual_r"]


def test_eod_body_carries_shadow_blocks(store):
    test_shadow_saved_when_exit_beats_original_plan(store)
    body = stats.build_eod_body(store, ist_now().date().isoformat(), "NORMAL", [])
    assert "EXIT SHADOW (original plan tracked to 15:10)" in body
    assert "SHADOW TOTALS BY EXIT TYPE" in body
    assert "OI-strong" in body
