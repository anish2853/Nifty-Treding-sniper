"""Phase B engine tests: candle bucketing, ORB, the frozen entry scoring matrix,
exit triggers in priority order, paper accounting and crash recovery."""
from datetime import datetime, timedelta

import pytest

import settings
from data_sources.nse_chain import parse_option_chain
from engines.candles import Candle, CandleEngine, orb_levels, parse_chart_payload
from engines.entry import EntryEngine
from engines.exit import ExitEngine
from engines.paper import PaperTrade
from engines.oi_engine import classify_oi_window, recent_oi_net
from journal.store import JournalStore
from utils import ist_now

TODAY = datetime(2026, 10, 5, tzinfo=settings.IST)


def candle(hh, mm, o, h, l, c, volume=None):
    return Candle(start=TODAY.replace(hour=hh, minute=mm, second=0, microsecond=0),
                  open=o, high=h, low=l, close=c, volume=volume)


# --- candle engine ---------------------------------------------------------------

def test_parse_chart_payload_buckets_and_skips_preopen():
    # IST-as-UTC epochs: 09:00:00 IST = 32_400_000 ms, 09:15:00 = 33_300_000 ms
    raw = {"grapthData": [
        [32_400_000, 100.0, "PO"],           # pre-open 09:00 tick -> excluded
        [33_300_000, 110.0, "NM"],           # 09:15:00
        [33_360_000, 112.0, "NM"],           # 09:16:00
        [33_540_000, 108.0, "NM"],           # 09:19:00
        [33_600_000, 111.0, "NM"],           # 09:20:00 (next bucket)
    ]}
    candles = parse_chart_payload(raw)
    assert len(candles) == 2
    first, second = candles
    assert first.start.hour == 9 and first.start.minute == 15
    assert (first.open, first.high, first.low, first.close) == (110.0, 112.0, 108.0, 108.0)
    assert second.start.minute == 20 and second.close == 111.0


def test_orb_levels():
    candles = [candle(9, 15, 100, 110, 99, 105), candle(9, 20, 108, 112, 107, 111),
               candle(9, 25, 105, 109, 104, 109), candle(9, 35, 110, 115, 109, 114)]
    orb = orb_levels(candles)
    assert orb["high"] == 112 and orb["low"] == 99 and orb["mid"] == (112 + 99) / 2
    assert orb["candles"] == 3


def test_engine_sample_fallback_and_fresh_candles():
    engine = CandleEngine(session=None, store=None)
    now = TODAY.replace(hour=9, minute=41)
    # samples inside the 09:35-09:40 window -> closed at 09:40
    engine.add_spot_sample(100.0, now.replace(minute=35))
    engine.add_spot_sample(102.0, now.replace(minute=37))
    engine.add_spot_sample(101.0, now.replace(minute=39))
    engine.add_spot_sample(103.0, now.replace(minute=41))  # forms the next candle
    fresh = engine.refresh(now=now)
    assert len(fresh) == 1
    c = fresh[0]
    assert c.source == "sample" and (c.open, c.high, c.low, c.close) == (100, 102, 100, 101)
    assert c.end <= now


def test_engine_chart_source_and_incremental_fresh():
    ticks = []
    base = 9 * 3600 + 15 * 60  # 09:15:00 IST-as-UTC seconds
    for sec in range(0, 600):  # 09:15-09:25
        ticks.append([(base + sec) * 1000, 100.0 + sec * 0.01, "NM"])
    session = type("S", (), {"get_json_once": lambda self, url: {"grapthData": ticks},
                             "last_status": 200})()
    engine = CandleEngine(session=session, store=None)
    now = TODAY.replace(hour=9, minute=26)
    fresh = engine.refresh(now=now)
    assert len(fresh) == 2 and all(c.source == "chart" for c in fresh)
    fresh2 = engine.refresh(now=now)
    assert fresh2 == []  # nothing newly closed on the second poll


# --- entry engine (frozen scoring matrix) ------------------------------------------

class FakeSnapshot:
    def __init__(self):
        self.underlying = 100.0
        self.nearest_expiry = "06-Oct-2026"
        self.rows = []


def make_ctx(orb, **overrides):
    ctx = {
        "candle": None, "snapshot": FakeSnapshot(), "spot": None,
        "vix_spike": False, "event_day": False,
        "regime_allows": lambda d, l: (True, ""),
        "caps_ok": lambda d: (True, ""),
        "option_ltp": lambda d, s: 100.0,
        "candle_engine": None,
        "orb": orb, "pdh": None, "pdl": None,
        "now": ist_now(),
    }
    ctx.update(overrides)
    return ctx


def make_store(tmp_path):
    return JournalStore(db_path=tmp_path / "t.sqlite3")


