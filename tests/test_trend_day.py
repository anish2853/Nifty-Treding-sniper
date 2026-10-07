"""Trend-Day Engine v2 tests: day-state classification (VWAP proxy, liquidity
break, structure), bonus scoring, the PULLBACK setup, OI-whispers exits,
structure trail, profit lock and the 14:30 cutoff."""
from datetime import datetime, timedelta

import pytest

import main
import settings
from alerts.telegram_bot import NullTelegramSender
from data_sources.market_data import MarketDataStore
from engines.candles import Candle
from engines.day_state import RANGE, TREND_UP, DayStateEngine
from engines.paper import PaperTrade
from engines.regime import RegimeClassifier
from journal.store import JournalStore
from utils import ist_now

TODAY = datetime(2026, 10, 6, tzinfo=settings.IST)


def candle(hh, mm, o, h, l, c, volume=None):
    return Candle(start=TODAY.replace(hour=hh, minute=mm), open=o, high=h,
                  low=l, close=c, volume=volume)


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


# --- A. day-state engine -------------------------------------------------------

def test_vwap_proxy_when_volume_missing():
    engine = DayStateEngine()
    candles = [candle(9, 15, 100, 110, 99, 105), candle(9, 20, 105, 112, 104, 111)]
    state = engine.update(candles, pdh=None, pdl=None, spot=111.0)
    assert state.vwap_proxy is True
    # proxy = mean of typical prices: ((110+99+105)/3 + (112+104+111)/3) / 2
    expected = ((110 + 99 + 105) / 3 + (112 + 104 + 111) / 3) / 2
    assert state.vwap == pytest.approx(expected)
    assert state.ema20 is not None


def test_liquidity_break_and_trend_up():
    engine = DayStateEngine()
    candles = [candle(9, 15, 22590, 22600, 22580, 22595),
               candle(9, 20, 22600, 22610, 22590, 22605),
               candle(9, 25, 22605, 22720, 22600, 22710)]   # close > PDH 22700
    state = engine.update(candles, pdh=22700.0, pdl=22500.0, spot=22705.0)
    assert state.broken_level == 22700.0 and state.broken_side == "UP"
    assert state.state == TREND_UP
    assert state.last_higher_low is not None               # ratcheting structure


def test_range_day_when_no_break():
    engine = DayStateEngine()
    candles = [candle(9, 15, 22590, 22620, 22580, 22600),
               candle(9, 20, 22600, 22640, 22590, 22630),
               candle(9, 25, 22630, 22650, 22600, 22620)]
    state = engine.update(candles, pdh=22700.0, pdl=22500.0, spot=22620.0)
    assert state.broken_level is None and state.state == RANGE


def test_structure_break_reevaluates(store):
    engine = DayStateEngine()
    candles = [candle(9, 15, 22590, 22600, 22580, 22595),
               candle(9, 20, 22600, 22720, 22590, 22710),
               candle(9, 25, 22705, 22730, 22700, 22725)]   # higher low held
    state = engine.update(candles, pdh=22700.0, pdl=22500.0, spot=22720.0)
    assert state.state == TREND_UP and state.structure_intact
    # next candle closes below the last higher low -> structure breaks
    candles.append(candle(9, 30, 22700, 22705, 22600, 22610))
    state = engine.update(candles, pdh=22700.0, pdl=22500.0, spot=22610.0)
    assert state.structure_intact is False and state.state == RANGE


# --- B. bonus scoring ------------------------------------------------------------

