"""EOD paper accounting, read from the journal (Prompt 2: 15:40 📕 EOD SUMMARY).

Paper trades appear here from Phase B onwards (SIGNAL rows with a trade_id, closed
by EXIT rows carrying result_r / pnl_rupees). The post-exit SHADOW (hotfix #3)
re-simulates every exit's ORIGINAL plan to 15:10 on stored candles and reports
EXIT SAVED vs EXIT COST by exit type - measured, never guessed.
"""
import json
import logging
from datetime import datetime, timedelta

import settings

log = logging.getLogger(__name__)


def closed_trades(conn, iso_date: str, family: str | None = None) -> list:
    """Today's EXIT rows joined to their SIGNAL rows (entry LTP) via trade_id,
    optionally filtered to one setup family."""
    query = ("SELECT ts, trade_id, direction, strike, option_ltp, result_r, exit_reason, "
             "pnl_rupees, family FROM journal WHERE type = 'EXIT' AND "
             "substr(ts, 1, 10) = ?")
    params = [iso_date]
    if family is not None:
        query += " AND family = ?"
        params.append(family)
    rows = conn.execute(query + " ORDER BY id", params).fetchall()
    trades = []
    for ts, trade_id, direction, strike, exit_ltp, result_r, exit_reason, pnl, fam in rows:
        entry_ltp = None
        if trade_id:
            hit = conn.execute(
                "SELECT option_ltp FROM journal WHERE type = 'SIGNAL' AND trade_id = ? "
                "ORDER BY id LIMIT 1", (trade_id,)).fetchone()
            entry_ltp = hit[0] if hit else None
        trades.append({"time": ts[11:16], "direction": direction or "—",
                       "strike": strike, "entry": entry_ltp, "exit": exit_ltp,
                       "result_r": result_r, "exit_reason": exit_reason or "—",
                       "pnl_rupees": pnl, "family": fam or "BREAKOUT"})
    return trades


def open_trades(conn, iso_date: str) -> list:
    """SIGNAL rows of the day with no matching EXIT (the 15:10 square-off check)."""
    rows = conn.execute(
        "SELECT trade_id, direction, strike, option_ltp, family FROM journal "
        "WHERE type = 'SIGNAL' AND substr(ts, 1, 10) = ? ORDER BY id",
        (iso_date,)).fetchall()
    opened = []
    for trade_id, direction, strike, option_ltp, family in rows:
        if trade_id is None:
            continue
        closed = conn.execute(
            "SELECT COUNT(*) FROM journal WHERE type = 'EXIT' AND trade_id = ?",
            (trade_id,)).fetchone()[0]
        if not closed:
            opened.append({"trade_id": trade_id, "direction": direction,
                           "strike": strike, "option_ltp": option_ltp,
                           "family": family or "BREAKOUT"})
    return opened


def running_stats(conn, family: str | None = None,
                  day_state: str | None = None) -> dict:
    """Running totals over ALL closed paper trades (optionally one setup family
    and/or one day state): count, win rate, expectancy, streak, max drawdown."""
    query = ("SELECT result_r FROM journal WHERE type = 'EXIT' AND result_r IS NOT NULL")
    params = []
    if family is not None:
        query += " AND (family = ? OR family IS NULL)"
        params.append(family)
    if day_state is not None:
        query += " AND day_state = ?"
        params.append(day_state)
    rows = conn.execute(query + " ORDER BY id", params).fetchall()
    rs = [r[0] for r in rows]
    count = len(rs)
    if count == 0:
        return {"count": 0, "win_rate": "—", "expectancy": "—", "streak": "—",
                "max_dd": 0.0, "total_r": 0.0}
    wins = sum(1 for r in rs if r > 0)
    total = sum(rs)
    last_sign = 1 if rs[-1] > 0 else -1
    streak_n = 0
    for r in reversed(rs):
        if (1 if r > 0 else -1) == last_sign:
            streak_n += 1
        else:
            break
    cumulative = peak = max_dd = 0.0
    for r in rs:
        cumulative += r
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return {"count": count,
            "win_rate": f"{wins / count:.0%}",
            "expectancy": f"{total / count:+.2f}R",
            "streak": f"{'W' if last_sign > 0 else 'L'}{streak_n}",
            "max_dd": max_dd, "total_r": total}


def _trade_line(t: dict) -> str:
    strike = f"{t['strike']:.0f}" if t["strike"] is not None else "?"
    entry = f"{t['entry']:.2f}" if t["entry"] is not None else "?"
    exit_ltp = f"{t['exit']:.2f}" if t["exit"] is not None else "?"
    result = f"{t['result_r']:+.2f}R" if t["result_r"] is not None else "—"
    family = f" [{t.get('family', 'BREAKOUT')}]" if t.get("family") else ""
    return (f"• {t['time']} IST {t['direction']} {strike}{family} | "
            f"entry {entry} -> exit {exit_ltp} | {result} | {t['exit_reason']}")


