"""Decision-log and liveness audit for --mode nearmiss."""
import json
import argparse
import re
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path

import settings
from engines.candles import orb_levels
from engines.level_grid import build_level_grid
from engines.liquidity import compute_liquidity_map
from journal.store import JournalStore
from utils import ist_now, safe_print


def _latest_state_and_levels(store, today):
    pdh = pdl = pdc = None
    rows = store.conn.execute(
        "SELECT reasons_json FROM journal WHERE type='REGIME' "
        "AND substr(ts,1,10)=? ORDER BY id DESC", (today,)).fetchall()
    for (raw,) in rows:
        try:
            reasons = json.loads(raw or "{}")
        except ValueError:
            continue
        pdh = reasons.get("pdh", pdh)
        pdl = reasons.get("pdl", pdl)
        pdc = reasons.get("pdc", pdc)
        if pdh is not None or pdl is not None:
            break
    return pdh, pdl, pdc


def _log_facts(log_path, today):
    facts = {"last_cycle": None, "last_day_state": None, "states": []}
    if not log_path.exists():
        return facts
    state_pattern = re.compile(r"day state: (TREND-UP|TREND-DOWN|RANGE)")
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(today):
            continue
        prefix = line.split(" | ", 1)[0]
        try:
            stamp = datetime.strptime(prefix, "%Y-%m-%d %H:%M:%S,%f") \
                .replace(tzinfo=settings.IST)
        except ValueError:
            continue
        if "| main | cycle:" in line:
            facts["last_cycle"] = stamp
        if "| main | day state:" in line:
            facts["last_day_state"] = stamp
            match = state_pattern.search(line)
            if match and (not facts["states"] or facts["states"][-1][1] != match.group(1)):
                facts["states"].append((stamp, match.group(1)))
    return facts


def _shadow_mfe_mae(candles, timestamp, direction, spot):
    if direction not in ("LONG", "SHORT") or spot is None:
        return "unavailable (direction/entry missing)"
    after = [c for c in candles if c["start"].isoformat() > timestamp]
    if not after:
        return "no post-skip candles stored"
    if direction == "LONG":
        favorable = max(c["high"] - spot for c in after)
        adverse = max(spot - c["low"] for c in after)
    else:
        favorable = max(spot - c["low"] for c in after)
        adverse = max(c["high"] - spot for c in after)
    return f"25pt-risk proxy: MFE {favorable:+.1f} pts, MAE {adverse:+.1f} pts"


