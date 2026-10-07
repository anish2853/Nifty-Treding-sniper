from datetime import timedelta
from types import SimpleNamespace

import main
from engines.level_decision import LevelDecisionEngine
from engines.level_grid import GridLevel
from engines.liquidity import LiquidityMap, OILevel
from engines.level_runtime import LevelRuntime
from journal.store import JournalStore
from utils import ist_now


def test_range_width_uses_nearest_strong_bracketing_levels():
    runtime = LevelRuntime()
    runtime.grid = [
        GridLevel("S1", "S1", 90, "S", ("OI",), strong=True),
        GridLevel("S2", "S2", 80, "S", ("OI",), strong=True),
        GridLevel("R1", "R1", 125, "R", ("OI",), strong=False),
        GridLevel("R2", "R2", 140, "R", ("OI",), strong=True),
    ]

    assert runtime.range_width(100) == 50
    distance, nearest = runtime.runway(100, "LONG")
    assert distance == 25 and nearest.label == "R1"


def test_watch_bias_uses_market_store_liquidity_snapshot():
    runtime = LevelRuntime()
    market = SimpleNamespace(
        spot_value=lambda: 100.0, pdh=None, pdl=None, pdc=None, candles=[],
        liquidity=LiquidityMap(None, 100.0, [
            OILevel("S", 1, 95, 300000, 8000, 5, 5.0, False),
        ]))
    day_state = SimpleNamespace(vwap=100.0)

    events = runtime.refresh_grid(market, day_state)

    assert len(events) == 1
    assert "put writers +8k below → HOLD bias" in events[0].message


def test_session_journals_and_sends_each_engine_authorized_watch(tmp_path):
    store = JournalStore(db_path=tmp_path / "watch.sqlite3")
    sent = []
    watch_engine = LevelDecisionEngine()
    watched_level = GridLevel("SWING-L", "SWING-L", 100, "R", ("SWING-L",))
    now = ist_now()
    first = watch_engine.approach_events(82, [watched_level], now=now)[0]
    watch_engine.approach_events(130, [watched_level], now=now + timedelta(minutes=5))
    watch_engine.approach_events(90, [watched_level], now=now + timedelta(minutes=20))
    second = watch_engine.approach_events(
        90, [watched_level], now=now + timedelta(minutes=31))[0]
    assert first.level_id != second.level_id
    session = SimpleNamespace(
        level_runtime=SimpleNamespace(zones={}), store=store,
        today_iso=ist_now().date().isoformat(),
        market=SimpleNamespace(spot_value=lambda: 100.0),
        day_state=None, event_day=False, _notify=sent.append)

    main.DaySession._handle_level_event(session, first)
    main.DaySession._handle_level_event(session, second)

    count = store.conn.execute(
        "SELECT COUNT(*) FROM journal WHERE type = 'SKIP' "
        "AND notes LIKE 'LEVEL-WATCH RESISTANCE ZONE:%'").fetchone()[0]
    assert count == 2
    assert len(sent) == 2
    store.close()