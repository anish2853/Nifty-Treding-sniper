"""Isolated replay journal: run-scoped trades, hard dedupe, integrity checks.

Live paper journal (journal.sqlite3) is never touched. Each replay batch gets a
unique run_id; stats and exports filter to ONE run_id only.
"""
from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import settings
from utils import ist_now

log = logging.getLogger(__name__)

REPLAY_DB_PATH = settings.DATA_DIR / "replay_journal.sqlite3"


class DataIntegrityError(RuntimeError):
    """Raised when journal row count != unique trade count for a run."""


@dataclass
class ReplayRun:
    run_id: str
    mode: str
    days: int
    started_at: str


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or REPLAY_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='replay_trades'")
    if cursor.fetchone() is not None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(replay_trades)").fetchall()}
        if "run_id" not in cols or "sl_spot" not in cols:
            conn.execute("DROP TABLE IF EXISTS replay_trades_legacy")
            conn.execute("ALTER TABLE replay_trades RENAME TO replay_trades_legacy")
            log.warning("Archived legacy replay_trades table to replay_trades_legacy")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS replay_runs (
            run_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            days INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS replay_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            replay_date TEXT NOT NULL,
            day_num INTEGER,
            family TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            entry_price REAL NOT NULL,
            sl_spot REAL NOT NULL DEFAULT 0,
            sl_prem REAL,
            exit_time TEXT,
            exit_price REAL,
            exit_reason TEXT,
            result_r REAL,
            day_state TEXT,
            oi_available INTEGER,
            zone_id TEXT,
            notes TEXT,
            UNIQUE(run_id, family, direction, entry_time, entry_price)
        );
        CREATE INDEX IF NOT EXISTS idx_replay_trades_run ON replay_trades(run_id);
        CREATE INDEX IF NOT EXISTS idx_replay_trades_date ON replay_trades(replay_date);
    """)
    for column, col_type in (("sl_spot", "REAL"), ("sl_prem", "REAL"),
                             ("zone_id", "TEXT"), ("run_id", "TEXT"),
                             ("risk_units", "REAL")):
        try:
            conn.execute(f"ALTER TABLE replay_trades ADD COLUMN {column} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_replay_trade_dedupe "
        "ON replay_trades(run_id, family, direction, entry_time, entry_price)")
    conn.commit()


def make_run_id(mode: str, days: int, when: datetime | None = None) -> str:
    stamp = (when or ist_now()).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{mode}-d{days}"


class ReplayJournal:
    """Write path for replay modes. Clears prior rows for the same mode on begin."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or REPLAY_DB_PATH)
        self.conn = _connect(self.db_path)
        init_schema(self.conn)
        self.run: ReplayRun | None = None
        self._fired_keys: set[tuple] = set()

    def begin_run(self, mode: str, days: int) -> str:
        run_id = make_run_id(mode, days)
        self.conn.execute(
            "DELETE FROM replay_trades WHERE run_id IN "
            "(SELECT run_id FROM replay_runs WHERE mode = ?)", (mode,))
        self.conn.execute("DELETE FROM replay_trades WHERE run_id IS NULL OR run_id = ''")
        self.conn.execute("DELETE FROM replay_runs WHERE mode = ?", (mode,))
        started = ist_now().isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO replay_runs (run_id, mode, days, started_at) VALUES (?, ?, ?, ?)",
            (run_id, mode, days, started))
        self.conn.commit()
        self.run = ReplayRun(run_id=run_id, mode=mode, days=days, started_at=started)
        self._fired_keys.clear()
        log.info("Replay run %s started (cleared prior %s rows)", run_id, mode)
        return run_id

    def finish_run(self) -> None:
        if self.run is None:
            return
        self.conn.execute(
            "UPDATE replay_runs SET finished_at = ? WHERE run_id = ?",
            (ist_now().isoformat(timespec="seconds"), self.run.run_id))
        self.conn.commit()
        verify_run_integrity(self.conn, self.run.run_id)

    @property
    def run_id(self) -> str | None:
        return self.run.run_id if self.run else None

    def trade_key(self, family: str, direction: str, entry_time: datetime,
                  entry_price: float) -> tuple:
        if isinstance(entry_time, datetime):
            minute = entry_time.replace(second=0, microsecond=0).isoformat(timespec="minutes")
        else:
            minute = str(entry_time)[:16]
        return (family, direction, minute, round(float(entry_price), 2))

    def already_fired(self, family: str, direction: str, entry_time: datetime,
                      entry_price: float) -> bool:
        key = self.trade_key(family, direction, entry_time, entry_price)
        return key in self._fired_keys

    def record_trade(self, *, replay_date: str, day_num: int, family: str,
                     direction: str, entry_time: datetime, entry_price: float,
                     sl_spot: float, sl_prem: float | None = None,
                     exit_time: datetime | None = None, exit_price: float | None = None,
                     exit_reason: str = "", result_r: float | None = None,
                     day_state: str | None = None, oi_available: bool = True,
                     zone_id: str | None = None, notes: str = "",
                     risk_units: float = 1.0) -> bool:
        """Insert one trade; return False if dedupe blocked (memory or DB)."""
        if self.run is None:
            raise RuntimeError("call begin_run() before record_trade()")
        key = self.trade_key(family, direction, entry_time, entry_price)
        if key in self._fired_keys:
            log.debug("Memory dedupe blocked %s %s @ %s", family, direction, entry_time)
            return False

        if sl_prem is None:
            sl_prem = round(100.0 * settings.SL_PREMIUM_FRACTION, 2)

        entry_time_str = (entry_time.isoformat(timespec="seconds")
                          if isinstance(entry_time, datetime) else str(entry_time))
        exit_time_str = (exit_time.isoformat(timespec="seconds")
                         if isinstance(exit_time, datetime) else (str(exit_time) if exit_time else None))
        entry_price_val = round(float(entry_price), 2)
        sl_spot_val = round(float(sl_spot), 2)
        exit_price_val = round(float(exit_price), 2) if exit_price is not None else None
        result_r_val = round(float(result_r), 4) if result_r is not None else None

        try:
            self.conn.execute(
                "INSERT INTO replay_trades "
                "(run_id, replay_date, day_num, family, direction, entry_time, entry_price, "
                "sl_spot, sl_prem, exit_time, exit_price, exit_reason, result_r, day_state, "
                "oi_available, zone_id, notes, risk_units) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.run.run_id, replay_date, day_num, family, direction,
                    entry_time_str, entry_price_val, sl_spot_val, sl_prem,
                    exit_time_str, exit_price_val, exit_reason or None, result_r_val,
                    day_state, 1 if oi_available else 0, zone_id, notes or None,
                    float(risk_units),
                ))
            self.conn.commit()
        except sqlite3.IntegrityError:
            log.debug("DB dedupe blocked %s %s @ %s", family, direction, entry_time)
            return False
        self._fired_keys.add(key)
        return True

    def row_count(self) -> int:
        if self.run is None:
            return 0
        return self.conn.execute(
            "SELECT COUNT(*) FROM replay_trades WHERE run_id = ?",
            (self.run.run_id,)).fetchone()[0]

    def export_csv(self, output_dir: Path | None = None) -> Path:
        if self.run is None:
            raise RuntimeError("no active run")
        verify_run_integrity(self.conn, self.run.run_id)
        out_dir = output_dir or settings.EXPORTS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"replay_{self.run.run_id}.csv"
        rows = fetch_trades(self.conn, self.run.run_id)
        columns = [
            "run_id", "date", "time", "family", "direction", "entry_price",
            "sl_spot", "sl_prem", "exit_time", "exit_price", "exit_reason", "result_r",
            "day_state", "zone_id", "notes",
        ]
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for r in rows:
                entry_dt = r["entry_time"]
                time_str = entry_dt[11:16] if entry_dt and len(entry_dt) >= 16 else (entry_dt or "—")
                exit_dt = r["exit_time"]
                exit_time_str = exit_dt[11:16] if exit_dt and len(exit_dt) >= 16 else (exit_dt or "—")
                writer.writerow({
                    "run_id": r["run_id"],
                    "date": r["replay_date"],
                    "time": time_str,
                    "family": r["family"],
                    "direction": r["direction"],
                    "entry_price": f"{r['entry_price']:.2f}",
                    "sl_spot": f"{r['sl_spot']:.2f}" if r["sl_spot"] is not None else "0.00",
                    "sl_prem": f"{r['sl_prem']:.2f}" if r["sl_prem"] is not None else "—",
                    "exit_time": exit_time_str,
                    "exit_price": f"{r['exit_price']:.2f}" if r["exit_price"] is not None else "—",
                    "exit_reason": r["exit_reason"] or "—",
                    "result_r": f"{r['result_r']:+.2f}" if r["result_r"] is not None else "—",
                    "day_state": r["day_state"] or "",
                    "zone_id": r["zone_id"] or "",
                    "notes": r["notes"] or "",
                })
        log.info("Exported %d trades to %s", len(rows), path)
        return path

    def close(self) -> None:
        self.conn.close()


