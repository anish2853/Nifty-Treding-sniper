from datetime import datetime, timedelta

import settings
from engines.candles import Candle
from journal.decision_audit import build_nearmiss_report
from journal.store import JournalStore
from utils import ist_now


def test_audit_report_lists_persisted_skips_and_level_crosses(tmp_path):
    store = JournalStore(db_path=tmp_path / "audit.sqlite3")
    today = ist_now().date()
    recent_start = datetime.combine(today, settings.ENTRY_EVAL_START, tzinfo=settings.IST)
    candles = [
        Candle(recent_start, 100, 105, 98, 103),
        Candle(recent_start + timedelta(minutes=5), 103, 104, 95, 97),
    ]
    store.upsert_candles(candles)
    store.journal_event("SKIP", direction="SHORT", spot=97, score=85,
                        family="LIQ-BREAK", notes="BREAK INTO WALL: put writers +103k")

    report = build_nearmiss_report(store, start_time=settings.ENTRY_EVAL_START,
                                   log_path=tmp_path / "missing.log")

    assert "## Liveness" in report
    assert "## State transitions" in report
    assert "SKIP LIQ-BREAK SHORT score 85" in report
    assert "BREAK INTO WALL" in report
    assert "## Derived 5m level crossings" in report
    store.close()