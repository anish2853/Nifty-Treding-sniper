"""Phase B+ tests: liquidity map, sweep-fade scoring, family stats, the VIX
wiring fix and the test-mode warning."""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
import settings
from alerts.telegram_bot import NullTelegramSender
from data_sources.market_data import MarketDataStore
from data_sources.nse_chain import parse_option_chain
from engines.candles import Candle
from engines.liquidity import compute_liquidity_map
from engines.regime import RegimeClassifier
from engines.sweep import SweepEngine
from journal.store import JournalStore
from utils import ist_now

TODAY = datetime(2026, 10, 5, tzinfo=settings.IST)
FIXTURE = Path(__file__).parent / "fixtures" / "nse_chain_sample.json"


@pytest.fixture()
def store(tmp_path):
    return JournalStore(db_path=tmp_path / "t.sqlite3")


def candle(hh, mm, o, h, l, c, volume=None):
    return Candle(start=TODAY.replace(hour=hh, minute=mm, second=0, microsecond=0),
                  open=o, high=h, low=l, close=c, volume=volume)


def make_lmap(pdh=112.0, pdl=99.0, spot=105.0):
    """Synthetic nearest-expiry rows: put walls 104/100/95, call walls 106/112/120."""
    rows = [
        SimpleNamespace(strike=104, ce_oi=None, pe_oi=500000, ce_change_oi=None,
                        pe_change_oi=15000, expiry="06-Oct-2026"),
        SimpleNamespace(strike=100, ce_oi=None, pe_oi=800000, ce_change_oi=None,
                        pe_change_oi=-20000, expiry="06-Oct-2026"),
        SimpleNamespace(strike=95, ce_oi=None, pe_oi=300000, ce_change_oi=None,
                        pe_change_oi=0, expiry="06-Oct-2026"),
        SimpleNamespace(strike=106, ce_oi=250000, pe_oi=None, ce_change_oi=5000,
                        pe_change_oi=None, expiry="06-Oct-2026"),
        SimpleNamespace(strike=112, ce_oi=600000, pe_oi=None, ce_change_oi=45000,
                        pe_change_oi=None, expiry="06-Oct-2026"),   # at PDH -> STRONG
        SimpleNamespace(strike=120, ce_oi=150000, pe_oi=None, ce_change_oi=-8000,
                        pe_change_oi=None, expiry="06-Oct-2026"),
        SimpleNamespace(strike=100, ce_oi=999999, pe_oi=None, ce_change_oi=0,
                        pe_change_oi=None, expiry="13-Oct-2026"),   # other expiry: out
    ]
    return compute_liquidity_map(rows, spot=spot, nearest_expiry="06-Oct-2026",
                                 pdh=pdh, pdl=pdl, ts="05-Oct-2026 09:15:00")


def test_liquidity_map_ladder_strong_and_pools():
    lmap = make_lmap()
    s = [lv for lv in lmap.levels if lv.side == "S"]
    r = [lv for lv in lmap.levels if lv.side == "R"]
    assert [lv.strike for lv in s] == [100, 104, 95]      # by PUT OI desc
    assert [lv.strike for lv in r] == [112, 106, 120]     # by CALL OI desc
    assert s[0].label == "S1" and r[0].label == "R1"
    # distances from spot (positive): S below, R above
    assert s[0].distance_pts == 5.0 and r[0].distance_pts == 7.0
    # STRONG: the 112 call wall sits exactly at PDH 112 (0% <= 0.15%)
    assert r[0].strong is True and r[1].strong is False
    # the 100 put wall is exactly at PDL 99? no (distance 1 > 0.1485 band) ->
    assert s[0].strong is False
    # arrows follow change-in-OI sign (S ranking: 100 by OI, then 104, then 95)
    assert s[0].arrow == "↓" and s[1].arrow == "↑" and s[2].arrow == "→"
    # pools carried with pct distance
    assert ("PDH", 112.0, pytest.approx((112 - 105) / 105 * 100)) in lmap.pools
    text = "\n".join(lmap.ladder_lines())
    assert "LIQUIDITY MAP" in text and "POOL PDH" in text and "STRONG" in text
    assert "↑" in text and "↓" in text


