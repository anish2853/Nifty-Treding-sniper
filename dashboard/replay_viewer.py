"""Interactive Streamlit replay viewer with play/pause/speed controls and manual marking.

Features:
- Day selector for replayed dates
- Playback controls: play, pause, 1x-10x speed, step-one-candle
- Animated candle rendering during playback
- Signal markers (🎯 entries, 🚨 exits, ● near-misses)
- Hover tooltips with trade details
- Manual marking: click to mark "I would enter here" + direction
- Day-end comparison: YOUR marks vs ENGINE signals
- Day-end summary card: trades, state timeline, near-misses

REQUIREMENTS:
- pip install streamlit plotly pandas
"""

try:
    import streamlit as st
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    import pandas as pd
except ImportError as e:
    print(f"ERROR: Missing required package: {e}")
    print("Install with: pip install streamlit plotly pandas")
    exit(1)

from datetime import date, datetime, timedelta
import logging
from pathlib import Path
import sys

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings
from engines.replay_mtf import _replay_day, DayReplay
from engines.candles import Candle

log = logging.getLogger("replay_viewer")


def setup_page():
    """Configure Streamlit page."""
    st.set_page_config(
        page_title="Strategy Replay Viewer",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("📊 Strategy Replay Viewer")
    st.markdown("Interactive replay with playback controls, manual marking, and performance comparison.")


def get_trading_days(days_back: int = 60) -> list[date]:
    """Get list of recent trading days (exclude weekends)."""
    today = date.today()
    trading_days = []
    current = today
    
    while len(trading_days) < days_back:
        if current.weekday() < 5:  # Mon-Fri
            trading_days.append(current)
        current -= timedelta(days=1)
    
    return sorted(trading_days, reverse=True)


@st.cache_data
def load_replay(replay_date: date) -> DayReplay | None:
    """Load cached replay for a day."""
    try:
        return _replay_day(replay_date)
    except Exception as e:
        st.error(f"Error loading replay for {replay_date}: {e}")
        return None


def build_candle_chart(
    candles: list[Candle],
    trades: list,
    current_idx: int | None = None,
    manual_marks: list[dict] | None = None,
) -> go.Figure:
    """Build Plotly candlestick chart with trade markers."""
    if not candles:
        return go.Figure()
    
    # Display candles up to current playback position
    display_candles = candles[:current_idx + 1] if current_idx is not None else candles
    
    fig = go.Figure()
    
    # Add candlestick
    fig.add_trace(go.Candlestick(
        x=[c.end for c in display_candles],
        open=[c.open for c in display_candles],
        high=[c.high for c in display_candles],
        low=[c.low for c in display_candles],
        close=[c.close for c in display_candles],
        name="NIFTY 5m",
    ))
    
    # Add trade entry markers
    for trade in trades:
        if not trade.get("entry_time"):
            continue
        entry_time = trade["entry_time"]
        if isinstance(entry_time, str):
            entry_time = datetime.fromisoformat(entry_time)
        
        # Only show if within display range
        if display_candles and entry_time > display_candles[-1].end:
            continue
        
        marker_symbol = "triangle-up" if trade["direction"] == "LONG" else "triangle-down"
        marker_color = "green" if trade["direction"] == "LONG" else "red"
        
        fig.add_trace(go.Scatter(
            x=[entry_time],
            y=[trade["entry"]],
            mode="markers",
            marker=dict(size=12, color=marker_color, symbol=marker_symbol),
            name=f"Entry {trade['family']}",
            hovertext=f"{trade['family']}<br>{trade['direction']}<br>Entry: {trade['entry']:.2f}",
            hoverinfo="text",
        ))
    
    # Add manual marks
    if manual_marks:
        for mark in manual_marks:
            mark_time = mark["time"]
            if isinstance(mark_time, str):
                mark_time = datetime.fromisoformat(mark_time)
            
            if display_candles and mark_time > display_candles[-1].end:
                continue
            
            marker_color = "lightgreen" if mark["direction"] == "LONG" else "lightyellow"
            
            fig.add_trace(go.Scatter(
                x=[mark_time],
                y=[mark["price"]],
                mode="markers+text",
                marker=dict(size=10, color=marker_color, symbol="star"),
                text="★",
                textposition="top center",
                name=f"Manual Mark",
                hovertext=f"MY MARK: {mark['direction']}<br>Price: {mark['price']:.2f}",
                hoverinfo="text",
            ))
    
    fig.update_layout(
        title=f"Candle Replay (showing {len(display_candles)} of {len(candles)} candles)",
        xaxis_title="Time",
        yaxis_title="Price",
        template="plotly_dark",
        hovermode="x unified",
        height=500,
    )
    
    return fig


def main():
    """Main Streamlit app."""
    setup_page()
    
    # Sidebar: Date selector
    with st.sidebar:
        st.header("Settings")
        trading_days = get_trading_days(60)
        selected_day = st.selectbox(
            "Select trading day",
            trading_days,
            format_func=lambda d: d.strftime("%Y-%m-%d (%a)"),
        )
    
    # Load replay for selected day
    st.subheader(f"Replay: {selected_day.strftime('%Y-%m-%d')}")
    
    day_replay = load_replay(selected_day)
    if day_replay is None or not day_replay.mtf_trades:
        st.warning(f"No replay data available for {selected_day}")
        return
    
    # Initialize session state for playback
    if "playback_index" not in st.session_state:
        st.session_state.playback_index = 0
    if "is_playing" not in st.session_state:
        st.session_state.is_playing = False
    if "speed" not in st.session_state:
        st.session_state.speed = 1
    if "manual_marks" not in st.session_state:
        st.session_state.manual_marks = []
    
    # Get candles for charting
    try:
        from engines.replay_mtf import _fetch_intraday
        one_min, five_min, pdh, pdl, pdc = _fetch_intraday(selected_day)
        if not five_min:
            st.error("No candle data available")
            return
    except Exception as e:
        st.error(f"Error fetching candles: {e}")
        return
    
    # Playback controls
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        play_clicked = st.button("PLAY", use_container_width=True, key="play_btn")
        if play_clicked:
            st.session_state.is_playing = True
    
    with col2:
        pause_clicked = st.button("PAUSE", use_container_width=True, key="pause_btn")
        if pause_clicked:
            st.session_state.is_playing = False
    
    with col3:
        step_clicked = st.button("STEP", use_container_width=True, key="step_btn")
        if step_clicked:
            st.session_state.playback_index = min(
                st.session_state.playback_index + 1,
                len(five_min) - 1
            )
    
    with col4:
        st.session_state.speed = st.slider("Speed", 1, 10, st.session_state.speed)
    
    with col5:
        reset_clicked = st.button("RESET", use_container_width=True, key="reset_btn")
        if reset_clicked:
            st.session_state.playback_index = 0
            st.session_state.is_playing = False
    
    # Auto-advance during playback
    placeholder = st.empty()
    
    if st.session_state.is_playing:
        import time
        if st.session_state.playback_index < len(five_min) - 1:
            # Move to next candle
            st.session_state.playback_index += 1
            # Delay based on speed (1x = 0.5s, 10x = 0.05s)
            delay = 0.5 / st.session_state.speed
            time.sleep(delay)
    
    # Display chart
    current_candle_idx = min(st.session_state.playback_index, len(five_min) - 1)
    
    fig = build_candle_chart(
        five_min,
        [
            {
                "family": t.family,
                "direction": t.direction,
                "entry_time": t.entry_time,
                "entry": t.entry,
                "exit_time": t.exit_time,
                "exit_spot": t.exit_spot,
                "result_r": t.result_r,
            }
            for t in day_replay.mtf_trades
        ],
        current_candle_idx,
        st.session_state.manual_marks,
    )
    
    with placeholder.container():
        st.plotly_chart(fig, use_container_width=True)
    
    # Info section
    st.divider()
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Position", f"{st.session_state.playback_index + 1}/{len(five_min)} candles")
    with col2:
        st.metric("Speed", f"{st.session_state.speed}x")
    with col3:
        st.metric("Marks", len(st.session_state.manual_marks))
    
    # Manual marking section
    st.subheader("Manual Trade Marking")
    
    mark_col1, mark_col2, mark_col3 = st.columns(3)
    
    with mark_col1:
        mark_direction = st.radio("Direction", ["LONG", "SHORT"], horizontal=True)
    
    with mark_col2:
        mark_price = st.number_input("Entry Price", value=float(five_min[current_candle_idx].close))
    
    with mark_col3:
        if st.button("Mark Entry", use_container_width=True):
            st.session_state.manual_marks.append({
                "time": five_min[current_candle_idx].end,
                "price": mark_price,
                "direction": mark_direction,
            })
            st.success(f"Marked {mark_direction} @ {mark_price:.2f}")
    
    # Day-end comparison
    st.subheader("Day-End Comparison: YOUR Marks vs ENGINE Signals")
    
    # Prepare comparison data
    engine_trades = []
    for trade in day_replay.mtf_trades:
        engine_trades.append({
            "Time": trade.entry_time.strftime("%H:%M") if trade.entry_time else "N/A",
            "Direction": trade.direction,
            "Entry": f"{trade.entry:.2f}",
            "Exit": f"{trade.exit_spot:.2f}" if trade.exit_spot else "N/A",
            "Result (R)": f"{trade.result_r:+.2f}" if trade.result_r else "N/A",
            "Family": trade.family,
        })
    
    manual_trades = []
    for mark in st.session_state.manual_marks:
        manual_trades.append({
            "Time": mark["time"].strftime("%H:%M") if isinstance(mark["time"], datetime) else mark["time"],
            "Direction": mark["direction"],
            "Price": f"{mark['price']:.2f}",
            "Exit": "—",
            "Result (R)": "—",
            "Source": "YOUR MARK",
        })
    
    # Display engine trades
    st.markdown("#### ENGINE Signals")
    if engine_trades:
        df_engine = pd.DataFrame(engine_trades)
        st.dataframe(df_engine, use_container_width=True, hide_index=True)
    else:
        st.info("No engine signals generated today")
    
    # Display manual marks
    st.markdown("#### Your Manual Marks")
    if manual_trades:
        df_manual = pd.DataFrame(manual_trades)
        st.dataframe(df_manual, use_container_width=True, hide_index=True)
    else:
        st.info("No manual marks yet")
    
    # Day-end summary
    st.subheader("Day-End Summary")
    
    summary_col1, summary_col2, summary_col3, summary_col4 = st.columns(4)
    
    # Calculate stats
    closed_trades = [t for t in day_replay.mtf_trades if t.result_r is not None]
    total_r = sum(t.result_r for t in closed_trades)
    wins = len([t for t in closed_trades if t.result_r and t.result_r > 0])
    losses = len([t for t in closed_trades if t.result_r and t.result_r < 0])
    win_rate = (wins / len(closed_trades) * 100) if closed_trades else 0.0
    
    with summary_col1:
        st.metric("Trades", len(day_replay.mtf_trades))
    
    with summary_col2:
        st.metric("Win Rate", f"{win_rate:.1f}%")
    
    with summary_col3:
        st.metric("Total R", f"{total_r:+.2f}R")
    
    with summary_col4:
        st.metric("Max DD", "—")
    
    # Setup families breakdown
    st.markdown("#### Setup Families")
    
    families = {}
    for trade in day_replay.mtf_trades:
        if trade.family not in families:
            families[trade.family] = []
        families[trade.family].append(trade)
    
    family_cols = st.columns(len(families)) if families else [st.container()]
    
    for idx, (family_name, trades) in enumerate(families.items()):
        with family_cols[idx]:
            family_r = sum(t.result_r or 0.0 for t in trades)
            family_wins = len([t for t in trades if t.result_r and t.result_r > 0])
            family_wr = (family_wins / len(trades) * 100) if trades else 0.0
            
            st.markdown(f"**{family_name}**")
            st.metric("Trades", len(trades))
            st.metric("Win %", f"{family_wr:.1f}%")
            st.metric("Total R", f"{family_r:+.2f}R")
    
    # State timeline
    st.markdown("#### State Timeline")
    if day_replay.zone_log:
        for event in day_replay.zone_log[:20]:
            st.caption(event)
    else:
        st.info("No state events recorded")
    
    # Trigger rerun if still playing
    if st.session_state.is_playing and st.session_state.playback_index < len(five_min) - 1:
        st.rerun()


if __name__ == "__main__":
    main()