def latest_run_id(conn: sqlite3.Connection, mode: str | None = None) -> str | None:
    query = "SELECT run_id FROM replay_runs"
    params: list = []
    if mode:
        query += " WHERE mode = ?"
        params.append(mode)
    query += " ORDER BY started_at DESC LIMIT 1"
    row = conn.execute(query, params).fetchone()
    return row["run_id"] if row else None


def fetch_trades(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM replay_trades WHERE run_id = ? "
        "ORDER BY replay_date, entry_time, id",
        (run_id,))
    cols = [col[0] for col in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def unique_trade_count(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT DISTINCT family, direction, entry_time, entry_price "
        "  FROM replay_trades WHERE run_id = ?"
        ")", (run_id,)).fetchone()
    return int(row[0])


def verify_run_integrity(conn: sqlite3.Connection, run_id: str) -> dict:
    """Abort stats if raw rows != unique trades."""
    total = conn.execute(
        "SELECT COUNT(*) FROM replay_trades WHERE run_id = ?", (run_id,)).fetchone()[0]
    unique = unique_trade_count(conn, run_id)
    if total != unique:
        raise DataIntegrityError(
            f"run {run_id}: journal rows={total} != unique trades={unique}")
    return {"run_id": run_id, "rows": total, "unique": unique}


def stats_for_run(conn: sqlite3.Connection, run_id: str) -> dict:
    verify_run_integrity(conn, run_id)
    trades = fetch_trades(conn, run_id)
    if not trades:
        return {"run_id": run_id, "trades_unique": 0, "rows": 0, "win_rate": "—",
                "avg_r": 0.0, "total_r": 0.0, "wins": 0, "losses": 0, "max_dd": 0.0,
                "by_family": {}}
    results = [float(t["result_r"] or 0.0) for t in trades]
    wins = sum(1 for r in results if r > 0)
    losses = sum(1 for r in results if r < 0)
    total_r = sum(results)
    running = 0.0
    max_dd = 0.0
    for r in results:
        running += r
        max_dd = min(max_dd, running)
    return {
        "run_id": run_id,
        "trades_unique": len(trades),
        "rows": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": f"{100 * wins / len(trades):.1f}%",
        "avg_r": total_r / len(trades),
        "total_r": total_r,
        "max_dd": max_dd,
        "by_family": _split_by(trades, "family"),
    }


def _split_by(trades: Iterable[dict], field: str) -> dict[str, dict]:
    buckets: dict[str, list[float]] = {}
    for trade in trades:
        key = trade.get(field) or "UNKNOWN"
        buckets.setdefault(key, []).append(float(trade.get("result_r") or 0.0))
    out = {}
    for key, values in buckets.items():
        wins = sum(1 for v in values if v > 0)
        out[key] = {
            "count": len(values),
            "win_rate": f"{100 * wins / len(values):.1f}%" if values else "—",
            "avg_r": sum(values) / len(values) if values else 0.0,
            "total_r": sum(values),
        }
    return out


def print_run_summary(conn: sqlite3.Connection, run_id: str) -> None:
    summary = stats_for_run(conn, run_id)
    print(f"run_id: {summary['run_id']}")
    print(f"trades (unique): {summary['trades_unique']}")
    print(f"win rate: {summary['win_rate']} | avg R {summary['avg_r']:+.2f} | "
          f"total R {summary['total_r']:+.2f} | max DD {summary['max_dd']:+.2f}R")
    for family, fam in sorted(summary.get("by_family", {}).items()):
        print(f"  [{family}] n={fam['count']} WR {fam['win_rate']} "
              f"exp {fam['avg_r']:+.2f}R | total {fam['total_r']:+.2f}R")