def test_liquidity_map_strong_band_pdl():
    # 99 put wall exactly at PDL 99 -> STRONG (within the 0.15% band)
    rows = [SimpleNamespace(strike=99, ce_oi=None, pe_oi=700000, ce_change_oi=None,
                            pe_change_oi=1000, expiry="06-Oct-2026"),
            SimpleNamespace(strike=110, ce_oi=400000, pe_oi=None, ce_change_oi=0,
                            pe_change_oi=None, expiry="06-Oct-2026")]
    lmap = compute_liquidity_map(rows, spot=105.0, nearest_expiry="06-Oct-2026",
                                 pdh=111.0, pdl=99.0)
    s1 = next(lv for lv in lmap.levels if lv.side == "S")
    assert s1.strong is True


def make_sweep_ctx(lmap, **overrides):
    class FakeSnapshot:
        underlying = 105.0
        nearest_expiry = "06-Oct-2026"
        rows = []
    ctx = {
        "liquidity": lmap, "snapshot": FakeSnapshot(), "spot": 105.0,
        "pdh": 112.0, "pdl": 99.0, "candle_engine": None,
        "regime_allows_sweep": lambda d: (True, ""),
        "caps_ok": lambda d: (True, ""),
        "option_ltp": lambda d, s: 100.0,
        "vix_spike": False, "event_day": False, "now": ist_now(),
    }
    ctx.update(overrides)
    return ctx


def test_sweep_up_side_scoring_and_firing(store, monkeypatch):
    engine = SweepEngine(store)
    lmap = make_lmap()
    # UP sweep of PDH pool at 112: wick 113, close 105 back inside; range 13,
    # depth (113-105)=8 -> 61% > 25% -> rejection component passes
    sweep_candle = candle(9, 35, 104, 113, 103, 105)
    ctx = make_sweep_ctx(lmap)
    evs = engine.evaluate(sweep_candle, ctx)
    assert len(evs) == 1
    ev = evs[0]
    assert ev.direction == "SHORT" and ev.sweep_side == "UP"
    assert ev.level_strike == 112.0 and ev.level_label == "PDH pool"
    assert ev.rejection and ev.sweep_extreme == 113.0
    assert ev.sl_spot == 118.0                       # sweep extreme + 5
    # OI unknown (no snapshot rows) + volume missing -> 20+20+15+10 = 65, near-miss
    assert ev.score == 65 and ev.near_miss and not ev.fired

    # OI accelerating at the level -> +20 -> 85 -> FIRED (BUY PE)
    monkeypatch.setattr(SweepEngine, "_oi_accel",
                        lambda self, ctx, level, spot, up: ("PASS", "112 CE +9000"))
    ev2 = engine.evaluate(sweep_candle, ctx)[0]
    assert ev2.score == 85 and ev2.fired and ev2.direction == "SHORT"


def test_sweep_down_side_buys_ce(store, monkeypatch):
    engine = SweepEngine(store)
    lmap = make_lmap()
    # DOWN sweep of PDL pool at 99: wick 98, close 102 back inside;
    # depth (102-98)/(106-98) = 50% > 25%
    sweep_candle = candle(13, 35, 103, 104, 98, 102)
    monkeypatch.setattr(SweepEngine, "_oi_accel",
                        lambda self, ctx, level, spot, up: ("PASS", "100 PE +7000"))
    evs = engine.evaluate(sweep_candle, make_sweep_ctx(lmap))
    down = [ev for ev in evs if ev.sweep_side == "DOWN"]
    assert len(down) == 1
    ev = down[0]
    assert ev.direction == "LONG" and ev.level_strike == 99.0
    assert ev.sl_spot == 93.0                        # sweep extreme - 5
    assert ev.score == 85 and ev.fired


def test_sweep_rejection_fraction_boundary(store):
    engine = SweepEngine(store)
    lmap = make_lmap()
    # depth exactly 25%: high 113, low 101, close 110 -> (113-110)/12 = 25% -> NOT >
    sweep_candle = candle(9, 40, 111, 113, 101, 110)
    ev = engine.evaluate(sweep_candle, make_sweep_ctx(lmap))[0]
    assert not ev.rejection
    assert ev.score == 45 and not ev.fired           # sweep + regime + window only


def test_sweep_windows(store):
    engine = SweepEngine(store)
    lmap = make_lmap()
    # 12:00 -> outside both windows: no evaluation at all
    lunch = candle(12, 0, 104, 113, 103, 105)
    assert engine.evaluate(lunch, make_sweep_ctx(lmap)) == []
    # 13:35 -> second window evaluates; 11:05 -> outside the first window
    assert engine.evaluate(candle(13, 35, 104, 113, 103, 105),
                           make_sweep_ctx(lmap)) != []
    assert engine.evaluate(candle(11, 5, 104, 113, 103, 105),
                           make_sweep_ctx(lmap)) == []


