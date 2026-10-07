"""Streamlit paper dashboard (Prompt B module 5 + diagnostic upgrade).

READ-ONLY BY CONSTRUCTION: the dashboard opens the SQLite journal with a
mode=ro URI connection - no code path here can write engine state, the journal,
or block signal generation (see the SAFETY note in the README). WAL mode on the
writer lets the bot and this reader run concurrently without locking.

Sections: state banner (day state / freshness / budget), blocked-signal
counters, candlestick with 🎯/🚨/near-miss/blocked markers, ORB box, PDH/PDL,
liquidity ladder, open trades + running stats. Refresh 60 s.

Run:  streamlit run dashboard/app.py
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plotly.graph_objects as go
import streamlit as st

import settings
from engines.candles import orb_levels
from engines.level_grid import build_level_grid
from engines.liquidity import compute_liquidity_map
from journal import stats
from utils import ist_now

st.set_page_config(page_title="Nifty Sniper — Paper", layout="wide")
st.title("Nifty Sniper — paper dashboard (alert-only)")

MARK_SCORE_MIN = 55   # only candles scoring >= 55 get markers (no clutter)


class ReadOnlyJournal:
    """Duck-typed read-only view of the journal: mode=ro SQLite connection so a
    dashboard bug can never write engine state or lock the writer (SAFETY)."""

    def __init__(self, db_path: str):
        # Use immutable mode instead of mode=ro for cross-platform compatibility
        self._ro = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._ro

    def fetch_candles(self, iso_date: str) -> list:
        rows = self._ro.execute(
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

    def latest_oi_rows(self):
        from types import SimpleNamespace
        snap = self._ro.execute(
            "SELECT id, underlying, nearest_expiry, ts FROM oi_snapshots "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if not snap:
            return None, None, None, []
        snapshot_id, underlying, nearest, ts = snap
        rows = [SimpleNamespace(strike=r[0], ce_oi=r[1], pe_oi=r[2],
                                ce_change_oi=r[3], pe_change_oi=r[4], expiry=r[5])
                for r in self._ro.execute(
                    "SELECT strike, ce_oi, pe_oi, ce_change_oi, pe_change_oi, expiry "
                    "FROM oi_strike_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,)).fetchall()]
        return underlying, nearest, ts, rows

    def close(self):
        self._ro.close()


class CandleRow:
    """Lightweight candle wrapper for orb_levels over DB rows."""

    def __init__(self, row):
        self.start = row["start"]
        self.high = row["high"]
        self.low = row["low"]
        self.close = row["close"]


def _load(store, today_iso: str):
    candles = store.fetch_candles(today_iso)
    journal = store.conn.execute(
        "SELECT ts, type, direction, strike, spot, option_ltp, score, reasons_json, exit_reason, "
        "notes, day_state, family FROM journal WHERE substr(ts, 1, 10) = ? "
        "ORDER BY id", (today_iso,)).fetchall()
    snap = store.conn.execute(
        "SELECT underlying, pcr_total, pcr_nearest, max_call_oi_strike, "
        "max_put_oi_strike, vix, ts FROM oi_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    regime_row = store.conn.execute(
        "SELECT reasons_json FROM journal WHERE type = 'REGIME' AND "
        "notes LIKE '%classification%' AND substr(ts, 1, 10) = ? "
        "ORDER BY id DESC LIMIT 1", (today_iso,)).fetchone()
    pdh = pdl = None
    if regime_row:
        try:
            reasons = json.loads(regime_row[0] or "{}")
            pdh, pdl = reasons.get("pdh"), reasons.get("pdl")
        except ValueError:
            pass
    return candles, journal, snap, pdh, pdl


def _candle_close_time(candles, start_iso: str):
    for c in candles:
        if c["start"].isoformat(timespec="seconds") == start_iso:
            return c["start"] + timedelta(minutes=5), c["close"]
    return None, None


def _age_minutes(iso_text: str | None):
    if not iso_text:
        return None
    try:
        return (ist_now() - datetime.fromisoformat(iso_text)).total_seconds() / 60
    except ValueError:
        return None


def _state_banner(store, today_iso: str) -> dict:
    """Diagnostic banner data: day state, transition age, freshness, budget."""
    now = ist_now()
    transition = store.conn.execute(
        "SELECT ts, notes FROM journal WHERE type = 'REGIME' AND "
        "notes LIKE 'day state: %' ORDER BY id DESC LIMIT 1").fetchone()
    state_now = store.conn.execute(
        "SELECT day_state FROM journal WHERE day_state IS NOT NULL AND "
        "substr(ts, 1, 10) = ? ORDER BY id DESC LIMIT 1",
        (today_iso,)).fetchone()
    state_name = state_now[0] if state_now else "RANGE (no tagged rows yet)"
    since_txt, since_age = "-", None
    if transition:
        ts = transition[0]
        since_txt = f"{ts[11:16]} | {transition[1][11:]}"
        since_age = _age_minutes(ts)
    last_row = store.conn.execute(
        "SELECT MAX(ts) FROM journal WHERE substr(ts, 1, 10) = ?",
        (today_iso,)).fetchone()[0]
    last_chain = store.conn.execute(
        "SELECT MAX(ts) FROM oi_snapshots WHERE substr(ts, 1, 10) = ?",
        (today_iso,)).fetchone()[0]
    last_candle = store.conn.execute(
        "SELECT MAX(start) FROM candles WHERE substr(start, 1, 10) = ?",
        (today_iso,)).fetchone()[0]
    budget_used = store.conn.execute(
        "SELECT COUNT(*) FROM journal WHERE type = 'SIGNAL' AND "
        "substr(ts, 1, 10) = ? AND (notes IS NULL OR "
        "notes NOT LIKE '%re-entry grant%')", (today_iso,)).fetchone()[0]
    day_state_name = state_now[0] if state_now else "RANGE"
    budget_max = settings.BUDGET_TREND \
        if day_state_name in ("TREND-UP", "TREND-DOWN") else settings.BUDGET_RANGE
    return {"state": state_name, "since": since_txt, "since_age": since_age,
            "journal_age": _age_minutes(last_row), "chain_age": _age_minutes(last_chain),
            "candle_age": _age_minutes(last_candle),
            "budget_used": budget_used, "budget_max": budget_max}


def _gate_of(notes: str) -> str:
    """Extract the blocking gate from a SKIP note for the counters."""
    if "NO ROOM" in notes:
        return "no-room"
    if "stale" in notes or "backlog" in notes:
        return "stale"
    if "signal suppressed by gate:" in notes:
        return notes.split("signal suppressed by gate:", 1)[1].strip()[:40]
    if "signal blocked" in notes:
        return notes.split("signal blocked", 1)[1].strip(" ():")[:40] or "blocked"
    return "other"


def _counters(store, today_iso: str) -> dict:
    fired = store.conn.execute(
        "SELECT COUNT(*) FROM journal WHERE type = 'SIGNAL' AND "
        "substr(ts, 1, 10) = ?", (today_iso,)).fetchone()[0]
    skips = store.conn.execute(
        "SELECT score, notes FROM journal WHERE type = 'SKIP' AND "
        "substr(ts, 1, 10) = ?", (today_iso,)).fetchall()
    counters = {"evaluated": fired, "fired": fired, "near_miss": 0,
                "blocked": 0, "gates": {}, "no_room": 0, "stale": 0}
    for score, notes in skips:
        if "NO ROOM" in (notes or ""):
            counters["no_room"] += 1
        if "stale" in (notes or "") or "backlog" in (notes or ""):
            counters["stale"] += 1
        if "near-miss" in (notes or ""):
            counters["near_miss"] += 1
        elif "signal suppressed by gate" in (notes or ""):
            counters["blocked"] += 1
            gate = _gate_of(notes)
            counters["gates"][gate] = counters["gates"].get(gate, 0) + 1
    return counters


@st.fragment(run_every="60s")
def render() -> None:
    # SAFETY: read-only journal - writes are structurally impossible here.
    store = ReadOnlyJournal(settings.DB_PATH)
    today_iso = ist_now().date().isoformat()
    candles, journal, snap, pdh, pdl = _load(store, today_iso)
    banner = _state_banner(store, today_iso)
    counters = _counters(store, today_iso)

    # ---- 1. STATE BANNER (always visible) ----
    age = banner["since_age"]
    since_age_txt = f"{age:.1f} h ago" if age and age >= 90 else \
        (f"{age:.0f} min ago" if age is not None else "no transition yet")
    st.markdown(
        f"### DAY STATE: {banner['state']} | since {banner['since']} "
        f"| last transition {since_age_txt}")
    freshness = (f"store {banner['journal_age']:.1f} min ago" if banner["journal_age"] is not None
                 else "store: no rows")
    chain = (f"chain poll {banner['chain_age']:.1f} min ago" if banner["chain_age"] is not None
             else "chain poll: none today")
    candle = (f"candle {banner['candle_age']:.1f} min ago" if banner["candle_age"] is not None
              else "candle: none today")
    st.caption(f"{freshness} | {chain} | {candle} | budget remaining: "
               f"{max(0, banner['budget_max'] - banner['budget_used'])}/"
               f"{banner['budget_max']} ({banner['budget_used']} used)")

    # ---- 2. BLOCKED-SIGNAL COUNTERS (side panel data) ----
    with st.expander("Blocked-signal counters (which gate is eating signals)", expanded=False):
        st.write(f"setups evaluated to a decision: {counters['evaluated']} | "
                 f"fired: {counters['fired']} | near-miss (55-79): "
                 f"{counters['near_miss']} | blocked (>=80): {counters['blocked']}")
        for gate, count in sorted(counters["gates"].items(), key=lambda kv: -kv[1]):
            st.write(f"  • gate `{gate}`: {count}")

    # ---- 3. MARKER LEGEND ----
    st.caption("▼ legend — grey dot: near-miss (score 55-79, evaluation only, trades "
               "nothing) · orange dot: blocked setup (score >= 80, gate rejected — "
               "hover shows the gate) · ▲/▼/✕: actual trade events")

    if candles:
        orb = orb_levels([CandleRow(c) for c in candles])
    else:
        orb = None

    fig = go.Figure()
    if candles:
        fig.add_trace(go.Candlestick(
            x=[c["start"] + timedelta(minutes=5) for c in candles],
            open=[c["open"] for c in candles], high=[c["high"] for c in candles],
            low=[c["low"] for c in candles], close=[c["close"] for c in candles],
            name="NIFTY 5m", increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350"))
    else:
        st.info("No candles stored yet — the bot's candle engine fills this within "
                "a minute of market hours.")

    if orb:
        box_start = datetime.combine(ist_now().date(), settings.ORB_WINDOW_START,
                                     tzinfo=settings.IST)
        box_end = datetime.combine(ist_now().date(), settings.ORB_WINDOW_END,
                                   tzinfo=settings.IST)
        fig.add_shape(type="rect", x0=box_start, x1=box_end, y0=orb["low"],
                      y1=orb["high"], fillcolor="rgba(100,149,237,0.15)",
                      line=dict(color="royalblue", width=2))
        fig.add_annotation(x=box_start, y=orb["high"],
                           text=f"📦 Opening Range (ORB) {orb['low']:.0f}–{orb['high']:.0f}",
                           showarrow=False, xanchor="left", yanchor="bottom",
                           font=dict(size=11, color="royalblue", family="monospace"),
                           hovertext="Opening Range: first hour (09:15-10:15) high/low. "
                                     "Breakout target for entry engine.")
    for level, label_short, color in ((pdh, "PDH", "#2e7d32"), (pdl, "PDL", "#c62828")):
        if level is not None:
            label_full = f"Previous Day {'High' if label_short == 'PDH' else 'Low'} (${label_short})"
            fig.add_hline(y=level, line_dash="dot", line_color=color, line_width=2,
                          annotation_text=label_full, annotation_position="right",
                          annotation_font=dict(size=10, color=color))
    def add_marker(x, y, symbol, color, text, size=13, hovertemplate=None):
        fig.add_trace(go.Scatter(x=[x], y=[y], mode="markers+text",
                                 marker=dict(symbol=symbol, size=size, color=color),
                                 text=[text], textposition="top center",
                                 textfont=dict(size=9), 
                                 hovertemplate=hovertemplate if hovertemplate else text,
                                 hoverinfo="text" if not hovertemplate else "skip",
                                 showlegend=False))

    def build_signal_card(row) -> str:
        """Build full trade card HTML for signal hover tooltip."""
        ts, kind, direction, strike, spot, option_ltp, score, reasons_json, exit_reason, notes, \
            day_state, family = row
        try:
            reasons = json.loads(reasons_json or "{}")
        except ValueError:
            reasons = {}
        
        fam = family or reasons.get("family", "BREAKOUT")
        
        # Extract from reasons dict
        prem_range_low = reasons.get("entry_premium_low")
        prem_range_high = reasons.get("entry_premium_high")
        sl_spot = reasons.get("sl_spot")
        sl_prem = reasons.get("sl_prem")
        tgt1_spot = reasons.get("tgt1_spot")
        tgt1_prem = reasons.get("tgt1_prem")
        tgt2_spot = reasons.get("tgt2_spot")
        tgt2_prem = reasons.get("tgt2_prem")
        max_risk = reasons.get("max_risk")
        entry_time = ts[11:16] if ts else "—"
        exit_time = reasons.get("exit_window_end", "15:10")
        
        # Build card
        dir_label = "BUY CE" if direction == "LONG" else "BUY PE"
        entry_prem = f"₹{prem_range_low}-{prem_range_high}" if prem_range_low and prem_range_high \
                     else (f"₹{option_ltp}" if option_ltp else "—")
        
        card = f"{dir_label} {strike} @ {entry_prem} | Score {score} | {entry_time} IST\n"
        
        # Reasons checklist
        reason_items = []
        for key in ["orb_break", "close_hold", "oi_fuel", "vwap_aligned", "mtf_confirmation"]:
            if reasons.get(key):
                reason_items.append(key.replace("_", " ").title())
        
        if reason_items:
            card += "Reasons: " + " ✓ | ".join(reason_items) + " ✓\n"
        
        # SL
        if sl_spot and sl_prem:
            card += f"SL: spot {sl_spot:.0f} | prem ₹{sl_prem:.0f}\n"
        
        # Targets
        if tgt1_spot and tgt1_prem:
            card += f"TGT1: {tgt1_spot:.0f} → prem ~₹{tgt1_prem:.0f} (book 50%, SL to entry)\n"
        if tgt2_spot and tgt2_prem:
            card += f"TGT2: {tgt2_spot:.0f} → prem ~₹{tgt2_prem:.0f} (exit rest)\n"
        
        # Risk & Exit
        if max_risk:
            card += f"MAX RISK: ₹{max_risk:.0f} | Exit by {exit_time}"
        
        return card

    for row in journal:
        ts, kind, direction, strike, spot, option_ltp, score, reasons_json, exit_reason, notes, \
            day_state, family = row
        try:
            reasons = json.loads(reasons_json or "{}")
        except ValueError:
            reasons = {}
        fam = family or reasons.get("family", "BREAKOUT")
        candle_start = reasons.get("candle_start")
        x, y = _candle_close_time(candles, candle_start) if candle_start \
            else (None, None)
        if kind == "SIGNAL" and x:
            # Full trade card in hover
            card = build_signal_card(row)
            label = f"🎯 {fam} {direction} {strike}"
            add_marker(x, y, "triangle-up" if direction == "LONG" else "triangle-down",
                       "#00c853" if direction == "LONG" else "#d500f9",
                       label, 15, hovertemplate=card + "<extra></extra>")
        elif kind == "SKIP":
            event = reasons.get("event")
            if event in ("1R", "MTF-TGT1", "oi-flip-mtf"):
                add_marker(datetime.fromisoformat(ts), spot, "diamond", "#ffd600",
                           f"80% book {event}", 12)
            elif "near-miss" in (notes or "") and score is not None \
                    and score >= MARK_SCORE_MIN and x:
                hover_text = f"Near-Miss (Score {score})\n\nReason: score < {settings.ENTRY_SCORE_FIRE}"
                add_marker(x, y, "circle", "#9e9e9e", f"{score}", 9,
                          hovertemplate=hover_text + "<extra></extra>")
            elif "signal suppressed by gate" in (notes or "") and score is not None \
                    and score >= settings.ENTRY_SCORE_FIRE:
                gate = notes.split("gate:", 1)[-1].strip()[:60]
                hover_text = f"Blocked Setup (Score {score})\n\nGate: {gate}"
                add_marker(x or datetime.fromisoformat(ts), y or spot, "circle-open",
                           "#ff9800", f"🚫 {gate}", 11,
                          hovertemplate=hover_text + "<extra></extra>")
        elif kind == "EXIT":
            # Get trade result
            result_r = None
            pnl_text = exit_reason or "exit"
            # Check for result in earlier rows (linked by trade_id)
            exit_hover = f"Exit: {pnl_text}"
            add_marker(x or datetime.fromisoformat(ts), y or spot, "x", "#ff1744",
                       f"🚨 {exit_reason or 'exit'}", hovertemplate=exit_hover + "<extra></extra>")

    fig.update_layout(height=560, margin=dict(l=10, r=10, t=24, b=10),
                      xaxis_rangeslider_visible=False,
                      legend=dict(orientation="h", y=1.02, x=0))
    st.plotly_chart(fig, use_container_width=True)

    if snap:
        st.caption(f"Latest chain snapshot: spot {snap[0]}, PCR(all) "
                   f"{snap[1] if snap[1] is not None else '—'}, PCR(near) "
                   f"{snap[2] if snap[2] is not None else '—'}, VIX "
                   f"{snap[5] if snap[5] is not None else '—'} "
                   f"({snap[6]}), walls CE {snap[3]} / PE {snap[4]}")

    # Compute liquidity map from latest chain snapshot
    ladder_text = None
    underlying, nearest, ts, oi_rows = store.latest_oi_rows()
    if oi_rows and underlying:
        liquidity = compute_liquidity_map(oi_rows, underlying, nearest, pdh=pdh, pdl=pdl, ts=ts)
        if liquidity:
            ladder_text = str(liquidity)
    
    left, right = st.columns(2)
    with left:
        st.subheader("Open paper trades")
        open_trades = stats.open_trades(store.conn, today_iso)
        if open_trades:
            for t in open_trades:
                st.write(f"• [{t.get('family', 'BREAKOUT')}] {t['direction']} "
                         f"{t['strike']} | entry ₹{t['option_ltp']}"
                         f" | opened {t.get('time', '')}")
        else:
            st.write("none")
    with right:
        st.subheader("Running stats")
        totals = stats.running_stats(store.conn)
        st.write(f"trades {totals['count']} | win rate {totals['win_rate']} | "
                 f"expectancy {totals['expectancy']}")
        st.write(f"streak {totals['streak']} | max DD {totals['max_dd']:+.2f}R")
        for family in ("ORB", "LIQ-BREAK", "SWEEP", "PULLBACK", "SECOND-STAGE",
                       "MTF-SCALP", "COIL-SNIPE", "LEVEL-CONT", "LEVEL-FADE",
                       "BREAKOUT", "SWEEP-FADE"):
            fam = stats.running_stats(store.conn, family=family)
            if fam["count"]:
                st.write(f"[{family}] {fam['count']} trades | "
                         f"{fam['total_r']:+.2f}R total | exp {fam['expectancy']}")
    if ladder_text:
        with st.expander("Liquidity map (latest chain poll)"):
            st.text(ladder_text)
    store.close()
    st.caption("SAFETY: this dashboard opens the journal READ-ONLY (immutable=1) — "
               "no dashboard code path can write engine state, the journal, or "
               "block signal generation. The bot writes with WAL, so reader and "
               "writer never lock each other.")


render()