def test_entry_scores_frozen_matrix(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    engine = EntryEngine(store)
    orb = {"high": 112.0, "low": 99.0, "mid": 105.5}
    prev = candle(9, 30, 104, 106, 103, 105)
    # break only, no volume, OI unknown -> 20+15+10 = 45 (below the journal band)
    ev = engine.evaluate(candle(9, 35, 105, 116, 104, 115), prev, make_ctx(orb))[0]
    assert ev.direction == "LONG" and ev.broke and ev.level_name == "ORB high"
    assert not ev.confirmed
    assert ev.score == 45 and not ev.near_miss and not ev.fired

    # OI PASS (writers covering) without confirmation -> 65 -> near-miss, journaled
    with_oi = make_ctx(orb)
    monkeypatch.setattr(EntryEngine, "_oi_state",
                        lambda self, ctx, d, s: ("PASS", "24950:-3000"))
    ev2 = engine.evaluate(candle(9, 35, 105, 116, 104, 115), prev, with_oi)[0]
    assert ev2.score == 65 and ev2.near_miss and not ev2.fired

    # confirmation on top -> 85 -> FIRED
    prev2 = candle(9, 30, 104, 116, 103, 113)   # prior close 113 > ORB high 112
    ev3 = engine.evaluate(candle(9, 35, 105, 116, 104, 115), prev2, with_oi)[0]
    assert ev3.confirmed and ev3.score == 85 and ev3.fired

    # volume component present on the break candle -> 80 exactly -> fires without
    # confirmation only when volume beats the 10-candle average
    engine_vol = EntryEngine(store)
    brk = candle(9, 35, 105, 116, 104, 115, volume=1000)
    # craft a candle engine with >=10 prior candles of volume 100, break candle included
    ce = CandleEngine(session=None, store=None)
    ce.candles = [candle(9, 15, 100, 110, 99, 105, volume=100),
                  candle(9, 20, 108, 112, 107, 111, volume=100),
                  candle(9, 25, 105, 109, 104, 109, volume=100),
                  candle(9, 30, 104, 106, 103, 105, volume=100)] + \
                 [candle(8, 30 + i, 100, 101, 99, 100, volume=100) for i in range(7)]
    ce.candles.append(brk)
    vol_ctx = make_ctx(orb, candle_engine=ce)
    ev4 = engine_vol.evaluate(brk, None, vol_ctx)[0]
    assert ev4.volume_state == "PASS" and ev4.score == 80 and ev4.fired


def test_entry_oi_reject_overrides_score(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    engine = EntryEngine(store)
    monkeypatch.setattr(EntryEngine, "_oi_state",
                        lambda self, ctx, d, s: ("REJECT", "24950:+5000"))
    orb = {"high": 112.0, "low": 99.0, "mid": 105.5}
    prev = candle(9, 30, 104, 116, 103, 113)  # confirmation present
    ev = engine.evaluate(candle(9, 35, 105, 116, 104, 115), prev, make_ctx(orb))[0]
    assert ev.rejected and not ev.fired
    assert "fresh writing wall" in ev.reject_reason


def test_entry_outside_window_never_evaluates(tmp_path):
    store = make_store(tmp_path)
    engine = EntryEngine(store)
    orb = {"high": 112.0, "low": 99.0, "mid": 105.5}
    assert engine.evaluate(candle(11, 5, 110, 120, 109, 119), None,
                           make_ctx(orb)) == []


def test_regime_gates_frozen(tmp_path):
    store = make_store(tmp_path)
    engine = EntryEngine(store)
    orb = {"high": 112.0, "low": 99.0, "mid": 105.5}
    prev = candle(9, 30, 104, 116, 103, 113)
    ctx = make_ctx(orb, regime_allows=lambda d, l: (False, "gap-up-extension: no fresh ORB longs"))
    ev = engine.evaluate(candle(9, 35, 105, 116, 104, 115), prev, ctx)[0]
    # break 20 + confirm 20 + window 10 = 50 with the regime component denied;
    # firing requires regime_ok regardless of score
    assert ev.score == 50 and not ev.fired and not ev.regime_ok


# --- exit engine + accounting -------------------------------------------------------

def make_trade(**overrides):
    trade = PaperTrade(
        trade_id="T01", direction="LONG", strike=115, expiry="06-Oct-2026",
        entry_spot=115.0, entry_prem=50.0, sl_spot=105.0, broken_level=112.0,
        level_name="ORB high", entry_time=TODAY.replace(hour=9, minute=35),
        score=85)
    trade.extreme_spot = 115.0
    trade.last_prem = 50.0
    for key, value in overrides.items():
        setattr(trade, key, value)
    return trade


def test_exit_priority_order(tmp_path):
    store = make_store(tmp_path)
    exit_engine = ExitEngine(store)
    now = TODAY.replace(hour=10, minute=0)   # controlled clock: 25 min after entry
    trade = make_trade()
    # a) candle close beyond SL spot wins over everything else
    sl_candle = candle(9, 55, 104, 105, 103, 104)
    decision = exit_engine.check(trade, {"candle": sl_candle, "ltp": 10.0,
                                         "vix_spike": True, "now": now})
    assert decision[0] == "SL_SPOT"
    # b) premium SL (50 * 0.75 = 37.5)
    decision = exit_engine.check(trade, {"candle": None, "ltp": 37.0,
                                         "vix_spike": False, "now": now})
    assert decision[0] == "SL_PREM"
    # c) thesis dead: close back inside ORB high
    inside = candle(9, 55, 111, 113, 110, 111)
    decision = exit_engine.check(trade, {"candle": inside, "ltp": 49.0,
                                         "vix_spike": False, "now": now})
    assert decision[0] == "THESIS_DEAD"
    # f) time stop after 75 min without +1R
    late = now + timedelta(minutes=76)
    decision = exit_engine.check(trade, {"candle": None, "ltp": 49.0,
                                         "vix_spike": False, "now": late})
    assert decision[0] == "TIME_STOP"
    # no trigger when nothing applies (25 min elapsed, spot above stop)
    assert exit_engine.check(trade, {"candle": candle(9, 55, 113, 116, 112, 115),
                                     "ltp": 49.0, "vix_spike": False,
                                     "now": now}) is None


def test_runner_rearm_and_blended_accounting(tmp_path):
    store = make_store(tmp_path)
    exit_engine = ExitEngine(store)
    now = ist_now()
    trade = make_trade()
    # 1R touched -> runner armed, half booked at 60
    trade.update_extreme(110.0)
    assert not trade.hit_tgt1()
    trade.update_extreme(110.5)  # tgt1 = 115 + 10 = 125... use closer numbers:
    trade2 = make_trade(entry_spot=100.0, sl_spot=90.0)   # 1R = 10, tgt1 110, tgt2 120
    trade2.entry_prem = 50.0
    trade2.extreme_spot = 100.0          # reset the make_trade default
    trade2.update_extreme(109.0)
    assert not trade2.hit_tgt1()
    trade2.update_extreme(110.2)
    assert trade2.hit_tgt1()
    trade2.runner_armed = True
    trade2.booked_prem = 60.0
    # runner stop = breakeven: candle close back to entry (99.9 < 100)
    decision = exit_engine.check(trade2, {"candle": candle(10, 30, 100, 100.5, 99, 99.9),
                                          "ltp": 55.0, "vix_spike": False, "now": now})
    assert decision[0] == "SL_SPOT" and "breakeven" in decision[1]
    # runner premium floor = entry premium
    decision = exit_engine.check(trade2, {"candle": None, "ltp": 49.5,
                                          "vix_spike": False, "now": now})
    assert decision[0] == "SL_PREM"
    # +2R touch exits the runner
    trade2.update_extreme(120.1)
    decision = exit_engine.check(trade2, {"candle": None, "ltp": 70.0,
                                          "vix_spike": False, "now": now})
    assert decision[0] == "TGT_2R"
    # blended accounting: half booked at 60, runner exited at 70
    assert trade2.blended_points(70.0) == pytest.approx(0.5 * 10 + 0.5 * 20)
    assert trade2.result_r(70.0) == pytest.approx(15.0 / (0.25 * 50))
    assert trade2.pnl_rupees(70.0) == pytest.approx(15.0 * 75)
    # time stop cancelled once runner is armed (ltp above the breakeven floor)
    late = now + timedelta(minutes=200)
    trade3 = make_trade(entry_spot=100.0, sl_spot=90.0)
    trade3.runner_armed = True
    assert exit_engine.check(trade3, {"candle": None, "ltp": 55.0,
                                      "vix_spike": False, "now": late}) is None


def test_short_side_mirror(tmp_path):
    store = make_store(tmp_path)
    exit_engine = ExitEngine(store)
    trade = make_trade(direction="SHORT", strike=85, entry_spot=100.0, sl_spot=110.0,
                       broken_level=88.0, level_name="ORB low")
    trade.extreme_spot = 100.0
    # SHORT: stop is ABOVE spot (ORB mid 110), tgt1 = 90 below
    assert trade.tgt1_spot == 90.0 and trade.tgt2_spot == 80.0
    assert trade.effective_stop_spot == 110.0
    trade.update_extreme(89.5)                 # touched TGT 1 (<= 90)
    assert trade.hit_tgt1()
    decision = exit_engine.check(trade, {"candle": candle(10, 0, 111, 112, 110, 111),
                                         "ltp": 49.0, "vix_spike": False,
                                         "now": TODAY.replace(hour=10, minute=5)})
    assert decision[0] == "SL_SPOT"  # close 111 > 110 = beyond stop


# --- OI window -----------------------------------------------------------------------

def test_recent_oi_net_and_classifier(tmp_path):
    store = make_store(tmp_path)
    conn = store.conn
    for sid in (1, 2, 3):
        conn.execute("INSERT INTO oi_snapshots (ts) VALUES (?)",
                     (f"2026-10-05T09:{sid:02d}+05:30",))
        for strike in (24950, 25000):
            values = {1: 1000, 2: 900, 3: 800}[sid] if strike == 24950 else \
                     {1: 500, 2: 700, 3: 900}[sid]
            conn.execute(
                "INSERT INTO oi_strike_snapshots (snapshot_id, ts, expiry, strike, "
                "ce_change_oi) VALUES (?, ?, ?, ?, ?)",
                (sid, f"2026-10-05T09:{sid:02d}", "06-Oct-2026", strike, values))
    conn.commit()
    net = recent_oi_net(conn, "06-Oct-2026", [24950, 25000], "ce")
    assert net[24950] == pytest.approx(-200)   # falling -> writers covering
    assert net[25000] == pytest.approx(400)    # rising -> fresh wall
    assert classify_oi_window(net) == "REJECT"  # any rising wins (conservative)
    assert classify_oi_window({24950: -200}) == "PASS"
    assert classify_oi_window({}) == "UNKNOWN"


# --- trade crash recovery --------------------------------------------------------------

def test_tradebook_recovery_with_runner_state(tmp_path):
    store = make_store(tmp_path)
    trade = make_trade(entry_spot=100.0, sl_spot=90.0)
    store.journal_event("SIGNAL", direction=trade.direction, strike=trade.strike,
                        spot=trade.entry_spot, option_ltp=trade.entry_prem,
                        score=trade.score, reasons=trade.to_reasons(),
                        trade_id=trade.trade_id, notes="signal candle LONG x")
    store.journal_event("SKIP", trade_id=trade.trade_id, option_ltp=60.0,
                        reasons={"event": "1R", "booked_prem": 60.0},
                        notes="1R reached: BOOK 50%")

    from main import TradeBook
    book = TradeBook(store, ist_now().date().isoformat())
    book.load()
    assert len(book.trades) == 1
    recovered = book.trades[0]
    assert recovered.strike == trade.strike and recovered.entry_prem == 50.0
    assert recovered.runner_armed and recovered.booked_prem == 60.0
    assert recovered.effective_stop_spot == 100.0  # breakeven, not ORB mid


# --- trade card format ------------------------------------------------------------------

def test_trade_card_exact_lines():
    from alerts import messages
    trade = make_trade(entry_spot=115.0, sl_spot=105.0, entry_prem=52.0,
                       candle_start=TODAY.replace(hour=9, minute=35))
    card = messages.trade_card(signal_no=1, direction="LONG", trade=trade,
                               spot=115.0, now_txt="09:35:07", data_source="live-chain")
    assert "🎯 SIGNAL #1 [BREAKOUT] — BUY CE 115 (weekly expiry 06-Oct-2026)" in card
    assert "ENTRY: BUY 115 at ₹52.00–₹62.00 (1 lot = 75 qty)" in card
    assert "SL spot: 105.00 — candle CLOSE beyond = EXIT" in card
    assert "SL prem: ₹39.00" in card
    assert "TGT 1: spot 125.00 → prem ~₹57.00 → BOOK 50%, SL to entry" in card
    assert "TGT 2: spot 135.00 → prem ~₹62.00 → EXIT rest" in card
    assert "MAX RISK: ₹975 | Exit by 15:10" in card
    assert "candle close 09:40" in card


# --- parser still accepts the v3 row shape (regression guard) -----------------------------

def test_parser_v3_shape(tmp_path):
    import copy, json
    from pathlib import Path
    fixture = json.loads((Path(__file__).parent / "fixtures" / "nse_chain_sample.json")
                         .read_text(encoding="utf-8"))
    for entry in fixture["records"]["data"]:
        entry["expiryDates"] = entry.pop("expiryDate")
        for side in ("CE", "PE"):
            if side in entry:
                entry[side]["expiryDate"] = "06-10-2026"
    snap = parse_option_chain(fixture, today=datetime(2026, 10, 5).date())
    assert len(snap.rows) == 8
    assert snap.rows[0].expiry == "06-Oct-2026"