def test_family_tagged_journal_and_stats(store):
    for family, rr in (("BREAKOUT", 0.5), ("BREAKOUT", -1.0), ("SWEEP-FADE", 1.2)):
        store.journal_event("SIGNAL", direction="LONG", strike=100, spot=100,
                            option_ltp=50, score=85, trade_id=f"T{family}{rr}",
                            family=family, reasons={})
        store.journal_event("EXIT", direction="LONG", strike=100, spot=100,
                            option_ltp=55, result_r=rr, exit_reason="test",
                            trade_id=f"T{family}{rr}", pnl_rupees=rr * 100,
                            family=family, notes="exit")
    from journal import stats
    breakout = stats.running_stats(store.conn, family="BREAKOUT")
    sweep = stats.running_stats(store.conn, family="SWEEP-FADE")
    assert breakout["count"] == 2 and breakout["total_r"] == pytest.approx(-0.5)
    assert sweep["count"] == 1 and sweep["total_r"] == pytest.approx(1.2)
    assert stats.running_stats(store.conn)["count"] == 3  # shared totals

    today = ist_now().date().isoformat()
    body = stats.build_eod_body(store, today, "NORMAL", [])
    assert "BY SETUP" in body and "BREAKOUT: 2 trade(s)" in body
    assert "SWEEP-FADE: 1 trade(s)" in body
    # per-family trade lines carry the tag
    assert any("[SWEEP-FADE]" in line for line in body.splitlines())


def test_test_mode_warning_text():
    from alerts import messages
    assert messages.test_mode_warning() == \
        "⚠️ TEST MODE during market hours — use --mode live for monitoring"


def test_snapshot_cycle_wires_live_vix_and_liquidity(store, monkeypatch):
    """Fix #4 integration: the regime classification applies the live VIX
    immediately and the liquidity map is built - all through the store."""
    fixture_raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    monkeypatch.setattr("data_sources.market_data.fetch_option_chain_with_retries",
                        lambda session: (fixture_raw, 200, [("/api/option-chain-v3", 200)]))
    monkeypatch.setattr("data_sources.market_data.india_vix",
                        lambda session: {"value": 13.9, "prev_close": 12.8,
                                         "status": "OK"})

    market = MarketDataStore(session=object(), journal=store, holidays=set())
    market.set_prev_day(25000.0, 24800.0, 24900.0)   # fixture 24950.15 -> +0.2% OK
    session = main.DaySession(store, NullTelegramSender(),
                              RegimeClassifier(event_dates=set()),
                              event_day=False,
                              today_iso=ist_now().date().isoformat(),
                              market=market)
    session.run_cycle()

    assert session.regime is not None
    assert session.regime.vix == pytest.approx(13.9)
    assert session.regime.vix_spike is True          # 13.9/12.8 = +8.6% > 8%
    assert market.liquidity is not None
    assert any(lv.side == "R" for lv in market.liquidity.levels)
    assert market.spot.asof == market.last_refresh   # ONE consistent set
    # the regime journal reasons carry the live VIX for crash recovery
    row = store.conn.execute(
        "SELECT reasons_json FROM journal WHERE type='REGIME' AND "
        "notes LIKE '%classification%'").fetchone()
    assert json.loads(row[0])["vix"] == pytest.approx(13.9)
    # the Telegram choke point transmitted the regime message for the live source
    assert any("REGIME" in m for m in session.tg.sent)


def test_sweep_trade_card_carries_family(tmp_path):
    from alerts import messages
    from engines.paper import PaperTrade
    trade = PaperTrade(trade_id="T01", direction="SHORT", strike=110, expiry="06-Oct-2026",
                       entry_spot=105.0, entry_prem=52.0, sl_spot=118.0,
                       broken_level=112.0, level_name="swept PDH pool",
                       entry_time=TODAY.replace(hour=9, minute=35), score=85,
                       candle_start=TODAY.replace(hour=9, minute=35),
                       family="SWEEP-FADE")
    card = messages.trade_card(signal_no=2, direction="SHORT", trade=trade,
                               spot=105.0, now_txt="09:36:10", family="SWEEP-FADE")
    assert "🎯 SIGNAL #2 [SWEEP-FADE] — BUY PE 110 (weekly expiry 06-Oct-2026)" in card
    assert "SL spot: 118.00 — candle CLOSE beyond = EXIT" in card