def build_eod_body(store, today_iso: str, regime_summary: str | None,
                   quality_lines: list) -> str:
    """Assemble the EOD body (the 📕 prefix is added by alerts.messages.eod)."""
    trades = closed_trades(store.conn, today_iso)
    day_r = sum((t["result_r"] or 0.0) for t in trades)
    pnls = [t["pnl_rupees"] for t in trades if t["pnl_rupees"] is not None]
    if not trades:
        day_rs = "Rs +0.00"
    elif len(pnls) == len(trades):
        day_rs = f"Rs {sum(pnls):+,.2f}"
    else:
        day_rs = "Rs n/a (rupee P&L fills from Phase B)"

    totals = running_stats(store.conn)
    lines = ["PAPER TRADES TODAY"]
    lines += [_trade_line(t) for t in trades] if trades else \
        ["• none — no qualifying signals today (score < 80, caps, or regime gates)"]
    lines += [
        "",
        f"DAY TOTAL: {day_r:+.2f}R | {day_rs}",
        "RUNNING TOTALS (all paper history)",
        f"trades {totals['count']} | win rate {totals['win_rate']} | "
        f"expectancy {totals['expectancy']}",
        f"streak {totals['streak']} | max drawdown {totals['max_dd']:+.2f}R",
    ]
    # Per-setup split (Phase B+): which setup family is carrying the other.
    # Legacy labels (BREAKOUT / SWEEP-FADE) kept for pre-v2 history.
    family_lines = []
    for family in ("ORB", "LIQ-BREAK", "SWEEP", "PULLBACK",
                   "BREAKOUT", "SWEEP-FADE"):
        fam = running_stats(store.conn, family=family)
        if fam["count"]:
            family_lines.append(f"• {family}: {fam['count']} trade(s), "
                                f"total {fam['total_r']:+.2f}R, "
                                f"expectancy {fam['expectancy']}, "
                                f"win rate {fam['win_rate']}")
    if family_lines:
        lines += ["BY SETUP"] + family_lines
    # Per-day-state split (Trend-Day v2): trend days vs range days.
    state_lines = []
    for day_state in ("TREND-UP", "TREND-DOWN", "RANGE"):
        st = running_stats(store.conn, day_state=day_state)
        if st["count"]:
            state_lines.append(f"• {day_state}: {st['count']} trade(s), "
                               f"total {st['total_r']:+.2f}R, "
                               f"expectancy {st['expectancy']}, "
                               f"win rate {st['win_rate']}")
    if state_lines:
        lines += ["BY DAY STATE"] + state_lines
    lines += [
        "",
        f"REGIME TODAY: {regime_summary or 'UNCLASSIFIED'}",
    ]
    if quality_lines:
        lines += ["", "DATA QUALITY"] + [f"• {q}" for q in quality_lines]
    shadow_day = shadow_report(store.conn, today_iso)
    if shadow_day["trades"]:
        lines += ["", "EXIT SHADOW (original plan tracked to 15:10)"]
        for t in shadow_day["trades"]:
            lines.append(f"• {t['trade_id']} [{t['bucket']}] actual "
                         f"{t['actual_r']:+.2f}R | shadow {t['shadow_r']:+.2f}R | "
                         f"SAVED Rs {t['saved']:,.0f} | COST Rs {t['cost']:,.0f}")
    totals = shadow_report(store.conn)
    bucket_lines = []
    for bucket in ("hard-SL", "thesis", "OI-strong", "OI-moderate", "time", "other"):
        b = totals["buckets"].get(bucket)
        if b:
            bucket_lines.append(f"• {bucket}: {b['count']} trade(s) | actual "
                                f"{b['actual_r']:+.2f}R vs shadow "
                                f"{b['shadow_r']:+.2f}R | SAVED Rs {b['saved']:,.0f} "
                                f"| COST Rs {b['cost']:,.0f}")
    if bucket_lines:
        lines += ["SHADOW TOTALS BY EXIT TYPE"] + bucket_lines
    return "\n".join(lines)


# --- post-exit shadow (hotfix #3): the metric for the 20-50 pt vision -----------

def _candles_for(conn, day: str) -> list:
    rows = conn.execute(
        "SELECT start, open, high, low, close FROM candles "
        "WHERE substr(start, 1, 10) = ? ORDER BY start", (day,)).fetchall()
    out = []
    for start, o, h, low, c in rows:
        try:
            out.append({"start": datetime.fromisoformat(start), "open": o,
                        "high": h, "low": low, "close": c})
        except ValueError:
            continue
    return out


