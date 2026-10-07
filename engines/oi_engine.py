"""OI engine.

Spec Module 1: "fresh-writing vs unwinding per strike by comparing change-in-OI
across consecutive snapshots". NSE's changeinOpenInterest is a DAY-level figure, so
diffing it between two snapshots yields the intraday rate of OI change:
rising  -> fresh writing (walls being built),
falling -> unwinding (writers covering).
"""
import logging
from dataclasses import dataclass, field

import settings

log = logging.getLogger(__name__)

FRESH_WRITING = "FRESH_WRITING"
UNWINDING = "UNWINDING"
FLAT = "FLAT"


@dataclass
class StrikeDelta:
    expiry: str
    strike: int
    ce_change_oi_now: float | None = None
    pe_change_oi_now: float | None = None
    ce_delta_snap: float | None = None   # curr change-in-OI minus prev snapshot's
    pe_delta_snap: float | None = None


@dataclass
class OIDiff:
    deltas: list = field(default_factory=list)
    underlying_prev: float | None = None
    underlying_now: float | None = None


def diff_snapshots(prev_rows, curr_rows, prev_underlying=None, curr_underlying=None) -> OIDiff:
    """Diff per-strike change-in-OI between two parsed snapshots. Strikes absent from
    the previous snapshot get None deltas (no basis for comparison yet)."""
    prev = {(r.expiry, r.strike): r for r in prev_rows}
    deltas = []
    for row in curr_rows:
        old = prev.get((row.expiry, row.strike))
        delta = StrikeDelta(expiry=row.expiry, strike=row.strike,
                            ce_change_oi_now=row.ce_change_oi,
                            pe_change_oi_now=row.pe_change_oi)
        if old is not None:
            if row.ce_change_oi is not None and old.ce_change_oi is not None:
                delta.ce_delta_snap = round(row.ce_change_oi - old.ce_change_oi, 2)
            if row.pe_change_oi is not None and old.pe_change_oi is not None:
                delta.pe_delta_snap = round(row.pe_change_oi - old.pe_change_oi, 2)
        deltas.append(delta)
    return OIDiff(deltas=deltas, underlying_prev=prev_underlying,
                  underlying_now=curr_underlying)


def classify(delta) -> str:
    if delta is None or delta == 0:
        return FLAT
    return FRESH_WRITING if delta > 0 else UNWINDING


def spot_adjacent(rows, spot, count: int = 3, side: str = "above", expiry=None):
    """Nearest `count` strikes above/below spot, optionally for one expiry. Phase B's
    entry engine uses this for the 'strikes just above spot' OI-confirmation check."""
    if spot is None:
        return []
    pool = [r for r in rows if expiry is None or r.expiry == expiry]
    if side == "above":
        picks = sorted((r for r in pool if r.strike > spot), key=lambda r: r.strike)
    else:
        picks = sorted((r for r in pool if r.strike < spot), key=lambda r: r.strike,
                       reverse=True)
    return picks[:count]


def recent_oi_net(store_conn, expiry: str, strikes, side: str,
                  snapshots: int | None = None) -> dict:
    """Net change-in-OI per strike across the last `snapshots` snapshots
    (spec: 'last 2-3 chain snapshots'). side: 'ce' | 'pe'. Strikes need an
    observation at BOTH ends of the window; returns {strike: net}."""
    snapshots = snapshots or settings.OI_CONFIRM_SNAPSHOTS
    ids = [r[0] for r in store_conn.execute(
        "SELECT id FROM oi_snapshots ORDER BY id DESC LIMIT ?", (snapshots,))]
    strike_list = sorted({int(s) for s in (strikes or [])})
    if len(ids) < 2 or not strike_list:
        return {}
    newest, oldest = ids[0], ids[-1]
    placeholders = ",".join("?" * len(strike_list))
    column = f"{side}_change_oi"
    ends: dict = {}
    for snapshot_id in (newest, oldest):
        rows = store_conn.execute(
            f"SELECT strike, {column} FROM oi_strike_snapshots "
            f"WHERE snapshot_id = ? AND expiry = ? AND strike IN ({placeholders})",
            (snapshot_id, expiry, *strike_list)).fetchall()
        ends[snapshot_id] = {r[0]: r[1] for r in rows if r[1] is not None}
    net = {}
    for strike in strike_list:
        new_v, old_v = ends[newest].get(strike), ends[oldest].get(strike)
        if new_v is not None and old_v is not None:
            net[strike] = round(new_v - old_v, 2)
    return net


def classify_oi_window(net_by_strike: dict) -> str:
    """'PASS' (any falling = writers covering) / 'REJECT' (any rising = fresh wall,
    signal rejected outright per spec) / 'UNKNOWN' (no usable data -> 0 points)."""
    values = list((net_by_strike or {}).values())
    if not values or all(v == 0 for v in values):
        return "UNKNOWN"
    if any(v > 0 for v in values):
        return "REJECT"
    if any(v < 0 for v in values):
        return "PASS"
    return "UNKNOWN"
