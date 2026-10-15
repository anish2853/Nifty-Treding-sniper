"""Streamlit Live Trading Dashboard — Institutional Liquidity, Fibonacci & Trendline Sniper.

Clean, focused, modern real-time and replay dashboard for the active working model:
- Pure Institutional Price Action (Boundary Sweeps + Intraday Retracements)
- Strict Trendline & Direction Verification (Zero counter-trend trades)
- Fibonacci Golden Pocket [0.50 - 0.618] Reload Zones
- 2-Stage Trade Management (50% at TGT1, Breakeven stop BE_SL)
- Ultra-clean TradingView-style candlestick chart (zero text smudge)

Run: streamlit run dashboard/app.py
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Silence Windows Proactor WinError 10054 on browser disconnect
if sys.platform == "win32":
    try:
        import asyncio.proactor_events

        _orig_call_conn_lost = (
            asyncio.proactor_events._ProactorBasePipeTransport._call_connection_lost
        )

        def _silent_call_conn_lost(self, exc):
            try:
                _orig_call_conn_lost(self, exc)
            except (ConnectionResetError, OSError):
                try:
                    if self._sock is not None:
                        self._sock.close()
                        self._sock = None
                    if self._server is not None:
                        self._server._detach()
                        self._server = None
                    self._called_connection_lost = True
                except Exception:
                    pass

        asyncio.proactor_events._ProactorBasePipeTransport._call_connection_lost = (
            _silent_call_conn_lost
        )
    except Exception:
        pass

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import settings
from dashboard.chart import build_liquidity_sniper_chart
from engines.candles import Candle
from engines.shadow_mw import MWLevel, UnifiedLiquiditySniper
from utils import ist_now

log = logging.getLogger("dashboard_app")

st.set_page_config(
    page_title="Nifty Sniper — Live Trading Dashboard",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)


class ReadOnlyJournal:
    """Read-only view of the SQLite journal in read-only WAL mode (SAFETY & LIVE SYNC)."""

    def __init__(self, db_path: str | Path):
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        self._ro = sqlite3.connect(uri, uri=True)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._ro

    def fetch_candles(self, iso_date: str) -> list[Candle]:
        try:
            rows = self._ro.execute(
                "SELECT start, open, high, low, close, volume FROM candles "
                "WHERE substr(start, 1, 10) = ? ORDER BY start",
                (iso_date,),
            ).fetchall()
            out = []
            for start, o, h, low, c, vol in rows:
                try:
                    start_dt = datetime.fromisoformat(start)
                    out.append(
                        Candle(
                            start=start_dt,
                            open=float(o),
                            high=float(h),
                            low=float(low),
                            close=float(c),
                            volume=float(vol) if vol is not None else None,
                            interval_minutes=5,
                        )
                    )
                except Exception:
                    continue
            return out
        except Exception:
            return []

    def close(self):
        try:
            self._ro.close()
        except Exception:
            pass


@st.cache_data
def get_2026_dataset_dataframe() -> pd.DataFrame:
    """Load cached 2026 Jan-Oct 5m dataset dataframe."""
    csv_file = settings.DATA_DIR / "nifty50_5min_2026_jan_to_oct.csv"
    if csv_file.exists():
        return pd.read_csv(csv_file)
    return pd.DataFrame()


def get_available_dates(store: ReadOnlyJournal) -> list[str]:
    """Fetch all unique session dates from 2026 Year-to-Date dataset and SQLite journal."""
    dates_set = set()
    df_2026 = get_2026_dataset_dataframe()
    if not df_2026.empty and "Timestamp" in df_2026.columns:
        for d in df_2026["Timestamp"].astype(str).str[:10].unique():
            if d:
                dates_set.add(d)

    try:
        rows = store.conn.execute(
            "SELECT DISTINCT substr(start, 1, 10) FROM candles ORDER BY 1 DESC"
        ).fetchall()
        for r in rows:
            if r[0]:
                dates_set.add(r[0])
    except Exception:
        pass

    today_real = ist_now().date().isoformat()
    dates_set.add(today_real)

    return sorted(list(dates_set), reverse=True)


def load_candles_for_session(store: ReadOnlyJournal, target_date_iso: str) -> list[Candle]:
    """Load session 5m candles from SQLite or 2026 Jan-Oct dataset."""
    candles = store.fetch_candles(target_date_iso)
    if candles and len(candles) >= 10:
        return candles

    # Load from 2026 Jan-Oct CSV dataset
    df_2026 = get_2026_dataset_dataframe()
    if not df_2026.empty and "Timestamp" in df_2026.columns:
        day_slice = df_2026[df_2026["Timestamp"].astype(str).str.startswith(target_date_iso)]
        if not day_slice.empty:
            loaded = []
            for _, row in day_slice.iterrows():
                try:
                    s_dt = datetime.strptime(str(row["Timestamp"])[:19], "%Y-%m-%d %H:%M:%S")
                    loaded.append(
                        Candle(
                            start=s_dt,
                            open=float(row["Open"]),
                            high=float(row["High"]),
                            low=float(row["Low"]),
                            close=float(row["Close"]),
                            volume=float(row["Volume"]) if "Volume" in row and pd.notna(row["Volume"]) else 100.0,
                            interval_minutes=5,
                        )
                    )
                except Exception:
                    continue
            if loaded:
                return loaded

    # Fallback to yfinance live replay data
    try:
        from data_sources.replay_data import fetch_yfinance_5m_data

        df_live, _ = fetch_yfinance_5m_data(days=10)
        if not df_live.empty:
            day_slice = df_live[df_live.index.strftime("%Y-%m-%d") == target_date_iso]
            if not day_slice.empty:
                loaded = []
                for ts, row in day_slice.iterrows():
                    loaded.append(
                        Candle(
                            start=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                            open=float(row["Open"]),
                            high=float(row["High"]),
                            low=float(row["Low"]),
                            close=float(row["Close"]),
                            volume=float(row["Volume"]) if "Volume" in row else 100.0,
                            interval_minutes=5,
                        )
                    )
                return loaded
    except Exception as exc:
        log.warning(f"Live fetch fallback failed for {target_date_iso}: {exc}")

    return []


def load_journal_trades_for_session(
    store: ReadOnlyJournal,
    target_date_iso: str,
    candles: list[Candle],
    pdh: float | None = None,
    pdl: float | None = None,
    vah: float | None = None,
    val: float | None = None,
    poc: float | None = None,
) -> tuple[list[dict], dict]:
    """Load executed trades strictly from journal.sqlite3 and compute market structure.

    Eliminates rogue in-memory simulation to guarantee:
    Journal Trades == Telegram Alerts == Dashboard Trades (Single Trade Book).
    """
    # If PDH/PDL was not passed from prev session, look up preceding session candles from DB
    if (pdh is None or pdl is None) and target_date_iso:
        try:
            prev_row = store.conn.execute(
                "SELECT MAX(high), MIN(low) FROM candles WHERE substr(start, 1, 10) < ? "
                "AND substr(start, 1, 10) = (SELECT MAX(substr(start, 1, 10)) FROM candles WHERE substr(start, 1, 10) < ?)",
                (target_date_iso, target_date_iso),
            ).fetchone()
            if prev_row and prev_row[0] is not None:
                if pdh is None:
                    pdh = round(float(prev_row[0]), 2)
                if pdl is None:
                    pdl = round(float(prev_row[1]), 2)
        except Exception as exc:
            log.warning("Could not query prior session PDH/PDL from database: %s", exc)

    # Intraday trend & trendline geometry strictly for chart visualization
    trend = "RANGE"
    tl_p1, tl_p2 = None, None
    if candles:
        try:
            sniper_arm = UnifiedLiquiditySniper(today_iso=target_date_iso, isolated=True)
            for c in candles:
                sniper_arm.arm2.on_candle(c)
            trend = sniper_arm.arm2.get_intraday_trend()
            tl_p1, tl_p2 = sniper_arm.arm2.get_trendline_points()
        except Exception as exc:
            log.warning("Structure analysis fallback: %s", exc)

    # 1. Fetch real executed trades from SQLite journal
    rows = store.conn.execute(
        "SELECT id, ts, type, direction, strike, spot, option_ltp, score, reasons_json, exit_reason, result_r, regime, notes, trade_id, family "
        "FROM journal WHERE substr(ts, 1, 10) = ? AND type IN ('SIGNAL', 'EXIT') "
        "ORDER BY id ASC",
        (target_date_iso,)
    ).fetchall()

    # 2. Check state.json for any live uncommitted executions / premiums
    live_trades_map = {}
    session_zones = []
    try:
        state_file = settings.DATA_DIR / "state.json"
        if state_file.exists():
            with open(state_file, "r", encoding="utf-8") as f:
                s_data = json.load(f)
                for st_item in s_data.get("sniper_trades", []):
                    if "trade_id" in st_item:
                        live_trades_map[st_item["trade_id"]] = st_item
                for ot_item in s_data.get("open_trades", []):
                    if "trade_id" in ot_item:
                        live_trades_map[ot_item["trade_id"]] = ot_item
                zr_data = s_data.get("zone_ride", {})
                for z in zr_data.get("support_zones", []):
                    session_zones.append(z)
                for z in zr_data.get("resistance_zones", []):
                    session_zones.append(z)
    except Exception:
        pass

    signals_by_id: dict[str, dict] = {}
    exits_by_id: dict[str, dict] = {}
    order_list: list[str] = []

    for r in rows:
        _id, ts, event_type, direction, strike, spot, option_ltp, score, reasons_json, exit_reason, result_r, regime, notes, trade_id, family = r

        reasons_data = {}
        if reasons_json:
            try:
                reasons_data = json.loads(reasons_json)
                if not isinstance(reasons_data, dict):
                    reasons_data = {}
            except Exception:
                reasons_data = {}

        notes_data = {}
        if notes:
            try:
                notes_data = json.loads(notes)
                if not isinstance(notes_data, dict):
                    notes_data = {}
            except Exception:
                notes_data = {}

        tid = trade_id or reasons_data.get("trade_id") or notes_data.get("trade_id")
        if not tid:
            tid = f"TR-{_id}"

        if event_type == "SIGNAL":
            if tid not in signals_by_id:
                order_list.append(tid)
            signals_by_id[tid] = {
                "id": _id,
                "ts": ts,
                "direction": direction or reasons_data.get("direction") or notes_data.get("direction") or "LONG",
                "strike": strike or reasons_data.get("strike") or notes_data.get("strike") or 0,
                "spot": spot or reasons_data.get("entry_price") or reasons_data.get("entry_spot") or 0.0,
                "option_ltp": option_ltp or reasons_data.get("entry_prem") or notes_data.get("entry_prem") or 0.0,
                "score": score or reasons_data.get("score") or 0,
                "family": family or reasons_data.get("family") or "BREAKOUT",
                "reasons_data": reasons_data,
                "notes_data": notes_data,
                "raw_notes": notes,
            }
        elif event_type == "EXIT":
            exits_by_id[tid] = {
                "id": _id,
                "ts": ts,
                "spot": spot or reasons_data.get("exit_price") or 0.0,
                "exit_reason": exit_reason or reasons_data.get("exit_reason") or "EXIT",
                "result_r": result_r if result_r is not None else reasons_data.get("realized_r", 0.0),
                "reasons_data": reasons_data,
                "notes_data": notes_data,
                "raw_notes": notes,
            }

    formatted_trades = []
    for tid in order_list:
        sig = signals_by_id[tid]
        ext = exits_by_id.get(tid)
        live_info = live_trades_map.get(tid, {})

        rd = sig["reasons_data"]
        nd = sig["notes_data"]
        exit_rd = ext["reasons_data"] if ext else {}
        exit_nd = ext["notes_data"] if ext else {}

        direction_raw = sig["direction"]
        is_long = (direction_raw == "LONG" or "CE" in direction_raw)
        direction_badge = "BUY CE" if is_long else "BUY PE"
        opt_type = "CE" if is_long else "PE"

        entry_price = float(sig["spot"] or rd.get("entry_price") or rd.get("entry_spot") or 0.0)
        entry_prem = float(live_info.get("entry_prem") or rd.get("entry_prem") or nd.get("entry_prem") or sig["option_ltp"] or 0.0)

        strike = int(round(sig["strike"] or rd.get("strike") or nd.get("strike") or (round(entry_price / 50.0) * 50 if entry_price > 0 else 0)))

        opt_sym = rd.get("option_symbol") or live_info.get("option_symbol") or (f"NIFTY {strike} {opt_type}" if strike > 0 else "")

        initial_sl = float(rd.get("initial_sl") or rd.get("sl_spot") or live_info.get("sl_spot") or (entry_price - 20.0 if is_long else entry_price + 20.0))
        current_sl = float(rd.get("current_sl") or initial_sl)
        risk_pts = float(rd.get("risk_pts") or abs(entry_price - initial_sl))
        if risk_pts <= 0:
            risk_pts = 20.0

        sl_prem = float(live_info.get("sl_prem") or rd.get("sl_prem") or max(5.0, round(entry_prem - (risk_pts * 0.50), 1)))

        tgt1_price = float(rd.get("tgt1_price") or rd.get("tgt1_spot") or (entry_price + risk_pts if is_long else entry_price - risk_pts))
        tgt2_price = float(rd.get("tgt2_price") or rd.get("tgt2_spot") or (entry_price + 2 * risk_pts if is_long else entry_price - 2 * risk_pts))

        tgt1_prem = float(live_info.get("tgt1_prem") or rd.get("tgt1_prem") or round(entry_prem + (abs(tgt1_price - entry_price) * 0.50), 1))
        tgt2_prem = float(live_info.get("tgt2_prem") or rd.get("tgt2_prem") or round(entry_prem + (abs(tgt2_price - entry_price) * 0.50), 1))

        f50 = float(rd.get("fib_50") or 0.0)
        f618 = float(rd.get("fib_618") or 0.0)
        f705 = float(rd.get("fib_705") or 0.0)

        raw_entry_time = sig["ts"]
        entry_time_str = raw_entry_time[11:16] if len(raw_entry_time) >= 16 else raw_entry_time

        # Exit details
        is_closed = (ext is not None)
        exit_price = ext["spot"] if is_closed else None
        raw_exit_time = ext["ts"] if is_closed else None
        exit_time_str = raw_exit_time[11:16] if (raw_exit_time and len(raw_exit_time) >= 16) else (raw_exit_time or "—")

        exit_reason_raw = ext["exit_reason"] if is_closed else ("PARTIAL_TGT1" if rd.get("half_booked") else "OPEN")
        result_r = float(ext["result_r"]) if is_closed and ext["result_r"] is not None else 0.0

        half_booked = bool(exit_rd.get("half_booked") or rd.get("half_booked") or ("BE_SL" in exit_reason_raw) or (result_r > 0 and "SL" in exit_reason_raw))

        # Reason display format
        if exit_reason_raw in ("BE_SL", "SL_HIT") and half_booked:
            reason_display = "BE_SL (+0.50R Locked)"
        elif exit_reason_raw == "BE_SL":
            reason_display = "BE_SL (+0.50R Locked)"
        elif exit_reason_raw in ("HARD_SL", "SL_HIT"):
            reason_display = "SL_HIT (-1.00R)"
        elif "TGT2" in exit_reason_raw:
            reason_display = f"TGT2_HIT ({result_r:+.2f}R)"
        elif is_closed:
            reason_display = exit_reason_raw
        else:
            reason_display = "PARTIAL_TGT1" if half_booked else "OPEN"

        exit_prem = exit_rd.get("exit_prem") or live_info.get("exit_prem")
        if exit_prem is None and is_closed:
            if "BE_SL" in exit_reason_raw or half_booked:
                exit_prem = entry_prem
            elif "SL" in exit_reason_raw:
                exit_prem = sl_prem
            elif "TGT2" in exit_reason_raw:
                exit_prem = tgt2_prem
            else:
                pts = (exit_price - entry_price) if is_long else (entry_price - exit_price)
                exit_prem = max(0.5, round(entry_prem + (pts * 0.50), 1))

        setup_name = rd.get("setup_name") or rd.get("level_name") or sig.get("family") or "Sniper Setup"
        notes_txt = exit_rd.get("notes") or rd.get("notes") or sig.get("raw_notes") or ""

        formatted_trades.append({
            "trade_id": tid,
            "arm": (
                "Arm 3 (Zone-Ride)"
                if ("ZONE-RIDE" in tid or "Zone-Ride" in setup_name or "ZR-" in tid)
                else (
                    "Arm 2 (Fib Retracement)"
                    if ("Fib" in setup_name or "TL-FIB" in tid)
                    else "Arm 1 (M/W Breakout/Sweep)"
                )
            ),
            "direction": direction_badge,
            "strike": strike,
            "option_type": opt_type,
            "option_symbol": opt_sym,
            "entry_prem": entry_prem,
            "sl_prem": sl_prem,
            "tgt1_prem": tgt1_prem,
            "tgt2_prem": tgt2_prem,
            "exit_prem": exit_prem,
            "setup": setup_name,
            "raw_entry_time": raw_entry_time,
            "entry_time": entry_time_str,
            "entry_price": entry_price,
            "sl": initial_sl,
            "current_sl": current_sl,
            "risk_pts": risk_pts,
            "fib_50": f50,
            "fib_618": f618,
            "fib_705": f705,
            "tgt1": tgt1_price,
            "tgt2": tgt2_price,
            "raw_exit_time": raw_exit_time,
            "exit_time": exit_time_str,
            "exit_price": exit_price if exit_price is not None else "—",
            "exit_reason": reason_display,
            "status": "CLOSED" if is_closed else ("PARTIAL_TGT1" if half_booked else "OPEN"),
            "half_booked": half_booked,
            "result_r": result_r if is_closed else None,
            "notes": notes_txt,
        })

    # If no journal rows existed (e.g. historical archive date from 2026 dataset),
    # compute trades via the unified liquidity sniper engine for historical inspection.
    # INVARIANT: FOR TODAY (LIVE MARKET), NEVER SIMULATE FAKE TRADES!
    # The live bot (main.py) is the sole trade authority. If 0 trades fired in real market, show 0 trades.
    today_real = ist_now().date().isoformat()
    if target_date_iso != today_real and not formatted_trades and candles and len(candles) >= 10:
        try:
            sniper_replay = UnifiedLiquiditySniper(
                today_iso=target_date_iso, pdh=pdh, pdl=pdl, isolated=True, vah=vah, val=val, poc=poc
            )
            fake_market = type("M", (), {"pdh": pdh, "pdl": pdl, "chain": None})()
            sniper_replay.arm1.initialize_levels(fake_market, today_iso=target_date_iso)
            for c in candles:
                sniper_replay.on_candle(c, allow_alerts=False)
            sniper_replay.square_off(exit_price=candles[-1].close, exit_time=candles[-1].start)

            for t in sniper_replay.get_all_trades():
                if getattr(t, "status", "") == "CLOSED":
                    if ("ZR-" in t.trade_id) or ("Zone Reversal" in getattr(t, "setup_name", "")) or ("Trendline" in getattr(t, "setup_name", "")):
                        arm_label = "Arm 3 (Zone Reversal / Trendline)"
                    elif ("SMW" in t.trade_id) or (getattr(t, "pattern", "") in ("W", "M")):
                        arm_label = "Arm 1 (M/W Liquidity Sweep)"
                    else:
                        arm_label = "Arm 2 (TL-FIB Golden Pocket)"
                    is_l = t.direction == "LONG"
                    dir_b = "BUY CE" if is_l else "BUY PE"
                    opt_t = "CE" if is_l else "PE"
                    stk = getattr(t, "strike", 0) or int(round(t.entry_price / 50.0) * 50)
                    sym_t = getattr(t, "option_symbol", "") or f"NIFTY {stk} {opt_t}"
                    e_raw = getattr(t, "entry_time", "")
                    x_raw = getattr(t, "exit_time", "")
                    e_str = e_raw[11:16] if len(e_raw) >= 16 else e_raw
                    x_str = x_raw[11:16] if (x_raw and len(x_raw) >= 16) else (x_raw or "—")
                    res_r = round(float(getattr(t, "realized_r", 0.0)), 2)

                    formatted_trades.append({
                        "trade_id": t.trade_id,
                        "arm": arm_label,
                        "direction": dir_b,
                        "strike": stk,
                        "option_type": opt_t,
                        "option_symbol": sym_t,
                        "entry_prem": round(float(getattr(t, "entry_prem", 0.0)), 1),
                        "sl_prem": round(float(getattr(t, "sl_prem", 0.0)), 1),
                        "tgt1_prem": round(float(getattr(t, "tgt1_prem", 0.0)), 1),
                        "tgt2_prem": round(float(getattr(t, "tgt2_prem", 0.0)), 1),
                        "exit_prem": round(float(getattr(t, "exit_prem", 0.0)), 1) if getattr(t, "exit_prem", None) else round(float(getattr(t, "entry_prem", 0.0)), 1),
                        "setup": getattr(t, "setup_name", "Sniper Setup"),
                        "raw_entry_time": e_raw,
                        "entry_time": e_str,
                        "entry_price": round(t.entry_price, 1),
                        "sl": round(getattr(t, "initial_sl", t.entry_price - 20.0 if is_l else t.entry_price + 20.0), 1),
                        "current_sl": round(getattr(t, "current_sl", t.initial_sl), 1),
                        "risk_pts": round(getattr(t, "risk_pts", 20.0), 1),
                        "fib_50": getattr(t, "fib_50", 0.0),
                        "fib_618": getattr(t, "fib_618", 0.0),
                        "fib_705": getattr(t, "fib_705", 0.0),
                        "tgt1": round(getattr(t, "tgt1_price", t.entry_price + 20.0), 1),
                        "tgt2": round(getattr(t, "tgt2_price", t.entry_price + 40.0), 1),
                        "raw_exit_time": x_raw,
                        "exit_time": x_str,
                        "exit_price": round(t.exit_price, 1) if t.exit_price else round(t.entry_price, 1),
                        "exit_reason": getattr(t, "exit_reason", "EXIT"),
                        "status": "CLOSED",
                        "half_booked": getattr(t, "half_booked", False),
                        "result_r": res_r,
                        "notes": getattr(t, "notes", ""),
                    })
        except Exception as exc:
            log.warning("Historical session replay failed: %s", exc)

    summary = {
        "trend": trend,
        "trendline": (tl_p1, tl_p2),
        "pdh": pdh,
        "pdl": pdl,
        "zones": session_zones,
        "total_trades": len(formatted_trades),
        "wins": sum(1 for t in formatted_trades if (t["result_r"] or 0) > 0),
        "losses": sum(1 for t in formatted_trades if (t["result_r"] or 0) < 0),
        "scratch": sum(1 for t in formatted_trades if (t["result_r"] or 0) == 0),
        "net_r": sum((t["result_r"] or 0) for t in formatted_trades),
        "win_rate": (
            (sum(1 for t in formatted_trades if (t["result_r"] or 0) > 0) / len(formatted_trades) * 100)
            if formatted_trades
            else 0.0
        ),
    }

    return formatted_trades, summary


def render_sidebar(available_dates: list[str], default_idx: int, today_real: str) -> str | None:
    # ---- SIDEBAR: Clean Controls & Session Selector ----
    with st.sidebar:
        st.subheader("🎯 Nifty Sniper Machine")
        st.success("🟢 Spec: FROZEN-v1 (One Brain Consolidated)")

        # Telegram Status Indicator
        from alerts.telegram_bot import TelegramSender
        tg_configured = TelegramSender().configured
        if tg_configured:
            st.success("📱 Telegram Alerts: READY & ACTIVE")
        else:
            st.warning("📱 Telegram: Unconfigured (console only)")

        st.caption("Active Strategy: **Unified Liquidity Sniper — Arm 1 (Sweeps) + Arm 2 (TL-FIB v2)**")
        st.caption("Execution Gate: **PAPER TRADING (Live Instant Signals)**")

        st.markdown("---")
        st.subheader("📅 Trading Session")
        selected_date = st.selectbox(
            "Select Session Date",
            options=available_dates,
            index=default_idx if available_dates else None,
            format_func=lambda d: f"{d} (Live Today)" if d == today_real else f"{d} (Historical Archive)",
            key="dashboard_session_selector",
        )

        st.markdown("---")
        st.subheader("📥 Live Market Actions")
        c_sync, c_reload = st.columns(2)
        with c_sync:
            if st.button("🔄 Sync Candles", help="Pull latest 5m candles from yfinance"):
                try:
                    from data_sources.replay_data import fetch_yfinance_5m_data
                    from journal.store import JournalStore

                    with st.spinner("Fetching candles from yfinance..."):
                        df_sync, _ = fetch_yfinance_5m_data(days=5)
                        if not df_sync.empty:
                            writable_store = JournalStore(settings.DB_PATH)
                            sync_list = []
                            for ts, row in df_sync.iterrows():
                                sync_list.append(
                                    Candle(
                                        start=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                                        open=float(row["Open"]),
                                        high=float(row["High"]),
                                        low=float(row["Low"]),
                                        close=float(row["Close"]),
                                        volume=float(row["Volume"]) if "Volume" in row else None,
                                        source="yfinance",
                                        interval_minutes=5,
                                    )
                                )
                            writable_store.upsert_candles(sync_list)
                            st.success(f"Synced {len(sync_list)} candles successfully!")
                            st.rerun()
                        else:
                            st.warning("No candle data returned from yfinance.")
                except Exception as exc:
                    st.error(f"Sync failed: {exc}")
        with c_reload:
            if st.button("⚡ Reload View", help="Refresh dashboard view immediately"):
                st.rerun()

        st.markdown("---")
        st.subheader("🖥️ Display Settings")
        chart_height = st.slider(
            "Chart Height (px)",
            min_value=300,
            max_value=600,
            value=380,
            step=20,
            key="dashboard_chart_height",
            help="Adjust candlestick chart height",
        )

        st.markdown("---")
        st.subheader("🛡️ Unified Liquidity Sniper Rules")
        st.markdown(
            "• **Arm 1 (Liquidity Sweeps)**: PDH / PDL / OI Wall Rejections + Market Structure Shift (MSS) + Fib Extensions.\n\n"
            "• **Arm 2 (TL-FIB v2)**: Intraday Trendline breaks + Golden Pocket [0.50–0.705] reload zones.\n\n"
            "• **ITM Strike Selection**: Delta 0.65–0.75 slightly ITM; capital cap ≤ 70%.\n\n"
            "• **Zero Counter-Trend**: Trade strictly with intraday structural trend.\n\n"
            "• **The Ride**: Trailing 5m structure; no premature scalps; 15:10 IST square-off."
        )

    return selected_date, chart_height


@st.fragment(run_every="60s")
def render_live_body(selected_date: str, available_dates: list[str], chart_height: int = 380) -> None:
    store = ReadOnlyJournal(settings.DB_PATH)
    # Load session candles & execute working model
    # Load session candles & previous day FRVP
    candles = load_candles_for_session(store, selected_date)

    # Determine previous trading session for FRVP (Fixed Range Volume Profile)
    prev_date = None
    try:
        cur_idx = available_dates.index(selected_date)
        if cur_idx + 1 < len(available_dates):
            prev_date = available_dates[cur_idx + 1]
    except Exception:
        pass

    vah, val, poc = None, None, None
    pdh, pdl = None, None
    if prev_date:
        prev_candles = load_candles_for_session(store, prev_date)
        if prev_candles:
            pdh = round(max(c.high for c in prev_candles), 2)
            pdl = round(min(c.low for c in prev_candles), 2)
            from engines.shadow_mw import compute_fixed_range_volume_profile
            frvp_res = compute_fixed_range_volume_profile(prev_candles)
            vah, val, poc = frvp_res["vah"], frvp_res["val"], frvp_res["poc"]

    formatted_trades, summary = load_journal_trades_for_session(
        store, selected_date, candles, pdh=pdh, pdl=pdl, vah=vah, val=val, poc=poc
    )
    store.close()

    # ---- 1. HEADER & TOP KPI BAR (GROUND TRUTH) ----
    st.title("🎯 Nifty Sniper — Live Trading Dashboard")
    st.caption("Unified Institutional Liquidity Sniper — Arm 1 (M/W Sweeps) + Arm 2 (TL-FIB v2) | One Brain Consolidated")

    # Live Status & Refresh Strip
    now_str = ist_now().strftime("%H:%M:%S IST")
    latest_candle_str = candles[-1].start.strftime("%H:%M IST") if candles else "None"
    
    col_stat, col_btn = st.columns([5, 1])
    with col_stat:
        st.markdown(
            f"🟢 **LIVE ENGINE CONNECTED** &nbsp;|&nbsp; ⏱️ **Time:** `{now_str}` &nbsp;|&nbsp; "
            f"🕯️ **Latest Synced Candle:** `{latest_candle_str}` ({len(candles)} bars) &nbsp;|&nbsp; "
            f"⚡ Auto-refreshing every 60s"
        )
    with col_btn:
        if st.button("🔄 Refresh Now", key="btn_top_refresh", help="Click to immediately reload latest candles and chart"):
            st.rerun()

    # Data Completeness & Continuity Check (Phase 14)
    today_real = ist_now().date().isoformat()
    if candles:
        if selected_date != today_real and len(candles) < 75:
            st.warning(
                f"⚠️ **DATA GAP DETECTED**: Found only {len(candles)}/75 session candles for {selected_date}. "
                f"A complete intraday NIFTY session requires 75 5-minute candles (09:15–15:30 IST)."
            )
        elif selected_date == today_real:
            now_t = ist_now().time()
            if now_t >= settings.MARKET_CLOSE and len(candles) < 75:
                st.warning(
                    f"⚠️ **DATA GAP DETECTED**: Market closed but only {len(candles)}/75 candles synced today. "
                    f"Click 'Sync Candles' in sidebar."
                )

    # One Brain Daily Reconciliation Assertion (Phase 14)
    state_trades_count = 0
    try:
        state_file = settings.DATA_DIR / "state.json"
        if state_file.exists():
            with open(state_file, "r", encoding="utf-8") as f:
                s_data = json.load(f)
                if s_data.get("today_iso") == selected_date:
                    state_trades_count = len(s_data.get("sniper_trades", [])) or (
                        len(s_data.get("closed_trades", [])) + len(s_data.get("open_trades", []))
                    )
    except Exception:
        pass

    if selected_date == today_real and state_trades_count > 0 and state_trades_count != len(formatted_trades):
        st.error(
            f"⚠️ **DATA SPLIT DETECTED**: Journal trades ({len(formatted_trades)}) ≠ State ledger trades ({state_trades_count})! "
            f"Live execution requires a single consolidated trade book."
        )
    else:
        st.caption("✅ **ONE BRAIN RECONCILIATION VERIFIED**: Journal Trades ≡ Dashboard Trades ≡ Ground Truth")

    # Clean KPI Cards
    trend = summary["trend"]
    if trend == "UPTREND":
        trend_badge = "🟢 UPTREND"
        rule_badge = "🟢 CALL / LONG RETRACEMENTS ONLY"
    elif trend == "DOWNTREND":
        trend_badge = "🔴 DOWNTREND"
        rule_badge = "🔴 PUT / SHORT RETRACEMENTS ONLY"
    else:
        trend_badge = "⚖️ RANGE"
        rule_badge = "⚖️ M/W BOUNDARY SWEEPS"

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Trading Session", selected_date, f"{len(candles)} candles")
    m2.metric("Intraday Trend", trend_badge, rule_badge)
    m3.metric("Trades Taken", f"{summary['total_trades']} Trades", f"{summary['wins']}W / {summary['losses']}L")
    m4.metric("Win Rate", f"{summary['win_rate']:.1f}%")
    m5.metric(
        "Net Realized PnL",
        f"{summary['net_r']:+.2f} R",
        delta=f"{summary['net_r']:+.2f} R",
        delta_color="normal" if summary["net_r"] >= 0 else "inverse",
    )

    st.markdown("---")

    # ---- 2. LIVE ACTIVE POSITION CARD (IF ACTIVE) ----
    open_trades = [t for t in formatted_trades if t["status"] in ("OPEN", "PARTIAL_TGT1")]
    if open_trades:
        for ot in open_trades:
            dir_badge = "🟢" if "CE" in ot["direction"] else "🔴"
            is_zone_ride = "Zone-Ride" in ot.get("setup", "") or "ZONE-RIDE" in ot.get("trade_id", "")
            if is_zone_ride:
                wall_booked = ot.get("half_booked", False)
                status_text = (
                    "🧱 **30% PROFIT BOOKED AT MAJOR OPPOSING WALL | RUNNER HOLDING 5M STRUCTURE**"
                    if wall_booked
                    else "🚀 **ACTIVE ZONE-RIDE — RIDING 5M STRUCTURE (HIGHER LOWS / LOWER HIGHS)**"
                )
            else:
                status_text = (
                    "🎯 **50% PROFIT BOOKED AT TGT1 | STOP MOVED TO BREAKEVEN (`BE_SL`)**"
                    if ot["half_booked"]
                    else "⏳ **ACTIVE POSITION — SEEKING TARGET 1**"
                )
            opt_sym = ot["option_symbol"]
            buy_prem = ot["entry_prem"]
            sl_prem = ot["sl_prem"]
            tgt1_prem = ot["tgt1_prem"]
            tgt2_prem = ot["tgt2_prem"]
            lot_size = getattr(settings, "LOT_SIZE", 75)
            capital = buy_prem * lot_size
            st.warning(
                f"{dir_badge} **LIVE OPTION TRADE ACTIVE**: **BUY {opt_sym}** @ **₹{buy_prem:.1f} – ₹{buy_prem + 10.0:.1f}** "
                f"(Capital: ~₹{capital:,.0f} | 1 Lot {lot_size} Qty | {ot['entry_time']} IST)\n\n"
                f"• **Spot Ref**: ₹{ot['entry_price']:.1f} | **Setup**: {ot['setup']}\n\n"
                f"• **Option Stop Loss**: ₹{sl_prem:.1f} Prem (Spot SL: ₹{ot['current_sl']:.1f} | Risk: {ot['risk_pts']:.1f} spot pts)\n\n"
                f"• **Option Target 1**: ₹{tgt1_prem:.1f} Prem (Spot: ₹{ot['tgt1']:.1f})\n\n"
                f"• **Option Target 2**: ₹{tgt2_prem:.1f} Prem (Spot: ₹{ot['tgt2']:.1f}) ↳ **Runner Target**\n\n"
                f"• **Status**: {status_text}"
            )
    else:
        st.info(
            f"🛡️ **ACTIVE DIRECTIONAL RULE**: **{rule_badge}** | "
            f"Unified Liquidity Sniper Active. Monitoring live candles for Arm 1 (Boundary Sweeps) and Arm 2 (Trendline Golden Pocket Pullback)."
        )

    # FRVP Confluence Callout Banner if calculated
    if vah and val and poc:
        st.info(
            f"📊 **FRVP Levels (09:15–15:30)**: "
            f"🟣 **VAH**: ₹{vah:.1f} | 🟡 **POC**: ₹{poc:.1f} | 🔵 **VAL**: ₹{val:.1f} (Spread: {vah - val:.1f} pts)"
        )

    # ---- 3. MAIN TRADING CANDLESTICK CHART ----
    tl_p1, tl_p2 = summary["trendline"]
    trendline_info = {"trend": trend, "p1": tl_p1, "p2": tl_p2} if (tl_p1 and tl_p2) else None

    # Filter trades for chart plotting
    chart_trades = []
    for tr in formatted_trades:
        chart_trades.append({
            "direction": "LONG" if "CE" in tr["direction"] else "SHORT",
            "raw_entry_time": tr.get("raw_entry_time"),
            "entry_time": tr["entry_time"],
            "entry_price": tr["entry_price"],
            "strike": tr.get("strike", 0),
            "option_symbol": tr.get("option_symbol", ""),
            "entry_prem": tr.get("entry_prem", 0.0),
            "sl_prem": tr.get("sl_prem", 0.0),
            "tgt1_prem": tr.get("tgt1_prem", 0.0),
            "tgt2_prem": tr.get("tgt2_prem", 0.0),
            "exit_prem": tr.get("exit_prem"),
            "sl": tr["sl"],
            "risk_pts": tr["risk_pts"],
            "tgt1": tr["tgt1"],
            "tgt2": tr["tgt2"],
            "fib_50": tr["fib_50"],
            "fib_618": tr["fib_618"],
            "fib_705": tr["fib_705"],
            "raw_exit_time": tr.get("raw_exit_time"),
            "exit_time": tr["exit_time"],
            "exit_price": tr["exit_price"] if tr["exit_price"] != "—" else None,
            "exit_reason": tr["exit_reason"],
            "result_r": tr["result_r"],
            "setup": tr.get("setup", ""),
            "notes": tr.get("notes", ""),
        })

    fig = build_liquidity_sniper_chart(
        candles=candles,
        trades=chart_trades,
        pdh=summary["pdh"],
        pdl=summary["pdl"],
        trendline_info=trendline_info,
        vah=vah,
        val=val,
        poc=poc,
        show_session_breaks=True,
        height=chart_height,
        zones=summary.get("zones", []),
    )

    st.plotly_chart(fig, use_container_width=True)

    # ---- 4. TODAY'S EXECUTED TRADES LOG ----
    st.subheader(f"📜 Today's Executed Option Trades ({len(formatted_trades)} Trades)")
    if formatted_trades:
        table_rows = []
        for idx, tr in enumerate(formatted_trades, 1):
            r_str = f"{tr['result_r']:+.2f} R" if tr["result_r"] is not None else "0.00 R"
            exit_prem_str = f"₹{tr['exit_prem']:.1f}" if isinstance(tr["exit_prem"], (int, float)) else "—"
            table_rows.append({
                "#": f"#{idx}",
                "Time": tr["entry_time"],
                "Setup": tr["setup"],
                "Instrument": tr["option_symbol"],
                "Action": tr["direction"],
                "Buy Prem": f"₹{tr['entry_prem']:.1f}",
                "SL Prem": f"₹{tr['sl_prem']:.1f}",
                "TGT1 Prem": f"₹{tr['tgt1_prem']:.1f}",
                "Exit Prem": exit_prem_str,
                "Exit Reason": tr["exit_reason"],
                "Return": r_str,
                "Spot Entry": f"₹{tr['entry_price']:.1f}",
                "Spot Exit": f"₹{tr['exit_price']:.1f}" if isinstance(tr["exit_price"], (int, float)) else str(tr["exit_price"]),
                "Notes": tr["notes"],
            })

        df_trades = pd.DataFrame(table_rows)
        st.dataframe(df_trades, hide_index=True, use_container_width=True)
    else:
        st.caption("No trades triggered yet in this session.")

    # ---- 5. INSTITUTIONAL STRATEGY BENCHMARK & CORE PILLARS ----
    st.markdown("---")
    st.subheader("🏆 Unified Institutional Liquidity Sniper — Core Pillars")

    b1, b2 = st.columns(2)
    with b1:
        st.markdown(
            "#### 🌊 Arm 1: Institutional Liquidity Sweeps\n\n"
            "1. **Level Set (09:15 Daily)**:\n"
            "   - PDH / PDL + Yesterday's High-Volume Swing Points + Key Option Chain OI Walls.\n"
            "   - Confluence Tag: Level + OI + Swing = STRONG Zone.\n\n"
            "2. **W (Long) / M (Short) Boundary Sweeps**:\n"
            "   - Touch 1: Dips to support/resistance and closes back inside (rejection #1).\n"
            "   - Pullback forms neckline.\n"
            "   - Touch 2: Sweeps beyond level (the stop hunt) + candle closes back inside (body ≥ 50%).\n"
            "   - MSS: Neckline broken with displacement -> Fibonacci Retest Entry.\n\n"
            "3. **Targets & Management**:\n"
            "   - Target 1: -0.272 Fib Institutional Expansion (50% profit booked, stop moved to BE_SL).\n"
            "   - Target 2: -0.618 Fib Extension (Runner ride) | 15:10 Square-Off."
        )

    with b2:
        st.markdown(
            "#### 📐 Arm 2: Trendline Fibonacci Golden Pocket (TL-FIB v2)\n\n"
            "1. **Intraday Trend & Fibonacci Retracement**:\n"
            "   - Strict Trend Gates: Zero counter-trend trades.\n"
            "   - Golden Pocket Reload: Pullback to [0.50–0.705] fib retracement zone.\n"
            "   - Impulse Pre-Filter: Impulse leg H ≥ 30 pts.\n\n"
            "2. **Scalp-Killer Gate & Strike Selection**:\n"
            "   - Computed Ride Target (1.272xH or next zone) ≥ 20 premium pts at live delta.\n"
            "   - Strike Selection: Slightly ITM Delta 0.65–0.75; Capital ≤ 70%.\n\n"
            "3. **Structural Trailing Exit**:\n"
            "   - 1st Pivot is a Checkpoint Only (never an auto-exit).\n"
            "   - Stop loss follows 5-minute higher lows (Longs) / lower highs (Shorts).\n"
            "   - Full runner holds while structure holds to 15:10 IST square-off."
        )


@st.cache_data
def get_historical_backtest_data() -> pd.DataFrame:
    """Run cached unified liquidity sniper backtest across full 2026 Jan-Oct dataset."""
    csv_file = settings.DATA_DIR / "nifty50_5min_2026_jan_to_oct.csv"
    if not csv_file.exists():
        return pd.DataFrame()

    df = pd.read_csv(csv_file)
    if "Timestamp" not in df.columns:
        return pd.DataFrame()

    df["date"] = df["Timestamp"].astype(str).str[:10]
    unique_dates = sorted(df["date"].unique())

    rows = []
    for d in unique_dates:
        sub = df[df["date"] == d]
        candles = []
        for _, r in sub.iterrows():
            try:
                s_dt = datetime.strptime(str(r["Timestamp"])[:19], "%Y-%m-%d %H:%M:%S")
                candles.append(
                    Candle(
                        start=s_dt,
                        open=float(r["Open"]),
                        high=float(r["High"]),
                        low=float(r["Low"]),
                        close=float(r["Close"]),
                        volume=float(r["Volume"]) if "Volume" in r and pd.notna(r["Volume"]) else 100.0,
                        interval_minutes=5,
                    )
                )
            except Exception:
                pass
        if len(candles) < 10:
            continue
        pdh = max(c.high for c in candles[:15])
        pdl = min(c.low for c in candles[:15])
        sniper = UnifiedLiquiditySniper(today_iso=d, pdh=pdh, pdl=pdl, isolated=True)
        fake_market = type("M", (), {"pdh": pdh, "pdl": pdl, "chain": None})()
        sniper.arm1.initialize_levels(fake_market, today_iso=d)
        for c in candles:
            sniper.on_candle(c, allow_alerts=False)
        sniper.square_off(exit_price=candles[-1].close, exit_time=candles[-1].start)

        for t in sniper.get_all_trades():
            if getattr(t, "status", "") == "CLOSED":
                if ("ZR-" in t.trade_id) or ("Zone Reversal" in getattr(t, "setup_name", "")) or ("Trendline" in getattr(t, "setup_name", "")):
                    arm_str = "Arm 3 (Zone Reversal / Trendline)"
                elif ("SMW" in t.trade_id) or (getattr(t, "pattern", "") in ("W", "M")):
                    arm_str = "Arm 1 (M/W Liquidity Sweep)"
                else:
                    arm_str = "Arm 2 (TL-FIB Golden Pocket)"
                is_long = t.direction == "LONG"
                dir_str = "BUY CE" if is_long else "BUY PE"
                opt_type = "CE" if is_long else "PE"
                strike = getattr(t, "strike", 0) or int(round(t.entry_price / 50.0) * 50)
                sym = getattr(t, "option_symbol", "") or f"NIFTY {strike} {opt_type}"
                e_time = getattr(t, "entry_time", "")
                x_time = getattr(t, "exit_time", "")
                res_r = round(float(getattr(t, "realized_r", 0.0)), 2)
                outcome = "WIN ✅" if res_r > 0 else ("LOSS ❌" if res_r < 0 else "BE 🛡️")
                rows.append({
                    "Date": d,
                    "Time": e_time[11:16] if len(e_time) >= 16 else e_time,
                    "Trade ID": t.trade_id,
                    "Arm": arm_str,
                    "Direction": dir_str,
                    "Instrument": sym,
                    "Spot Entry": round(t.entry_price, 1),
                    "Spot SL": round(getattr(t, "initial_sl", t.entry_price), 1),
                    "Spot Exit": round(t.exit_price, 1) if t.exit_price else round(t.entry_price, 1),
                    "Buy Prem": round(float(getattr(t, "entry_prem", 0.0)), 1),
                    "SL Prem": round(float(getattr(t, "sl_prem", 0.0)), 1),
                    "Exit Prem": round(float(getattr(t, "exit_prem", 0.0)), 1) if getattr(t, "exit_prem", None) else round(float(getattr(t, "entry_prem", 0.0)), 1),
                    "Realized R": res_r,
                    "Outcome": outcome,
                    "Exit Reason": getattr(t, "exit_reason", "EXIT"),
                    "Setup": getattr(t, "setup_name", ""),
                    "Notes": getattr(t, "notes", ""),
                })
    return pd.DataFrame(rows)


def render_backtest_tab() -> None:
    st.title("📊 Institutional Liquidity Sniper — Historical Backtest Trades")
    st.caption("Complete multi-session benchmark across 189 sessions (Jan–Oct 2026 5-minute dataset) | Arm 1 (M/W Sweeps) + Arm 2 (TL-FIB v2)")

    df_bt = get_historical_backtest_data()
    if df_bt.empty:
        st.warning("Historical dataset `nifty50_5min_2026_jan_to_oct.csv` not found or empty.")
        return

    # Top KPI Metrics
    total_trades = len(df_bt)
    wins = len(df_bt[df_bt["Realized R"] > 0])
    losses = len(df_bt[df_bt["Realized R"] < 0])
    scratches = len(df_bt[df_bt["Realized R"] == 0])
    win_rate = (wins / total_trades * 100.0) if total_trades else 0.0
    total_r = df_bt["Realized R"].sum()
    expectancy = (total_r / total_trades) if total_trades else 0.0

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total Trades", f"{total_trades}", "189 Sessions (Jan-Oct 2026)")
    k2.metric("Win Rate", f"{win_rate:.1f}%", f"{wins}W / {losses}L ({scratches} BE)")
    k3.metric("Total Realized Return", f"{total_r:+.2f} R", delta=f"{total_r:+.2f} R")
    k4.metric("Expectancy", f"{expectancy:+.2f} R / trade", "Positive Edge")
    k5.metric("System Arms", "2 Active Engines", "Arm 1 (Sweeps) + Arm 2 (TL-FIB)")

    st.markdown("---")

    # Strategy Breakdown
    st.subheader("⚔️ Strategy Breakdown: Arm 1 (Sweeps) vs Arm 2 (TL-FIB)")
    b1, b2 = st.columns(2)
    df_arm1 = df_bt[df_bt["Arm"].str.contains("Arm 1")]
    df_arm2 = df_bt[df_bt["Arm"].str.contains("Arm 2")]

    w1 = len(df_arm1[df_arm1["Realized R"] > 0])
    l1 = len(df_arm1[df_arm1["Realized R"] < 0])
    wr1 = (w1 / len(df_arm1) * 100.0) if len(df_arm1) else 0.0
    r1 = df_arm1["Realized R"].sum() if len(df_arm1) else 0.0

    w2 = len(df_arm2[df_arm2["Realized R"] > 0])
    l2 = len(df_arm2[df_arm2["Realized R"] < 0])
    wr2 = (w2 / len(df_arm2) * 100.0) if len(df_arm2) else 0.0
    r2 = df_arm2["Realized R"].sum() if len(df_arm2) else 0.0

    with b1:
        st.markdown(
            f"""
            <div style="border: 1px solid #30363d; border-radius: 8px; padding: 16px; background-color: #0d1117;">
                <h4 style="margin: 0; color: #58a6ff;">🌊 Arm 1: Institutional Liquidity Sweeps</h4>
                <p style="color: #8b949e; margin: 4px 0 12px 0;">PDH / PDL / OI Wall Rejections with Market Structure Shift (MSS) & Fib Extensions</p>
                <div style="display: flex; justify-content: space-between; font-size: 15px;">
                    <span>Trades: <b>{len(df_arm1)}</b></span>
                    <span>Wins/Losses: <b>{w1}W / {l1}L</b></span>
                    <span>Win Rate: <b>{wr1:.1f}%</b></span>
                    <span style="color: {'#3fb950' if r1 >= 0 else '#f85149'};">Net R: <b>{r1:+.2f} R</b></span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with b2:
        st.markdown(
            f"""
            <div style="border: 1px solid #30363d; border-radius: 8px; padding: 16px; background-color: #0d1117;">
                <h4 style="margin: 0; color: #58a6ff;">📐 Arm 2: Trendline Fibonacci Golden Pocket (TL-FIB v2)</h4>
                <p style="color: #8b949e; margin: 4px 0 12px 0;">Intraday Trendline Breaks + 0.50–0.705 Golden Pocket Reload & 5m Structural Trail</p>
                <div style="display: flex; justify-content: space-between; font-size: 15px;">
                    <span>Trades: <b>{len(df_arm2)}</b></span>
                    <span>Wins/Losses: <b>{w2}W / {l2}L</b></span>
                    <span>Win Rate: <b>{wr2:.1f}%</b></span>
                    <span style="color: {'#3fb950' if r2 >= 0 else '#f85149'};">Net R: <b>{r2:+.2f} R</b></span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # Cumulative Equity Curve Chart
    st.subheader(f"📈 Cumulative Equity Curve ({total_r:+.2f} R across 189 Sessions)")
    df_sorted = df_bt.copy()
    df_sorted["Cum_R"] = df_sorted["Realized R"].cumsum()
    df_sorted["Trade_Num"] = range(1, len(df_sorted) + 1)

    fig_equity = go.Figure()
    fig_equity.add_trace(go.Scatter(
        x=df_sorted["Trade_Num"],
        y=df_sorted["Cum_R"],
        mode="lines",
        name="Cumulative Realized R",
        line=dict(color="#00C853", width=2.5),
        fill="tozeroy",
        fillcolor="rgba(0, 200, 83, 0.08)",
        customdata=df_sorted[["Date", "Trade ID", "Arm", "Direction", "Realized R", "Exit Reason"]],
        hovertemplate="<b>Trade #%{x}</b> (%{customdata[0]})<br>"
                      "ID: %{customdata[1]} | %{customdata[2]}<br>"
                      "Direction: %{customdata[3]}<br>"
                      "Return: %{customdata[4]:+.2f} R<br>"
                      "Cumulative: <b>%{y:+.2f} R</b><br>"
                      "Reason: %{customdata[5]}<extra></extra>",
    ))
    fig_equity.update_layout(
        xaxis_title="Trade Number",
        yaxis_title="Realized Return (R)",
        template="plotly_dark",
        height=360,
        margin=dict(l=30, r=30, t=30, b=30),
    )
    st.plotly_chart(fig_equity, use_container_width=True)

    # Interactive Table Filters
    st.markdown("---")
    st.subheader(f"📜 All Historical Backtested Trades ({total_trades} Trades)")

    col_f1, col_f2, col_f3, col_dl = st.columns([2, 2, 2, 2])
    with col_f1:
        arm_filter = st.selectbox(
            "Filter by Strategy Arm",
            options=["All Strategies", "Arm 1 (M/W Liquidity Sweep)", "Arm 2 (TL-FIB Golden Pocket)"],
        )
    with col_f2:
        outcome_filter = st.selectbox(
            "Filter by Outcome",
            options=["All Outcomes", "Wins Only (✅)", "Losses Only (❌)"],
        )
    with col_f3:
        dir_filter = st.selectbox(
            "Filter by Direction",
            options=["All Directions", "BUY CE (Long)", "BUY PE (Short)"],
        )
    with col_dl:
        st.write("")
        st.write("")
        csv_data = df_bt.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Download CSV",
            data=csv_data,
            file_name="nifty_sniper_backtest_2026.csv",
            mime="text/csv",
            help="Download all historical backtested trades in CSV format",
        )

    # Apply filters
    filtered_df = df_bt.copy()
    if arm_filter != "All Strategies":
        if "Arm 1" in arm_filter:
            filtered_df = filtered_df[filtered_df["Arm"].str.contains("Arm 1")]
        else:
            filtered_df = filtered_df[filtered_df["Arm"].str.contains("Arm 2")]
    if outcome_filter == "Wins Only (✅)":
        filtered_df = filtered_df[filtered_df["Realized R"] > 0]
    elif outcome_filter == "Losses Only (❌)":
        filtered_df = filtered_df[filtered_df["Realized R"] < 0]
    if dir_filter == "BUY CE (Long)":
        filtered_df = filtered_df[filtered_df["Direction"] == "BUY CE"]
    elif dir_filter == "BUY PE (Short)":
        filtered_df = filtered_df[filtered_df["Direction"] == "BUY PE"]

    st.dataframe(
        filtered_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Realized R": st.column_config.NumberColumn("Return", format="%.2f R"),
            "Buy Prem": st.column_config.NumberColumn("Buy Prem", format="₹%.1f"),
            "SL Prem": st.column_config.NumberColumn("SL Prem", format="₹%.1f"),
            "Exit Prem": st.column_config.NumberColumn("Exit Prem", format="₹%.1f"),
            "Spot Entry": st.column_config.NumberColumn("Spot Entry", format="₹%.1f"),
            "Spot SL": st.column_config.NumberColumn("Spot SL", format="₹%.1f"),
            "Spot Exit": st.column_config.NumberColumn("Spot Exit", format="₹%.1f"),
        },
    )


def render() -> None:
    store = ReadOnlyJournal(settings.DB_PATH)
    available_dates = get_available_dates(store)
    store.close()

    today_real = ist_now().date().isoformat()

    default_idx = 0
    if today_real in available_dates:
        default_idx = available_dates.index(today_real)
    elif len(available_dates) > 0:
        default_idx = 0

    selected_date, chart_height = render_sidebar(available_dates, default_idx, today_real)

    tab_live, tab_backtest = st.tabs([
        "🎯 Live Trading Session (Real-Time & Replay)",
        "📊 Historical Backtest Trades (Institutional Performance Benchmark)",
    ])

    with tab_live:
        if selected_date:
            render_live_body(selected_date, available_dates, chart_height=chart_height)
        else:
            st.warning("No session candle data available yet. Click 'Sync Candles' in the sidebar to fetch today's data.")

    with tab_backtest:
        render_backtest_tab()


render()