def _shadow_bucket(code: str, grade: str) -> str:
    if grade == "OI-STRONG":
        return "OI-strong"
    if grade == "OI-MODERATE":
        return "OI-moderate"
    if code in ("SL_SPOT", "SL_PREM"):
        return "hard-SL"
    if code == "THESIS_DEAD":
        return "thesis"
    if code == "TIME_STOP":
        return "time"
    return "other"


def _simulate_shadow(shadow: dict, candles: list):
    """Walk the ORIGINAL plan (entry SL + original targets; after TGT 1 the stop
    moves to breakeven) forward from the exit to 15:10. Premium is modelled with
    the frozen PREMIUM_BETA. SL-first within a candle (pessimistic)."""
    direction = shadow.get("direction")
    entry_spot, entry_prem = shadow.get("entry_spot"), shadow.get("entry_prem")
    sl, t1, t2 = shadow.get("sl_spot"), shadow.get("orig_tgt1"), shadow.get("orig_tgt2")
    if None in (direction, entry_spot, entry_prem, sl, t1, t2):
        return None
    long = direction == "LONG"
    beta = settings.PREMIUM_BETA

    def prem(level):
        return entry_prem + beta * ((level - entry_spot) if long
                                    else (entry_spot - level))

    booked = False
    booked_level = None
    final = None
    for c in candles:
        if long:
            if c["low"] <= sl:
                final = sl
                break
            if not booked and c["high"] >= t1:
                booked, booked_level = True, t1
            if booked and c["low"] <= entry_spot:
                final = entry_spot
                break
            if c["high"] >= t2:
                final = t2
                break
        else:
            if c["high"] >= sl:
                final = sl
                break
            if not booked and c["low"] <= t1:
                booked, booked_level = True, t1
            if booked and c["high"] >= entry_spot:
                final = entry_spot
                break
            if c["low"] <= t2:
                final = t2
                break
    if final is None:
        final = candles[-1]["close"] if candles else entry_spot
    if booked:
        points = 0.5 * (prem(booked_level) - entry_prem) \
            + 0.5 * (prem(final) - entry_prem)
    else:
        points = prem(final) - entry_prem
    risk = settings.RISK_PREM_FRACTION * entry_prem
    return {"points": points, "r": (points / risk) if risk else None,
            "final_level": final, "booked": booked}


def shadow_report(conn, iso_date: str | None = None) -> dict:
    """Per exited trade: EXIT SAVED (₹ protected by exiting when we did) vs
    EXIT COST (profit the original plan would have made), bucketed by exit
    type: hard-SL / thesis / OI-strong / OI-moderate / time."""
    query = ("SELECT ts, trade_id, direction, result_r, exit_reason, reasons_json "
             "FROM journal WHERE type = 'EXIT' AND result_r IS NOT NULL")
    params = []
    if iso_date:
        query += " AND substr(ts, 1, 10) = ?"
        params.append(iso_date)
    trades = []
    buckets: dict = {}
    for ts, trade_id, direction, result_r, exit_reason, reasons_json in \
            conn.execute(query + " ORDER BY id", params).fetchall():
        try:
            shadow = (json.loads(reasons_json or "{}") or {}).get("shadow")
        except ValueError:
            shadow = None
        if not shadow:
            continue  # pre-shadow rows
        exit_ts_raw = shadow.get("exit_ts") or ts
        day = exit_ts_raw[:10]
        try:
            exit_dt = datetime.fromisoformat(exit_ts_raw)
        except ValueError:
            exit_dt = datetime.fromisoformat(ts)
        candles = [c for c in _candles_for(conn, day)
                   if c["start"] >= exit_dt
                   and (c["start"] + timedelta(minutes=5)).time()
                   <= settings.SQUARE_OFF_TIME]
        sim = _simulate_shadow(shadow, candles)
        if sim is None:
            continue
        bucket = _shadow_bucket(shadow.get("code", ""), shadow.get("grade", ""))
        actual_points = result_r * settings.RISK_PREM_FRACTION * shadow["entry_prem"]
        saved = max(0.0, actual_points - sim["points"]) * settings.LOT_SIZE
        cost = max(0.0, sim["points"] - actual_points) * settings.LOT_SIZE
        trades.append({"trade_id": trade_id, "direction": direction,
                       "bucket": bucket, "grade": shadow.get("grade", "OI-N/A"),
                       "actual_r": result_r, "shadow_r": sim["r"],
                       "saved": saved, "cost": cost})
    for t in trades:
        b = buckets.setdefault(t["bucket"], {"count": 0, "actual_r": 0.0,
                                             "shadow_r": 0.0, "saved": 0.0,
                                             "cost": 0.0})
        b["count"] += 1
        b["actual_r"] += t["actual_r"]
        b["shadow_r"] += t["shadow_r"]
        b["saved"] += t["saved"]
        b["cost"] += t["cost"]
    return {"trades": trades, "buckets": buckets}