def test_bonus_confluence_and_vwap(store):
    session = make_session(store)
    session.market.set_prev_day(22700.2, 22500.0, 22600.0)  # PDH ~= ORB high
    session.day_state = type("DS", (), {"state": "TREND-UP", "vwap": 22550.0,
                                        "vwap_proxy": True, "broken_level": 22700.0,
                                        "structure_intact": True,
                                        "as_ctx": lambda self=None: {}})()
    session.market.candles = [candle(9, 15, 22590, 22700.0, 22580, 22595),
                              candle(9, 20, 22600, 22720, 22590, 22710)]
    # ORB high = PDH (22700.2 vs 22700.0 -> within 0.15%) + spot above VWAP
    orb = {"high": 22700.0, "low": 22580.0, "mid": 22640.0}
    prev = candle(9, 30, 22700, 22710, 22690, 22705)
    brk = candle(9, 35, 22705, 22730, 22700, 22725)         # breaks ORB high again
    evs = session.entry.evaluate(brk, prev, {
        "candle": brk, "snapshot": None, "spot": 22725.0, "vix_spike": False,
        "event_day": False, "regime_allows": lambda d, l: (True, ""),
        "caps_ok": lambda d: (True, ""), "option_ltp": lambda d, s: 120.0,
        "candle_engine": None, "orb": orb, "pdh": 22700.2, "pdl": 22500.0,
        "vwap": 22550.0, "now": ist_now()})
    long_ev = evs[0]
    bonuses = [points for name, ok, points, _ in long_ev.reasons
               if name.startswith("BONUS") and ok]
    assert len(bonuses) == 2                                # +10 confluence, +10 VWAP
    assert long_ev.score >= 80


# --- C. pullback setup -------------------------------------------------------------

def test_pullback_fires_on_trend_day(store):
    session = make_session(store, data_source="live-chain")
    session.market.trade_expiry = "06-Oct-2026"
    row = type("R", (), {"strike": 22600, "expiry": "06-Oct-2026",
                         "ce_ltp": 120.0, "pe_ltp": 110.0})()
    session.market.chain = type("S", (), {"rows": [row],
                                          "nearest_expiry": "06-Oct-2026",
                                          "underlying": 22600.0})()
    session.market.spot = type("SV", (), {"value": 22602.0, "asof": "x",
                                          "source": "test"})()
    session.day_state = type("DS", (), {
        "state": "TREND-UP", "vwap": 22595.0, "vwap_proxy": True,
        "broken_level": 22600.0, "broken_side": "UP",
        "last_higher_low": 22590.0, "last_lower_high": None,
        "structure_intact": True, "notes": [],
        "as_ctx": lambda self=None: {"state": "TREND-UP",
                                     "structure_intact": True,
                                     "vwap": 22595.0,
                                     "broken_level": 22600.0,
                                     "broken_side": "UP"}})()
    # pullback to the broken PDH (22600) with a bullish close off it
    pull = candle(10, 5, 22600, 22601, 22596, 22600.5)
    prev = candle(10, 0, 22610, 22615, 22600, 22602)
    monkey_evs = session.pullbacks.evaluate(pull, prev, {
        "day_state": session.day_state.as_ctx(), "vwap": 22595.0,
        "snapshot": session.market.chain, "spot": 22600.5,
        "candle_engine": None,
        "caps_ok": lambda d: (True, ""),
        "option_ltp": lambda d, s: 120.0})
    assert len(monkey_evs) == 1
    ev = monkey_evs[0]
    assert ev.direction == "LONG" and ev.level == 22600.0
    assert ev.sl_spot == 22595.0                            # level - 5 (frozen)
    # score: touch 20 + rejection 20 + OI unknown 0 + vwap/structure 15
    #        + trend 15 + volume missing 0 = 70 -> near-miss band
    assert ev.score == 70 and ev.near_miss


def test_pullback_ignored_outside_trend(store):
    session = make_session(store)
    session.day_state = type("DS", (), {"state": "RANGE", "vwap": 22595.0,
                                        "broken_level": None,
                                        "as_ctx": lambda self=None: {}})()
    pull = candle(10, 5, 22600, 22601, 22596, 22600.5)
    assert session.pullbacks.evaluate(pull, None, {
        "day_state": session.day_state.as_ctx(), "vwap": 22595.0}) == []


# --- D. OI-whispers exits -----------------------------------------------------------

