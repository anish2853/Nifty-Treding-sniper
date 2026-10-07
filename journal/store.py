"""Module 6 - SQLite journal + OI snapshot store, with daily CSV export.

Tables:
  journal              - Module 6 schema (SIGNAL/EXIT/REGIME/SKIP events) plus
                         trade_id (links an EXIT to its SIGNAL) and pnl_rupees
                         (Phase B paper accounting fills these; EOD reads them)
  oi_snapshots         - one row per 180 s chain poll (spot, PCR, max-OI strikes, VIX)
  oi_strike_snapshots  - per-strike detail for each snapshot, incl. the snapshot-to-
                         snapshot change-in-OI deltas from the OI engine (what the
                         Phase C replay backtester will read)
"""
import argparse
import csv
import json
import logging
import sqlite3
from datetime import datetime

import settings
from engines.oi_engine import OIDiff
from utils import ist_now, setup_logging

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    direction TEXT,
    strike REAL,
    spot REAL,
    option_ltp REAL,
    score REAL,
    reasons_json TEXT,
    exit_reason TEXT,
    result_r REAL,
    regime TEXT,
    notes TEXT,
    trade_id TEXT,
    pnl_rupees REAL,
    family TEXT,
    day_state TEXT
);
CREATE TABLE IF NOT EXISTS oi_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    underlying REAL,
    chain_ts TEXT,
    nearest_expiry TEXT,
    total_call_oi REAL,
    total_put_oi REAL,
    pcr_total REAL,
    pcr_nearest REAL,
    max_call_oi_strike REAL,
    max_put_oi_strike REAL,
    max_call_oi_strike_nearest REAL,
    max_put_oi_strike_nearest REAL,
    vix REAL,
    vix_prev_close REAL,
    vix_pct_change REAL
);
CREATE TABLE IF NOT EXISTS oi_strike_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES oi_snapshots(id),
    ts TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    ce_oi REAL, ce_change_oi REAL, ce_ltp REAL, ce_iv REAL, ce_volume REAL,
    pe_oi REAL, pe_change_oi REAL, pe_ltp REAL, pe_iv REAL, pe_volume REAL,
    ce_delta_snap REAL, pe_delta_snap REAL
);
CREATE TABLE IF NOT EXISTS candles (
    start TEXT PRIMARY KEY,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(ts);
CREATE INDEX IF NOT EXISTS idx_oi_snap_ts ON oi_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_oi_strike_snap ON oi_strike_snapshots(snapshot_id);
"""


class JournalStore:
    def __init__(self, db_path=None):
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path or settings.DB_PATH)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        # Migration for DBs created before the daily-worker upgrade (Prompt 2):
        for column in ("trade_id TEXT", "pnl_rupees REAL", "family TEXT",
                       "day_state TEXT"):
            try:
                self._conn.execute(f"ALTER TABLE journal ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        """Raw connection for read-only reporting (journal.stats)."""
        return self._conn

    def flush(self):
        """Checkpoint the WAL so every journaled row is inside the main DB file."""
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

    # -- Module 6 journal ---------------------------------------------------
    def journal_event(self, event_type, *, direction=None, strike=None, spot=None,
                      option_ltp=None, score=None, reasons=None, exit_reason=None,
                      result_r=None, regime=None, notes=None, trade_id=None,
                      pnl_rupees=None, family=None, day_state=None):
        ts = ist_now().isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT INTO journal (ts, type, direction, strike, spot, option_ltp, score, "
            "reasons_json, exit_reason, result_r, regime, notes, trade_id, pnl_rupees, "
            "family, day_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, event_type, direction, strike, spot, option_ltp, score,
             json.dumps(reasons, default=str) if reasons is not None else None,
             exit_reason, result_r, regime, notes, trade_id, pnl_rupees, family,
             day_state))
        self._conn.commit()
        log.info("journal: %s | %s", event_type, (regime or notes or "")[:120])

    def has_journal_event(self, iso_date: str, note_contains: str) -> bool:
        """True if a journal row for the date already carries this note fragment -
        the dedup that makes crash-restarts idempotent (no double pre-market/EOD
        messages after a watchdog restart)."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM journal WHERE substr(ts, 1, 10) = ? AND notes LIKE ?",
            (iso_date, f"%{note_contains}%")).fetchone()
        return bool(row[0])

    def latest_regime_state(self, iso_date: str):
        """Reasons dict of the newest live regime entry of the day (excludes the
        pre-market report and EOD rows) so a mid-day crash can rebuild RegimeState
        instead of mis-classifying from the current spot."""
        row = self._conn.execute(
            "SELECT reasons_json FROM journal WHERE substr(ts, 1, 10) = ? "
            "AND type = 'REGIME' AND reasons_json IS NOT NULL "
            "AND notes NOT LIKE 'pre-market%' AND notes NOT LIKE 'EOD%' "
            "ORDER BY id DESC LIMIT 1", (iso_date,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    # -- OI snapshots ----------------------------------------------------------
    def save_snapshot(self, snapshot, diff: OIDiff | None = None,
                      vix_info: dict | None = None) -> int:
        ts = ist_now().isoformat(timespec="seconds")
        vix_info = vix_info or {}
        cursor = self._conn.execute(
            "INSERT INTO oi_snapshots (ts, underlying, chain_ts, nearest_expiry, "
            "total_call_oi, total_put_oi, pcr_total, pcr_nearest, max_call_oi_strike, "
            "max_put_oi_strike, max_call_oi_strike_nearest, max_put_oi_strike_nearest, "
            "vix, vix_prev_close, vix_pct_change) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, snapshot.underlying,
             snapshot.chain_ts.isoformat(timespec="seconds") if snapshot.chain_ts else None,
             snapshot.nearest_expiry, snapshot.total_call_oi, snapshot.total_put_oi,
             snapshot.pcr_total, snapshot.pcr_nearest, snapshot.max_call_oi_strike,
             snapshot.max_put_oi_strike, snapshot.max_call_oi_strike_nearest,
             snapshot.max_put_oi_strike_nearest, vix_info.get("value"),
             vix_info.get("prev_close"), vix_info.get("pct_change")))
        snapshot_id = cursor.lastrowid
        delta_map = {(d.expiry, d.strike): d for d in (diff.deltas if diff else [])}
        rows = []
        for r in snapshot.rows:
            d = delta_map.get((r.expiry, r.strike))
            rows.append((snapshot_id, ts, r.expiry, r.strike,
                         r.ce_oi, r.ce_change_oi, r.ce_ltp, r.ce_iv, r.ce_volume,
                         r.pe_oi, r.pe_change_oi, r.pe_ltp, r.pe_iv, r.pe_volume,
                         d.ce_delta_snap if d else None, d.pe_delta_snap if d else None))
        self._conn.executemany(
            "INSERT INTO oi_strike_snapshots (snapshot_id, ts, expiry, strike, ce_oi, "
            "ce_change_oi, ce_ltp, ce_iv, ce_volume, pe_oi, pe_change_oi, pe_ltp, pe_iv, "
            "pe_volume, ce_delta_snap, pe_delta_snap) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        self._conn.commit()
        return snapshot_id

    def latest_oi_rows(self):
        """(underlying, nearest_expiry, ts, rows) for the newest snapshot; rows are
        lightweight namespaces with the attrs compute_liquidity_map needs."""
        from types import SimpleNamespace
        snap = self._conn.execute(
            "SELECT id, underlying, nearest_expiry, ts FROM oi_snapshots "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if not snap:
            return None, None, None, []
        snapshot_id, underlying, nearest, ts = snap
        rows = [SimpleNamespace(strike=r[0], ce_oi=r[1], pe_oi=r[2],
                                ce_change_oi=r[3], pe_change_oi=r[4], expiry=r[5])
                for r in self._conn.execute(
                    "SELECT strike, ce_oi, pe_oi, ce_change_oi, pe_change_oi, expiry "
                    "FROM oi_strike_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,)).fetchall()]
        return underlying, nearest, ts, rows

    def count_snapshots(self, iso_date: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM oi_snapshots WHERE substr(ts, 1, 10) = ?",
            (iso_date,)).fetchone()
        return row[0]

    # -- candles (Prompt B dashboard + entry engine persistence) ----------------
    def upsert_candles(self, candles) -> None:
        self._conn.executemany(
            "INSERT OR REPLACE INTO candles (start, open, high, low, close, volume, "
            "source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(c.start.isoformat(timespec="seconds"), c.open, c.high, c.low, c.close,
              c.volume, c.source) for c in candles])
        self._conn.commit()

    def fetch_candles(self, iso_date: str) -> list:
        """Closed candles of one day, ascending. Rows come back as simple dicts."""
        rows = self._conn.execute(
            "SELECT start, open, high, low, close, volume, source FROM candles "
            "WHERE substr(start, 1, 10) = ? ORDER BY start", (iso_date,)).fetchall()
        out = []
        for start, o, h, low, c, volume, source in rows:
            try:
                start_dt = datetime.fromisoformat(start)
            except ValueError:
                continue
            out.append({"start": start_dt, "open": o, "high": h, "low": low,
                        "close": c, "volume": volume, "source": source})
        return out

    # -- daily CSV export ---------------------------------------------------------
    def export_day(self, iso_date: str):
        """Write journal / oi_snapshots / oi_strike_snapshots rows for one day to CSV."""
        settings.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        paths = []
        for table in ("journal", "oi_snapshots", "oi_strike_snapshots"):
            rows, columns = self._fetch_day(table, iso_date)
            if not rows:
                continue
            out_path = settings.EXPORTS_DIR / f"{iso_date}_{table}.csv"
            with open(out_path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                writer.writerows(rows)
            paths.append(out_path)
        log.info("CSV export for %s: %s", iso_date, [str(p) for p in paths])
        return paths

    def _fetch_day(self, table, iso_date):
        columns = [r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")]
        rows = self._conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE substr(ts, 1, 10) = ? ORDER BY id",
            (iso_date,)).fetchall()
        return rows, columns

    def close(self):
        self._conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Export one day's journal + OI snapshots to CSV files.")
    parser.add_argument("--date", default=ist_now().date().isoformat(),
                        help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    setup_logging()
    exported = JournalStore().export_day(args.date)
    print("Exported:", *[f"  {p}" for p in exported] or ["  (no rows for that date)"],
          sep="\n")
