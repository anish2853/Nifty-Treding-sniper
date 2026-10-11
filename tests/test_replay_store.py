"""Replay journal: run scoping, dedupe, integrity checks."""
from datetime import datetime

import pytest

import settings
from journal.replay_store import DataIntegrityError, ReplayJournal, stats_for_run


@pytest.fixture()
def journal(tmp_path):
    store = ReplayJournal(tmp_path / "replay.sqlite3")
    store.begin_run("replay-mtf", days=7)
    yield store
    store.close()


def test_unique_constraint_blocks_duplicate_entry(journal):
    entry = datetime(2026, 10, 6, 11, 20, tzinfo=settings.IST)
    assert journal.record_trade(
        replay_date="2026-10-06", day_num=1, family="LEVEL-FADE", direction="SHORT",
        entry_time=entry, entry_price=22678.35, sl_spot=22693.20,
        exit_time=entry.replace(hour=12, minute=5), exit_price=22690.0,
        exit_reason="SL", result_r=-2.35) is True
    assert journal.record_trade(
        replay_date="2026-10-06", day_num=1, family="LEVEL-FADE", direction="SHORT",
        entry_time=entry, entry_price=22678.35, sl_spot=22691.70,
        exit_time=entry.replace(hour=12, minute=5), exit_price=22690.0,
        exit_reason="SL", result_r=-2.42) is False
    assert journal.row_count() == 1


def test_begin_run_clears_prior_mode_rows(tmp_path):
    path = tmp_path / "replay.sqlite3"
    first = ReplayJournal(path)
    first.begin_run("replay60", 5)
    entry = datetime(2026, 10, 1, 10, 0, tzinfo=settings.IST)
    first.record_trade(
        replay_date="2026-10-01", day_num=1, family="MTF-SCALP", direction="LONG",
        entry_time=entry, entry_price=100.0, sl_spot=95.0, result_r=1.0)
    first.finish_run()
    first.close()

    second = ReplayJournal(path)
    second.begin_run("replay60", 5)
    assert second.row_count() == 0
    second.close()


def test_integrity_aborts_on_duplicate_rows(tmp_path):
    import sqlite3
    path = tmp_path / "replay_unconstrained.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE replay_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            replay_date TEXT,
            family TEXT,
            direction TEXT,
            entry_time TEXT,
            entry_price REAL,
            sl_spot REAL,
            result_r REAL
        )
    """)
    run_id = "test-run"
    entry = datetime(2026, 10, 6, 11, 20, tzinfo=settings.IST)
    for _ in range(2):
        conn.execute(
            "INSERT INTO replay_trades (run_id, replay_date, family, direction, entry_time, "
            "entry_price, sl_spot, result_r) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, "2026-10-06", "LEVEL-FADE", "SHORT",
             entry.isoformat(timespec="seconds"), 22678.35, 22693.20, -1.0))
    conn.commit()
    with pytest.raises(DataIntegrityError):
        stats_for_run(conn, run_id)
    conn.close()