def seed_oi_history(store, strike, values):
    for i, value in enumerate(values, 1):
        store.conn.execute("INSERT INTO oi_snapshots (ts) VALUES (?)",
                           (f"2026-10-06T09:{i:02d}+05:30",))
        store.conn.execute(
            "INSERT INTO oi_strike_snapshots (snapshot_id, ts, expiry, strike, "
            "ce_change_oi) VALUES (?, ?, '06-Oct-2026', ?, ?)",
            (i, f"2026-10-06T09:{i:02d}", strike, value))
    store.conn.commit()


def make_trade(**overrides):
    trade = PaperTrade(trade_id="T01", direction="LONG", strike=22600,
                       expiry="12-Oct-2026", entry_spot=22600.0,
                       entry_prem=100.0, sl_spot=22500.0, broken_level=22700.0,
                       level_name="PDH",
                       entry_time=TODAY.replace(hour=9, minute=35), score=85)
    trade.capture_original_plan()
    trade.last_prem = 100.0
    trade.extreme_spot = 22600.0
    for key, value in overrides.items():
        setattr(trade, key, value)
    return trade


def test_oi_flip_books_half_and_sets_structure_trail(store):
    session = make_session(store, data_source="live-chain")
    seed_oi_history(store, 22700, [1000, 3000, 5000])    # rising = flip
    session.day_state = type("DS", (), {"state": "TREND-UP",
                                        "last_higher_low": 22590.0,
                                        "structure_intact": True})()
    session.market.chain = type("S", (), {"rows": [], "nearest_expiry":
                                          "06-Oct-2026", "underlying": 22600.0})()
    session.market.spot = type("SV", (), {"value": 22600.0, "asof": "x",
                                          "source": "test"})()
    trade = make_trade()
    session.book.add(trade)
    session._execute_exit(trade, ("OI_FLIP", "fresh writing beyond spot"),
                          spot=22600.0)
    assert session.book.trades == [trade]                # flip ALONE: no full exit
    assert trade.runner_armed and trade.booked_prem == 100.0
    # trail = max(broken level 22700, last higher low 22590) = 22700
    assert trade.trail_stop == 22700.0
    assert any("[OI-FLIP]" in m and "22700" in m for m in session.tg.sent)


def test_second_flip_within_30_min_exits_fully(store):
    session = make_session(store, data_source="live-chain")
    seed_oi_history(store, 22700, [1000, 3000, 5000])
    session.day_state = type("DS", (), {"state": "TREND-UP",
                                        "last_higher_low": 22590.0})()
    session.market.chain = type("S", (), {"rows": [], "nearest_expiry":
                                          "06-Oct-2026", "underlying": 22600.0})()
    session.market.spot = type("SV", (), {"value": 22600.0, "asof": "x",
                                          "source": "test"})()
    trade = make_trade()
    trade.flip_times = [ist_now() - timedelta(minutes=10)]   # recent flip
    session.book.add(trade)
    session._execute_exit(trade, ("OI_FLIP", "fresh writing again"), spot=22600.0)
    assert session.book.trades == []                     # wall thickening -> out
    reason = store.conn.execute(
        "SELECT exit_reason FROM journal WHERE type='EXIT'").fetchone()[0]
    assert "SECOND_FLIP" in reason


def test_structure_break_exits_trend_runner(store):
    session = make_session(store, data_source="live-chain")
    session.market.chain = None
    trade = make_trade()
    trade.trail_stop = 22590.0                           # structure trail armed
    session.book.add(trade)
    brk = candle(10, 30, 22595, 22596, 22540, 22550)     # closes below the trail
    session._execute_exit(trade, ("STRUCTURE_BREAK", "close below trail"),
                          spot=22550.0)
    assert session.book.trades == []
    reason = store.conn.execute(
        "SELECT exit_reason FROM journal WHERE type='EXIT'").fetchone()[0]
    assert "STRUCTURE_BREAK" in reason


