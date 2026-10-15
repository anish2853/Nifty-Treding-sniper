"""Plotly Candlestick Chart Engine for Nifty Sniper Live Trading Dashboard.

Renders:
1. 5-Minute Nifty Candlesticks with Session Breaks.
2. PDH, PDL, and Previous Session FRVP Levels (VAH, VAL, POC).
3. Session Trendline (Uptrend / Downtrend).
4. Trade Setups:
   - Upside Trigger: Green ▲ (triangle-up) + '🟢 BUY CE' candle annotation.
   - Downside Trigger: Red ▼ (triangle-down) + '🔴 BUY PE' candle annotation.
   - Fibonacci Golden Pocket [0.50 – 0.618] shaded zone.
   - Stop Loss, Target 1, Target 2 lines.
   - Exit Marker: 🏁 EXIT 'x' with realized R-multiple annotation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, date
from typing import Any

import plotly.graph_objects as go
import settings
from engines.candles import Candle

log = logging.getLogger("dashboard.chart")


def _normalize_dt(dt_val: Any, base_date: date | None = None) -> datetime | None:
    """Safely normalizes various timestamp formats into a comparable datetime."""
    if not dt_val or dt_val == "—":
        return None
    if isinstance(dt_val, datetime):
        return dt_val.replace(tzinfo=None)
    if isinstance(dt_val, str):
        try:
            parsed = datetime.fromisoformat(dt_val)
            return parsed.replace(tzinfo=None)
        except Exception:
            pass
        if len(dt_val) >= 5 and ":" in dt_val and base_date is not None:
            try:
                hh, mm = map(int, dt_val[:5].split(":"))
                return datetime(base_date.year, base_date.month, base_date.day, hh, mm)
            except Exception:
                pass
    return None


def build_liquidity_sniper_chart(
    candles: list[Candle],
    trades: list[dict],
    pdh: float | None = None,
    pdl: float | None = None,
    trendline_info: dict | None = None,
    vah: float | None = None,
    val: float | None = None,
    poc: float | None = None,
    show_session_breaks: bool = True,
    height: int = 400,
    zones: list[dict] | None = None,
) -> go.Figure:
    """Build ultra-clean Plotly candlestick chart with upside/downside entry symbols, FRVP, and Golden Pocket."""
    if not candles:
        fig = go.Figure()
        fig.add_annotation(
            text="No candle data available for this session",
            showarrow=False,
            font=dict(size=16, color="gray"),
        )
        fig.update_layout(template="plotly_dark", height=height)
        return fig

    fig = go.Figure()

    # Normalize candle timestamps to naive datetime to prevent tz discrepancies
    x_candles = [c.start.replace(tzinfo=None) if hasattr(c.start, "replace") else c.start for c in candles]
    session_date = candles[0].start.date() if hasattr(candles[0].start, "date") else None

    # Calculate session time bounds so early-morning candles never balloon into giant blocks
    if session_date is not None:
        session_start = datetime(session_date.year, session_date.month, session_date.day, 9, 10)
        session_end = datetime(session_date.year, session_date.month, session_date.day, 15, 35)
        view_start = session_start
        if len(candles) <= 12:  # First hour: comfortable 2.5h window
            view_end = datetime(session_date.year, session_date.month, session_date.day, 11, 45)
        elif len(candles) <= 30:  # Mid-morning: up to 13:30
            view_end = datetime(session_date.year, session_date.month, session_date.day, 13, 30)
        else:
            view_end = session_end
    else:
        session_start = x_candles[0] - timedelta(minutes=5)
        session_end = x_candles[-1] + timedelta(minutes=5)
        view_start = session_start
        view_end = session_end

    # 1. Main Candlestick trace
    fig.add_trace(go.Candlestick(
        x=x_candles,
        open=[c.open for c in candles],
        high=[c.high for c in candles],
        low=[c.low for c in candles],
        close=[c.close for c in candles],
        name="NIFTY 5m",
        increasing_line_color="#00e676",
        decreasing_line_color="#ff5252",
    ))

    # 2. Reference lines span the entire session forward
    x_start = session_start
    x_end = session_end

    if pdh is not None and pdh > 0:
        fig.add_shape(
            type="line", x0=x_start, x1=x_end, y0=pdh, y1=pdh,
            line=dict(color="rgba(244, 67, 54, 0.75)", width=1.5, dash="dash"),
        )
        fig.add_annotation(
            x=view_start, y=pdh, text=f"🔴 PDH: {pdh:.1f}",
            showarrow=False, xanchor="left", yanchor="bottom",
            font=dict(size=10, color="#f44336"),
        )

    if pdl is not None and pdl > 0:
        fig.add_shape(
            type="line", x0=x_start, x1=x_end, y0=pdl, y1=pdl,
            line=dict(color="rgba(76, 175, 80, 0.75)", width=1.5, dash="dash"),
        )
        fig.add_annotation(
            x=view_start, y=pdl, text=f"🟢 PDL: {pdl:.1f}",
            showarrow=False, xanchor="left", yanchor="top",
            font=dict(size=10, color="#4caf50"),
        )

    # 2b. Fixed Range Volume Profile (FRVP) Levels Extended for Today
    if vah is not None and vah > 0:
        fig.add_shape(
            type="line", x0=x_start, x1=x_end, y0=vah, y1=vah,
            line=dict(color="#ba68c8", width=1.5, dash="dashdot"),
        )
        fig.add_annotation(
            x=view_end, y=vah, text=f"🟣 VAH: {vah:.1f} (Value Area High 70%)",
            showarrow=False, xanchor="right", yanchor="bottom",
            font=dict(size=10, color="#ba68c8"),
        )

    if poc is not None and poc > 0:
        fig.add_shape(
            type="line", x0=x_start, x1=x_end, y0=poc, y1=poc,
            line=dict(color="#ffca28", width=1.5, dash="dot"),
        )
        fig.add_annotation(
            x=view_end, y=poc, text=f"🟡 POC: {poc:.1f} (Point of Control)",
            showarrow=False, xanchor="right", yanchor="bottom",
            font=dict(size=10, color="#ffca28"),
        )

    if val is not None and val > 0:
        fig.add_shape(
            type="line", x0=x_start, x1=x_end, y0=val, y1=val,
            line=dict(color="#26c6da", width=1.5, dash="dashdot"),
        )
        fig.add_annotation(
            x=view_end, y=val, text=f"🔵 VAL: {val:.1f} (Value Area Low 70%)",
            showarrow=False, xanchor="right", yanchor="top",
            font=dict(size=10, color="#26c6da"),
        )

    # 2b. Phase 15 STRONG Liquidity Zones: Only include zones within proximate trading range
    # to avoid blowing out the Y-axis (which flattens the 5m candles into invisible lines)
    min_candle_low = min(c.low for c in candles)
    max_candle_high = max(c.high for c in candles)
    prox_padding = max(100.0, (max_candle_high - min_candle_low) * 0.40)
    visible_y_min = min_candle_low - prox_padding
    visible_y_max = max_candle_high + prox_padding

    # Expand slightly for key reference levels if they are reasonably close (< 250 pts)
    for ref_level in [pdh, pdl, vah, val, poc]:
        if ref_level and ref_level > 0 and abs(ref_level - ((min_candle_low + max_candle_high) / 2)) <= 350.0:
            visible_y_min = min(visible_y_min, ref_level - 20.0)
            visible_y_max = max(visible_y_max, ref_level + 20.0)

    if zones:
        for z in zones:
            z_low = float(z.get("zone_low", 0.0))
            z_high = float(z.get("zone_high", 0.0))
            if z_low <= 0 or z_high <= 0:
                continue
            # Only draw zones that are near the active trading action
            if z_high < visible_y_min or z_low > visible_y_max:
                continue

            z_type = z.get("zone_type", "SUPPORT")
            k = z.get("anchor_strike", int((z_low + z_high) / 2))
            is_sup = (z_type == "SUPPORT")
            fill_col = "rgba(76, 175, 80, 0.12)" if is_sup else "rgba(244, 67, 54, 0.12)"
            line_col = "rgba(76, 175, 80, 0.45)" if is_sup else "rgba(244, 67, 54, 0.45)"
            tag = z.get("tag", "STRONG")
            txt = f"🟢 SUPPORT {k} [{tag}]" if is_sup else f"🔴 RESISTANCE {k} [{tag}]"

            fig.add_shape(
                type="rect",
                x0=x_start,
                x1=x_end,
                y0=z_low,
                y1=z_high,
                fillcolor=fill_col,
                line=dict(color=line_col, width=1, dash="dot"),
            )
            fig.add_annotation(
                x=view_start,
                y=(z_low + z_high) / 2,
                text=txt,
                showarrow=False,
                xanchor="left",
                yanchor="middle",
                font=dict(size=9, color="#81c784" if is_sup else "#e57373"),
            )

    # 2c. Session Breaks (09:15 Open Vertical Marker)
    if show_session_breaks:
        seen_dates = set()
        for idx, c in enumerate(candles):
            c_date = c.start.date() if hasattr(c.start, "date") else None
            if c_date and c_date not in seen_dates:
                seen_dates.add(c_date)
                fig.add_vline(
                    x=x_candles[idx],
                    line_width=1,
                    line_dash="dot",
                    line_color="rgba(144, 202, 249, 0.45)",
                    annotation_text="🔔 09:15 OPEN",
                    annotation_position="top left",
                    annotation_font=dict(size=9, color="#90caf9"),
                )

    # 3. Dynamic Session Trendline
    if trendline_info:
        tl_trend = trendline_info.get("trend", "RANGE")
        p1 = trendline_info.get("p1")
        p2 = trendline_info.get("p2")
        if p1 and p2:
            p1_x = p1[0].replace(tzinfo=None) if hasattr(p1[0], "replace") else p1[0]
            p2_x = p2[0].replace(tzinfo=None) if hasattr(p2[0], "replace") else p2[0]
            tl_color = "#ef5350" if tl_trend == "DOWNTREND" else "#26a69a"
            fig.add_trace(go.Scatter(
                x=[p1_x, p2_x],
                y=[p1[1], p2[1]],
                mode="lines",
                line=dict(color=tl_color, width=2, dash="dash"),
                name=f"Trendline ({tl_trend})",
                hoverinfo="text",
                hovertext=f"📐 Session Trendline ({tl_trend}): {p1[1]:.1f} ➔ {p2[1]:.1f}",
            ))

    # 4. Add Upside/Downside Entry Symbols, Golden Pocket, SL, Targets, and Exits
    for tr in trades:
        entry_time = _normalize_dt(tr.get("raw_entry_time") or tr.get("entry_time"), session_date)
        if not entry_time:
            continue

        exit_time = _normalize_dt(tr.get("raw_exit_time") or tr.get("exit_time"), session_date) or x_end

        direction = tr.get("direction", "LONG")
        is_long = ("LONG" in direction or "CE" in direction)
        action_label = "BUY CE" if is_long else "BUY PE"
        marker_sym = "triangle-up" if is_long else "triangle-down"
        marker_col = "#00e676" if is_long else "#ff1744"

        opt_symbol = tr.get("option_symbol") or f"NIFTY {tr.get('strike', '')} {'CE' if is_long else 'PE'}"
        entry_px = float(tr.get("entry_price", 0.0))
        sl_val = float(tr.get("sl", 0.0))
        tgt1_val = float(tr.get("tgt1", 0.0))
        tgt2_val = float(tr.get("tgt2", 0.0))
        exit_px = float(tr.get("exit_price", entry_px)) if tr.get("exit_price") not in ("—", None) else None
        result_r = float(tr.get("result_r", 0.0)) if tr.get("result_r") is not None else 0.0
        exit_reason = tr.get("exit_reason", "")
        setup_name = tr.get("setup", "Fibonacci Setup")
        f50 = float(tr.get("fib_50", 0.0))
        f618 = float(tr.get("fib_618", 0.0))
        f705 = float(tr.get("fib_705", 0.0))

        # Shaded Fibonacci Golden Pocket zone
        if f50 > 0 and f618 > 0:
            box_low = min(f50, f618, f705 if f705 > 0 else f618)
            box_high = max(f50, f618, f705 if f705 > 0 else f618)
            fig.add_shape(
                type="rect",
                x0=entry_time - timedelta(minutes=15),
                x1=exit_time,
                y0=box_low,
                y1=box_high,
                fillcolor="rgba(255, 215, 0, 0.09)",
                line=dict(color="rgba(255, 215, 0, 0.35)", width=1, dash="dot"),
            )

        # Stop Loss horizontal line segment
        if sl_val > 0:
            fig.add_trace(go.Scatter(
                x=[entry_time, exit_time],
                y=[sl_val, sl_val],
                mode="lines",
                line=dict(color="rgba(255, 82, 82, 0.7)", width=1.5, dash="dot"),
                name="Stop Loss",
                hoverinfo="text",
                hovertext=f"🛑 Stop Loss: ₹{sl_val:.1f} (Risk: {tr.get('risk_pts', 0):.1f} pts)",
                showlegend=False,
            ))

        # Target 1 horizontal line segment (Prior Pivot)
        if tgt1_val > 0:
            fig.add_trace(go.Scatter(
                x=[entry_time, exit_time],
                y=[tgt1_val, tgt1_val],
                mode="lines",
                line=dict(color="rgba(255, 235, 59, 0.8)", width=1.5, dash="dash"),
                name="Target 1",
                hoverinfo="text",
                hovertext=f"🎯 Target 1: ₹{tgt1_val:.1f} (50% Take Profit + Breakeven Lock)",
                showlegend=False,
            ))

        # Target 2 horizontal line segment (-0.618 Fib Extension)
        if tgt2_val > 0:
            fig.add_trace(go.Scatter(
                x=[entry_time, exit_time],
                y=[tgt2_val, tgt2_val],
                mode="lines",
                line=dict(color="rgba(0, 229, 255, 0.8)", width=1.5, dash="dash"),
                name="Target 2",
                hoverinfo="text",
                hovertext=f"🚀 Target 2: ₹{tgt2_val:.1f} (Runner Target)",
                showlegend=False,
            ))

        # ENTRY TRIGGER SYMBOL (Upside ▲ or Downside ▼)
        fig.add_trace(go.Scatter(
            x=[entry_time],
            y=[entry_px],
            mode="markers",
            marker=dict(
                size=16,
                color=marker_col,
                symbol=marker_sym,
                line=dict(width=2, color="#ffffff"),
            ),
            name=f"ENTRY ({action_label})",
            hovertext=(
                f"<b>🎯 {setup_name}</b><br>"
                f"Action: <b>{'🟢' if is_long else '🔴'} {action_label}</b> ({opt_symbol})<br>"
                f"Entry Time: {entry_time.strftime('%H:%M')} IST<br>"
                f"Spot Entry: ₹{entry_px:.1f}<br>"
                f"Option Buy Prem: ₹{tr.get('entry_prem', 0):.1f}<br>"
                f"Stop Loss: ₹{sl_val:.1f} (SL Prem: ₹{tr.get('sl_prem', 0):.1f})<br>"
                f"Target 1: ₹{tgt1_val:.1f} (TGT1 Prem: ₹{tr.get('tgt1_prem', 0):.1f})<br>"
                f"Target 2: ₹{tgt2_val:.1f} (TGT2 Prem: ₹{tr.get('tgt2_prem', 0):.1f})<br>"
            ),
            hoverinfo="text",
            showlegend=False,
        ))

        # Text banner annotation on candle
        ay_offset = 35 if is_long else -35
        fig.add_annotation(
            x=entry_time,
            y=entry_px,
            text=f"<b>{'🟢' if is_long else '🔴'} {action_label}</b><br>{opt_symbol}<br>@ ₹{entry_px:.1f}",
            showarrow=True,
            arrowhead=2,
            arrowsize=1.2,
            arrowwidth=2,
            arrowcolor=marker_col,
            ax=0,
            ay=ay_offset,
            bgcolor="rgba(18, 22, 34, 0.90)",
            bordercolor=marker_col,
            borderwidth=1.5,
            borderpad=4,
            font=dict(size=10, color="#ffffff"),
        )

        # EXIT TRIGGER SYMBOL (🏁 'x' marker + outcome annotation)
        if exit_px is not None and exit_px > 0 and exit_time:
            exit_col = "#00e676" if result_r > 0 else ("#ffa726" if result_r == 0 else "#ff5252")
            fig.add_trace(go.Scatter(
                x=[exit_time],
                y=[exit_px],
                mode="markers",
                marker=dict(size=14, color=exit_col, symbol="x", line=dict(width=2.5, color=exit_col)),
                name="EXIT",
                hovertext=(
                    f"<b>🏁 EXIT: {exit_reason}</b><br>"
                    f"Exit Time: {exit_time.strftime('%H:%M')} IST<br>"
                    f"Exit Price: ₹{exit_px:.1f}<br>"
                    f"Realized Return: <b>{result_r:+.2f} R</b><br>"
                    f"Exit Prem: ₹{tr.get('exit_prem', 0):.1f}<br>"
                    f"Notes: {tr.get('notes', '—')}"
                ),
                hoverinfo="text",
                showlegend=False,
            ))
            fig.add_annotation(
                x=exit_time,
                y=exit_px,
                text=f"<b>🏁 EXIT ({result_r:+.2f}R)</b>",
                showarrow=True,
                arrowhead=2,
                arrowsize=1,
                arrowwidth=1.5,
                arrowcolor=exit_col,
                ax=0,
                ay=-28 if is_long else 28,
                bgcolor="rgba(18, 22, 34, 0.90)",
                bordercolor=exit_col,
                borderwidth=1,
                font=dict(size=9, color=exit_col),
            )

    trend_title = trendline_info.get("trend", "") if trendline_info else ""
    title_suffix = f" — Intraday Trend: {trend_title}" if trend_title else ""
    fig.update_layout(
        title=f"🎯 Institutional Liquidity & Fibonacci Sniper{title_suffix} ({len(candles)} candles)",
        xaxis_title="Time (IST)",
        yaxis_title="NIFTY Spot Price",
        template="plotly_dark",
        height=height,
        margin=dict(l=40, r=40, t=50, b=40),
        xaxis_rangeslider_visible=False,
    )
    fig.update_xaxes(
        range=[view_start, view_end],
        rangeslider_visible=False,
    )
    fig.update_yaxes(
        range=[visible_y_min, visible_y_max],
        autorange=False,
    )
    return fig