def build_nearmiss_report(store: JournalStore, start_time: dt_time = dt_time(12, 45),
                          log_path: Path | None = None) -> str:
    now = ist_now()
    today = now.date().isoformat()
    start = datetime.combine(now.date(), start_time, tzinfo=settings.IST)
    since_iso = start.isoformat(timespec="seconds")
    rows = store.conn.execute(
        "SELECT ts,type,direction,strike,score,spot,exit_reason,result_r,family,notes,"
        "reasons_json FROM journal WHERE substr(ts,1,10)=? AND ts>=? ORDER BY id",
        (today, since_iso)).fetchall()
    class CandleView:
        """Attribute + item access over a candle dict (grid wants attrs, the
        audit body wants items)."""

        def __init__(self, d):
            object.__setattr__(self, "_d", dict(d))

        def __getattr__(self, name):
            try:
                return object.__getattribute__(self, "_d")[name]
            except KeyError as exc:
                raise AttributeError(name) from exc

        def __getitem__(self, name):
            return object.__getattribute__(self, "_d")[name]

    raw_candles = store.fetch_candles(today)
    candles = [CandleView(c) for c in raw_candles]
    recent_candles = [c for c in candles if c["start"].time() >= start_time]
    pdh, pdl, pdc = _latest_state_and_levels(store, today)
    orb = orb_levels(candles) if candles else None
    underlying, nearest, snap_ts, oi_rows = store.latest_oi_rows()
    liquidity = compute_liquidity_map(oi_rows, underlying, nearest, pdh=pdh, pdl=pdl,
                                      ts=snap_ts) if oi_rows and underlying else None
    grid = build_level_grid(spot=underlying or (candles[-1].close if candles else None),
                            pdh=pdh, pdl=pdl, pdc=pdc, orb=orb,
                            candles=candles, liquidity=liquidity)
    facts = _log_facts(log_path or settings.LOG_FILE, today)
    last_candle = candles[-1]["start"] + timedelta(minutes=5) if candles else None
    latest_chain = store.conn.execute(
        "SELECT MAX(ts) FROM oi_snapshots WHERE substr(ts,1,10)=?", (today,)).fetchone()[0]
    latest_chain = datetime.fromisoformat(latest_chain) if latest_chain else None

    lines = [f"# WHY NO TRADE — {today} {start_time:%H:%M} to {now:%H:%M} IST", "",
             "## Liveness",
             f"- Store refresh/loop: {facts['last_cycle'].isoformat(timespec='seconds') if facts['last_cycle'] else 'no cycle log'}",
             f"- Last day-state log: {facts['last_day_state'].isoformat(timespec='seconds') if facts['last_day_state'] else 'none'}",
             f"- Last stored 5m candle: {last_candle.isoformat(timespec='seconds') if last_candle else 'none'}",
             f"- Last chain poll: {latest_chain.isoformat(timespec='seconds') if latest_chain else 'none'}"]
    for label, stamp in (("loop", facts["last_cycle"]), ("candle", last_candle),
                         ("chain", latest_chain)):
        age = (now - stamp).total_seconds() if stamp else None
        lines.append(f"- {label} age: {age / 60:.1f} min" if age is not None
                     else f"- {label} age: unknown")

    lines += ["", "## State transitions"]
    if facts["states"]:
        lines.extend(f"- {stamp.strftime('%H:%M:%S')} {state}"
                     for stamp, state in facts["states"])
    else:
        lines.append("- No day-state log entries recorded today.")
    morning = [c for c in candles if dt_time(9, 15) <= c["start"].time() < dt_time(10, 15)]
    if len(morning) >= 2:
        floor = min(c["low"] for c in morning)
        ceiling = max(c["high"] for c in morning)
        below = 0
        above = 0
        breaks = []
        for candle in candles:
            if candle["start"].time() < dt_time(10, 15):
                continue
            below = below + 1 if candle["close"] < floor else 0
            above = above + 1 if candle["close"] > ceiling else 0
            if below == 2:
                breaks.append((candle["start"], "TREND-DOWN", floor))
            if above == 2:
                breaks.append((candle["start"], "TREND-UP", ceiling))
        lines.append(f"- First-hour range: {floor:.2f}-{ceiling:.2f}")
        if breaks:
            lines.extend(f"- Two-close range break: {stamp.strftime('%H:%M')} "
                         f"{state} beyond {level:.2f}" for stamp, state, level in breaks)
        else:
            lines.append("- No two consecutive 5m closes beyond the first-hour range boundary.")
    else:
        lines.append("- Insufficient stored bars to assess first-hour range breaks.")

    lines += ["", "## Persisted decisions"]
    decision_rows = [row for row in rows if row[1] in ("SIGNAL", "EXIT")
                     or row[1] == "SKIP"]
    if not decision_rows:
        lines.append("- No SIGNAL/EXIT/SKIP rows in this interval; the prior engine did not persist a full candidate trace.")
    for ts, kind, direction, strike, score, spot, exit_reason, result_r, family, notes, raw in decision_rows:
        reasons = {}
        try:
            reasons = json.loads(raw or "{}")
        except ValueError:
            pass
        if kind == "SKIP":
            shadow = _shadow_mfe_mae(recent_candles, ts, direction, spot)
            lines.append(f"- {ts[11:19]} SKIP {family or '?'} {direction or '?'} "
                         f"score {score if score is not None else 'not recorded'} "
                         f"spot {spot if spot is not None else '?'} | {notes or 'reason not recorded'} | {shadow}")
        elif kind == "SIGNAL":
            lines.append(f"- {ts[11:19]} SIGNAL {family or '?'} {direction or '?'} "
                         f"score {score if score is not None else '?'} spot {spot} strike {strike}")
        else:
            lines.append(f"- {ts[11:19]} EXIT {direction or '?'} {exit_reason or '?'} "
                         f"result {result_r if result_r is not None else '?'}R")

    lines += ["", "## Derived 5m level crossings"]
    crossings = []
    for previous, current in zip(recent_candles, recent_candles[1:]):
        for level in grid:
            if previous["close"] >= level.price and current["close"] < level.price:
                crossings.append((current["start"], "PE", level))
            elif previous["close"] <= level.price and current["close"] > level.price:
                crossings.append((current["start"], "CE", level))
    if crossings:
        for stamp, direction, level in crossings:
            lines.append(f"- {stamp.strftime('%H:%M')} {direction} crossed {level.label} "
                         f"{level.price:.2f}; score/gate: not persisted for this raw crossing")
    else:
        lines.append("- No adjacent-close crosses of the reconstructed PDH/PDL/ORB/swing/OI ladder.")

    lines += ["", "## Gate audit", 
              "- Legacy EntryEngine only evaluates 5m closes through 11:00; SweepEngine also has a 13:30-14:45 window.",
              "- The hardcoded windows above can silently prevent the reported 12:45+ candidates from reaching scoring.",
              "- Current OI runway uses total OI/max-OI strikes in some paths; that is not proof of writers actively adding.",
              "- CE/PE symmetry must be verified by the focused synthetic regression test after the gate changes.",
              "- This report separates persisted decisions from derived crossings; unlogged scores are not invented."]
    return "\n".join(lines)


def emit_nearmiss_report(store, tg=None, start_time: dt_time = dt_time(0, 0)) -> str:
    report = build_nearmiss_report(store, start_time=start_time)
    output = settings.BASE_DIR / "audit_report.md"
    output.write_text(report + "\n", encoding="utf-8")
    safe_print(report)
    if tg is not None:
        summary = "\n".join(report.splitlines()[:18])
        for offset in range(0, len(summary), 3500):
            tg.send(summary[offset:offset + 3500])
    return report


if __name__ == "__main__":
    from alerts.telegram_bot import TelegramSender

    parser = argparse.ArgumentParser(description="Print today's decision audit.")
    parser.add_argument("--since", default="00:00", help="HH:MM IST start time")
    args = parser.parse_args()
    try:
        hour, minute = (int(part) for part in args.since.split(":"))
        start_time = dt_time(hour, minute)
    except (ValueError, TypeError):
        parser.error("--since must be HH:MM")
    store = JournalStore()
    emit_nearmiss_report(store, TelegramSender(), start_time=start_time)
    store.close()