def test_time_stop_applies_only_in_range(store):
    session = make_session(store, data_source="live-chain")
    session.market.chain = None
    late = ist_now() + timedelta(minutes=120)
    trade = make_trade(entry_time=ist_now() - timedelta(minutes=120))
    trend_ctx = {"candle": None, "ltp": 99.0, "vix_spike": False,
                 "now": late, "day_state": {"state": "TREND-UP"}}
    assert session.exit_engine.check(trade, trend_ctx) is None   # trend: hold
    range_ctx = dict(trend_ctx, day_state={"state": "RANGE"})
    decision = session.exit_engine.check(trade, range_ctx)
    assert decision[0] == "TIME_STOP"


# --- E. profit lock + 14:30 cutoff ---------------------------------------------------

def test_profit_lock_banks_at_two_r(store):
    session = make_session(store, data_source="live-chain")
    session.market.set_prev_day(22700.0, 22500.0, 22600.0)
    for i, rr in enumerate((1.2, 0.9), 1):               # day total +2.1R
        store.journal_event("EXIT", direction="LONG", strike=22600,
                            option_ltp=100.0, result_r=rr, exit_reason="TGT_2R",
                            trade_id=f"T{i}", pnl_rupees=rr * 100, family="ORB",
                            notes="exit")
    session._check_halt()
    assert session.trading_halted_reason is not None
    assert "PROFIT LOCK" in session.trading_halted_reason
    # entries are now refused
    ev = type("E", (), {"direction": "LONG", "candle": candle(11, 0, 22590, 22610, 22580, 22600),
                        "score": 100, "level": 22580.0, "level_name": "PDH",
                        "reasons": []})()
    trade = session.fire(ev, "ORB", sl_spot=22550.0, broken_level=22580.0,
                         level_name="PDH")
    assert trade is None
    assert any("TRADING HALTED" in m for m in session.tg.sent)


def test_no_new_entries_after_1430(store, monkeypatch):
    fake_now = TODAY.replace(hour=14, minute=31)
    monkeypatch.setattr(main, "ist_now", lambda: fake_now)
    session = make_session(store, data_source="live-chain")
    session.market.set_prev_day(22700.0, 22500.0, 22600.0)
    session.market.trade_expiry = "12-Oct-2026"
    session.market.chain = type("S", (), {"rows": [], "nearest_expiry":
                                          "06-Oct-2026", "underlying": 22600.0})()
    session.market.spot = type("SV", (), {"value": 22600.0, "asof": "x",
                                          "source": "test"})()
    ev = type("E", (), {"direction": "LONG", "candle": candle(14, 25, 22590, 22610, 22580, 22600),
                        "score": 100, "level": 22580.0, "level_name": "PDH",
                        "reasons": []})()
    trade = session.fire(ev, "ORB", sl_spot=22550.0, broken_level=22580.0,
                         level_name="PDH")
    assert trade is None
    assert any("after 14:30" in n[0] for n in store.conn.execute(
        "SELECT notes FROM journal WHERE type='SKIP'").fetchall())


def test_range_day_blocks_breakout_but_not_sweep(store, monkeypatch):
    fake_now = TODAY.replace(hour=10, minute=0)
    monkeypatch.setattr(main, "ist_now", lambda: fake_now)
    session = make_session(store, data_source="live-chain")
    session.market.set_prev_day(22700.0, 22500.0, 22600.0)
    session.market.trade_expiry = "12-Oct-2026"
    session.market.chain = type("S", (), {"rows": [], "nearest_expiry":
                                          "06-Oct-2026", "underlying": 22600.0})()
    session.market.spot = type("SV", (), {"value": 22600.0, "asof": "x",
                                          "source": "test"})()
    session.day_state = type("DS", (), {"state": "RANGE", "vwap": 22600.0,
                                        "broken_level": None,
                                        "structure_intact": True,
                                        "as_ctx": lambda self=None: {
                                            "state": "RANGE"}})()
    ev = type("E", (), {"direction": "LONG", "candle": candle(10, 0, 22590, 22610, 22580, 22600),
                        "score": 100, "level": 22580.0, "level_name": "PDH",
                        "reasons": []})()
    assert session._entry_permission("ORB") is not None
    assert session._entry_permission("SWEEP") is None    # sweeps still allowed
