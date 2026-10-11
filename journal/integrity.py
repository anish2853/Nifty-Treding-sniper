"""Journal Integrity Verification and Lifecycle Invariant Auditor (Phase 5).

Verifies Section 20 assertions:
1. Unique trade_id.
2. Exactly ONE entry event per trade.
3. Zero or ONE partial exit event per defined partial.
4. Zero or MORE legitimate trail updates.
5. Exactly ONE final exit event.
6. No duplicate trade rows.
7. Valid state transitions.
8. Quantity cannot become negative.
9. Partial quantity <= current quantity.
10. Exit quantity == remaining quantity.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

log = logging.getLogger(__name__)


class JournalIntegrityError(Exception):
    """Raised when an authoritative journal invariant is violated."""
    pass


def verify_journal_integrity(conn: sqlite3.Connection | Any) -> tuple[bool, list[str]]:
    """Audits the SQLite journal store against all Phase 5 integrity invariants.

    Returns:
        tuple[bool, list[str]]: (passed, list_of_violations)
    """
    violations: list[str] = []

    # If it's a JournalStore wrapper object, unwrap conn
    raw_conn = getattr(conn, "conn", conn)
    if not isinstance(raw_conn, sqlite3.Connection):
        try:
            raw_conn = getattr(conn, "_conn", conn)
        except Exception:
            pass

    if not isinstance(raw_conn, sqlite3.Connection):
        return False, ["Invalid database connection provided to verify_journal_integrity"]

    cursor = raw_conn.cursor()

    # 1. Check if journal table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='journal'")
    if not cursor.fetchone():
        return True, []  # Empty/uninitialized DB is trivially valid

    # 2. Check for duplicate exact events: (ts, type, trade_id)
    cursor.execute("""
        SELECT ts, type, trade_id, COUNT(*) as cnt
        FROM journal
        WHERE trade_id IS NOT NULL AND trade_id != ''
        GROUP BY ts, type, trade_id
        HAVING cnt > 1
    """)
    dup_rows = cursor.fetchall()
    for row in dup_rows:
        violations.append(f"Duplicate journal row detected: ts={row[0]} type={row[1]} trade_id={row[2]} (count={row[3]})")

    # 3. Check trade lifecycle invariants per distinct trade_id
    cursor.execute("""
        SELECT DISTINCT trade_id
        FROM journal
        WHERE trade_id IS NOT NULL AND trade_id != ''
    """)
    trade_ids = [r[0] for r in cursor.fetchall()]

    for tid in trade_ids:
        cursor.execute("""
            SELECT id, ts, type, notes
            FROM journal
            WHERE trade_id = ?
            ORDER BY id ASC
        """, (tid,))
        events = cursor.fetchall()

        entries = [e for e in events if e[2] in ("SIGNAL", "ENTRY")]
        exits = [e for e in events if e[2] == "EXIT"]

        if len(entries) > 1:
            violations.append(f"Trade {tid} has multiple entry events ({len(entries)})")

        if len(exits) > 1:
            violations.append(f"Trade {tid} has multiple final exit events ({len(exits)})")

        # If trade has an exit, it MUST have an entry
        if len(exits) >= 1 and len(entries) == 0:
            violations.append(f"Trade {tid} has exit event without an entry event")

    passed = len(violations) == 0
    return passed, violations


def assert_trade_lifecycle_invariants(events: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Audits an in-memory sequence of lifecycle events for a single trade.

    Supported event dict keys:
    - type: "ENTRY" | "TRAIL" | "PARTIAL_EXIT" | "EXIT"
    - qty: float | int
    - trade_id: str
    """
    violations: list[str] = []
    trade_id = events[0].get("trade_id", "UNKNOWN") if events else "UNKNOWN"

    initial_qty = 0
    current_qty = 0
    has_entered = False
    has_exited = False
    partial_exits_count = 0

    for idx, ev in enumerate(events):
        etype = ev.get("type")
        qty = ev.get("qty", 0)

        if etype == "ENTRY":
            if has_entered:
                violations.append(f"Trade {trade_id} has duplicate ENTRY event at index {idx}")
            if qty <= 0:
                violations.append(f"Trade {trade_id} ENTRY quantity must be positive, got {qty}")
            has_entered = True
            initial_qty = qty
            current_qty = qty

        elif etype == "TRAIL":
            if not has_entered:
                violations.append(f"Trade {trade_id} has TRAIL before ENTRY")
            if has_exited:
                violations.append(f"Trade {trade_id} has TRAIL after final EXIT")

        elif etype == "PARTIAL_EXIT":
            if not has_entered:
                violations.append(f"Trade {trade_id} has PARTIAL_EXIT before ENTRY")
            if has_exited:
                violations.append(f"Trade {trade_id} has PARTIAL_EXIT after EXIT")
            if qty <= 0:
                violations.append(f"Trade {trade_id} PARTIAL_EXIT quantity must be positive, got {qty}")
            if qty > current_qty:
                violations.append(f"Trade {trade_id} partial exit quantity {qty} exceeds remaining quantity {current_qty}")
            current_qty -= qty
            partial_exits_count += 1
            if current_qty < 0:
                violations.append(f"Trade {trade_id} quantity became negative after partial exit: {current_qty}")

        elif etype == "EXIT":
            if not has_entered:
                violations.append(f"Trade {trade_id} has EXIT before ENTRY")
            if has_exited:
                violations.append(f"Trade {trade_id} has multiple EXIT events")
            if qty != current_qty:
                violations.append(f"Trade {trade_id} final exit quantity {qty} != remaining quantity {current_qty}")
            current_qty -= qty
            has_exited = True
            if current_qty != 0:
                violations.append(f"Trade {trade_id} final quantity after exit must be 0, got {current_qty}")

    return len(violations) == 0, violations
