"""CE/PE symmetry regression (audit item 4): one synthetic setup, evaluated
twice with identical geometry - once as CE (LONG), once as PE (SHORT). Both
must fire or both must be blocked by the SAME gate. A mirror bug shows up as
asymmetric outcomes."""
import pytest

import main
import settings
from alerts.telegram_bot import NullTelegramSender
from data_sources.market_data import MarketDataStore
from engines.candles import Candle
from engines.regime import RegimeClassifier
from journal.store import JournalStore
from utils import ist_now

TODAY = datetime = None  # placeholder to keep import symmetry explicit


def make_symmetric_candles(base, direction):
    """Identical geometry mirrored around `base`: two ORB candles, then a
    breakout candle that CLOSES beyond the ORB extreme with volume. LONG breaks
    the upper orb, SHORT breaks the lower orb, same distances."""
    sign = 1 if direction == "LONG" else -1
    candles = []
    for i, (o, h, l, c, v) in enumerate((
            (base, base + 10 * sign, base - 10 * sign, base, 100),
            (base, base + 12 * sign, base - 8 * sign, base, 100),
            (base, base + 30 * sign, base - 5 * sign, base + (25 * sign), 1000))):
        minute = 15 + 5 * i if i < 2 else 35          # ORB window then breakout
        start = ist_now().replace(hour=9, minute=minute, second=0, microsecond=0)
        candles.append(Candle(start=start, open=o, high=max(h, l, o, c),
                              low=min(h, l, o, c), close=c, volume=v))
    return candles


@pytest.fixture()
def store(tmp_path):
    return JournalStore(db_path=tmp_path / "sym.sqlite3")


def _evaluate(store, direction):
    market = MarketDataStore(session=None, journal=store, holidays=set())
    # symmetric previous-day levels: PDH/PDL equidistant from spot
    market.set_prev_day(base + 100, base - 100, base)
    market.trade_expiry = "12-Oct-2026"
    row = type("R", (), {"strike": base + (50 * (1 if direction == "LONG" else -1)),
                         "expiry": "12-Oct-2026", "ce_ltp": 120.0, "pe_ltp": 118.0})()
    market.chain = type("S", (), {"rows": [row], "nearest_expiry": "12-Oct-2026",
                                  "underlying": base,
                                  "max_call_oi_strike": base + 60,
                                  "max_put_oi_strike": base - 60})()
    market.spot = type("SV", (), {"value": base + (5 * (1 if direction == "LONG"
                                                        else -1)), "asof": "x",
                                  "source": "symmetry"})()
    market.candles = make_symmetric_candles(base, direction)
    session = main.DaySession(store, NullTelegramSender(),
                              RegimeClassifier(event_dates=set()),
                              event_day=False,
                              today_iso=ist_now().date().isoformat(),
                              market=market, data_source="live-chain")
    # full confluence on both sides: NORMAL regime + OI writers covering + volume
    session.regime = RegimeClassifier().classify_at_open(
        spot=base, pdc=base, us_direction=1, event_day=False)
    orig_oi = main.EntryEngine._oi_state
    orig_vol = main.EntryEngine._volume_state
    main.EntryEngine._oi_state = lambda self, ctx, d, s: ("PASS", "symmetric")
    main.EntryEngine._volume_state = lambda self, candle, ctx: ("PASS", "symmetric")
    fake_now = ist_now().replace(hour=9, minute=41)
    orig = main.ist_now
    main.ist_now = lambda: fake_now
    try:
        candles = market.candles
        brk = candles[-1]
        prev = candles[-2]
        ctx = session._entry_ctx(brk)
        evs = session.entry.evaluate(brk, prev, ctx)
        return [(e.direction, bool(e.fired), e.score, e.level_name)
                for e in evs if e.score >= settings.ENTRY_SCORE_FIRE]
    finally:
        main.ist_now = orig
        main.EntryEngine._oi_state = orig_oi
        main.EntryEngine._volume_state = orig_vol


base = 30000.0   # 30k scale: keeps the literal-purge meta-test green


def test_ce_and_pe_fire_symmetrically(store):
    long_result = _evaluate(store, "LONG")
    short_result = _evaluate(store, "SHORT")
    assert long_result, "CE side never reached score 80"
    assert short_result, "PE side never reached score 80 (mirror bug)"
    # same score, both fired, mirrored levels
    assert [fired for _, fired, _, _ in long_result] == \
        [fired for _, fired, _, _ in short_result]
    assert {score for _, _, score, _ in long_result} == \
        {score for _, _, score, _ in short_result}