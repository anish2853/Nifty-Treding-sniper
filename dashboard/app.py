"""Streamlit paper dashboard (Prompt B module 5). Read-only over the journal:
candles with 🎯/🚨 markers, ORB box, PDH/PDL, max-OI walls, grey near-miss dots,
open trades + running stats. Refresh 60 s.

Run:  streamlit run dashboard/app.py
"""
import json
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
from journal.store import JournalStore
from utils import ist_now

st.set_page_config(page_title="Nifty Sniper — Paper", layout="wide")
st.title("Nifty Sniper — paper dashboard (alert-only)")


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
        "SELECT ts, type, direction, strike, spot, score, reasons_json, exit_reason, "
        "notes FROM journal WHERE substr(ts, 1, 10) = ? ORDER BY id",
        (today_iso,)).fetchall()
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


@st.fragment(run_every="60s")
def render() -> None:
    store = JournalStore()
    today_iso = ist_now().date().isoformat()
    candles, journal, snap, pdh, pdl = _load(store, today_iso)

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
                      line=dict(color="royalblue", width=1))
        fig.add_annotation(x=box_start, y=orb["high"],
                           text=f"ORB {orb['low']:.0f}–{orb['high']:.0f}",
                           showarrow=False, xanchor="left", yanchor="bottom",
                           font=dict(size=10, color="royalblue"))
    for level, label, color in ((pdh, "PDH", "#2e7d32"), (pdl, "PDL", "#c62828")):
        if level is not None:
            fig.add_hline(y=level, line_dash="dot", line_color=color,
                          annotation_text=label, annotation_position="right")
    for row in journal:
        try:
            reasons = json.loads(row[6] or "{}")
        except ValueError:
            reasons = {}
        viz = reasons.get("viz")
        payload = {k: v for k, v in reasons.items() if k != "viz"}
        if viz == "zone":
            color = {"WAITING": "rgba(255,193,7,0.18)",
                     "CONFIRMED": "rgba(76,175,80,0.22)",
                     "EXPIRED": "rgba(158,158,158,0.15)",
                     "STALE": "rgba(158,158,158,0.15)",
                     "FIRED": "rgba(33,150,243,0.2)"}.get(
                payload.get("status"), "rgba(255,193,7,0.18)")
            formed = payload.get("formed_at")
            x0 = datetime.fromisoformat(formed) if formed else None
            x1 = ist_now() if x0 else None
            if x0 and payload.get("trigger") is not None and payload.get("stop") is not None:
                y0, y1 = sorted((payload["trigger"], payload["stop"]))
                fig.add_shape(type="rect", x0=x0, x1=x1, y0=y0, y1=y1,
                              fillcolor=color, line=dict(width=1, color="#ffb300"),
                              layer="below")
                fig.add_annotation(x=x1, y=y1, text=payload.get("status", "ZONE"),
                                   showarrow=False, font=dict(size=9))
        elif viz == "coil" and payload.get("low") is not None:
            start = payload.get("start")
            x0 = datetime.fromisoformat(start) if start else None
            if x0:
                fig.add_shape(type="rect", x0=x0, x1=ist_now(),
                              y0=payload["low"], y1=payload["high"],
                              fillcolor="rgba(156,39,176,0.12)",
                              line=dict(color="#8e24aa", width=1),
                              annotation=dict(text=f"COIL {payload.get('status', 'COILING')}",
                                              showarrow=False, font=dict(size=9)))

    vwap_points = []
    if candles:
        typical = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles]
        cumulative = 0.0
        for index, candle in enumerate(candles):
            cumulative += typical[index]
            vwap_points.append(cumulative / (index + 1))
        fig.add_trace(go.Scatter(
            x=[c["start"] + timedelta(minutes=5) for c in candles],
            y=vwap_points, mode="lines", name="VWAP (proxy)",
            line=dict(color="#1565c0", width=1, dash="dot")))

    if snap:
        if snap[3] is not None:
            fig.add_hline(y=snap[3], line_dash="dash", line_color="#ef6c00",
                          annotation_text=f"max CE OI {snap[3]:.0f}",
                          annotation_position="right")
        if snap[4] is not None:
            fig.add_hline(y=snap[4], line_dash="dash", line_color="#6a1b9a",
                          annotation_text=f"max PE OI {snap[4]:.0f}",
                          annotation_position="right")

    # Liquidity map (Phase B+): S/R ladder + STRONG tags + PDH/PDL pools
    underlying, nearest, snap_ts, oi_rows = store.latest_oi_rows()
    ladder_text = ""
    lmap = None
    if oi_rows and underlying:
        lmap = compute_liquidity_map(oi_rows, underlying, nearest, pdh=pdh, pdl=pdl,
                                     ts=snap_ts)
        if lmap:
            ladder_text = "\n".join(lmap.ladder_lines())
            for lv in lmap.levels:
                fig.add_hline(
                    y=lv.strike,
                    line_dash="dot" if not lv.strong else "dashdot",
                    line_width=2 if lv.strong else 1,
                    line_color="#43a047" if lv.side == "S" else "#e53935",
                    annotation_text=f"{lv.label}{' STRONG' if lv.strong else ''} "
                                    f"{lv.arrow}",
                    annotation_position="right")
    if oi_rows and underlying and pdh is not None and candles:
        for level in build_level_grid(
                spot=underlying, pdh=pdh, pdl=pdl,
                candles=[CandleRow(c) for c in candles],
                vwap=vwap_points[-1] if vwap_points else None,
                liquidity=lmap):
            if level.label == "VWAP":
                continue
            fig.add_hline(
                y=level.price,
                line_dash="dashdot" if level.strong else "dot",
                line_width=2 if level.strong else 1,
                line_color="#1b5e20" if level.side == "S" else "#b71c1c",
                annotation_text=f"{level.label}{' STRONG' if level.strong else ''}",
                annotation_position="right")

    def add_marker(x, y, symbol, color, text, size=13):
        fig.add_trace(go.Scatter(x=[x], y=[y], mode="markers+text",
                                 marker=dict(symbol=symbol, size=size, color=color),
                                 text=[text], textposition="top center",
                                 textfont=dict(size=9), hovertext=text,
                                 hoverinfo="text", showlegend=False))

    for row in journal:
        ts, kind, direction, strike, spot, score, reasons_json, exit_reason, notes = row
        try:
            reasons = json.loads(reasons_json or "{}")
        except ValueError:
            reasons = {}
        family = reasons.get("family", "BREAKOUT")
        candle_start = reasons.get("candle_start")
        x, y = _candle_close_time(candles, candle_start) if candle_start \
            else (None, None)
        if kind == "SIGNAL" and x:
            add_marker(x, y, "triangle-up" if direction == "LONG" else "triangle-down",
                       "#00c853" if direction == "LONG" else "#d500f9",
                       f"🎯 {family} {direction} {strike} score {score}", 15)
        elif kind == "SKIP":
            event = reasons.get("event")
            if event in ("1R", "MTF-TGT1", "oi-flip-mtf"):
                add_marker(datetime.fromisoformat(ts), spot, "diamond", "#ffd600",
                           f"80% book {event}", 12)
            elif "near-miss" in (notes or "") and x:
                add_marker(x, y, "circle", "#9e9e9e", f"{score}", 9)
        elif kind == "EXIT":
            add_marker(x or datetime.fromisoformat(ts), y or spot, "x", "#ff1744",
                       f"🚨 {exit_reason or 'exit'}")

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
        for family in ("BREAKOUT", "SWEEP-FADE", "MTF-SCALP", "COIL-SNIPE", "LEVEL-CONT",
                       "LEVEL-FADE"):
            fam = stats.running_stats(store.conn, family=family)
            if fam["count"]:
                st.write(f"[{family}] {fam['count']} trades | "
                         f"{fam['total_r']:+.2f}R total | exp {fam['expectancy']}")
    if ladder_text:
        with st.expander("Liquidity map (latest chain poll)"):
            st.text(ladder_text)
    store.close()


render()
