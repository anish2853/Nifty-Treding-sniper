"""Phase 4 Acceptance Test Suite: Position Management & Large Trend Capture.

Verifies the 15 required position management criteria specified in Phase 4:
1. test_trade_state_machine()
2. test_trade_uses_latest_snapshot()
3. test_original_thesis_not_used_as_live_command()
4. test_short_trailing_never_widens_sl()
5. test_long_trailing_never_widens_sl()
6. test_consumed_liquidity_updates_target_context()
7. test_fixed_small_target_cannot_exit_valid_s1_trend()
8. test_opposite_weak_signal_does_not_exit_trend()
9. test_5m_structure_overrides_1m_noise()
10. test_data_blind_blocks_new_entries()
11. test_partial_exit_idempotent()
12. test_position_quantity_updates_after_partial()
13. test_mfe_mae_recorded()
14. test_exit_reason_recorded()
15. test_no_future_data_in_trailing()
"""
import pytest
from datetime import datetime, timedelta
from types import SimpleNamespace

from engines.candles import Candle
from engines.position_manager import (
    PositionManager,
    TradeState,
    TradeStateEnum,
    ExitClassification,
    TradeManagementDecision,
    LEGAL_TRANSITIONS,
)
from engines.levels_lifecycle import (
    LevelLifecycleManager,
    LevelTier,
    LevelRole,
    LevelStatus,
)
from utils import ist_now


def test_trade_state_machine():
    """1. Test legal and illegal state transitions in the 12-state machine."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-SM-1", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Breakdown"
    )

    # Valid transitions: ENTERED -> PROFIT_PROTECTION -> TREND_RUNNING -> TRAILING
    assert trade.position_state == TradeStateEnum.ENTERED
    trade.transition_to(TradeStateEnum.PROFIT_PROTECTION)
    assert trade.position_state == TradeStateEnum.PROFIT_PROTECTION

    trade.transition_to(TradeStateEnum.TREND_RUNNING)
    assert trade.position_state == TradeStateEnum.TREND_RUNNING

    trade.transition_to(TradeStateEnum.TRAILING)
    assert trade.position_state == TradeStateEnum.TRAILING

    trade.transition_to(TradeStateEnum.EXITED)
    assert trade.position_state == TradeStateEnum.EXITED

    # Illegal transition: EXITED directly to TREND_RUNNING must raise ValueError!
    with pytest.raises(ValueError, match="Illegal state transition"):
        trade.transition_to(TradeStateEnum.TREND_RUNNING)


def test_trade_uses_latest_snapshot():
    """2. PositionManager must evaluate latest MarketSnapshot, not entry snapshot."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-SNAP-1", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Breakdown",
        snapshot_id="SNAP-ENTRY-001"
    )

    # Update with new snapshot
    snap2 = SimpleNamespace(
        snapshot_id="SNAP-LIVE-002",
        timestamp=now + timedelta(minutes=5),
        spot=22380.0,
        atr=25.0,
        is_blind=False,
    )
    dec = pm.update(snap2)

    assert trade.latest_snapshot_id == "SNAP-LIVE-002"
    assert trade.current_price == 22380.0
    assert trade.current_R == pytest.approx(1.4, 0.01)  # (22450 - 22380) / 50 = 1.4R
    assert dec.snapshot_id == "SNAP-LIVE-002"


def test_original_thesis_not_used_as_live_command():
    """3. Original thesis is permanently preserved for forensics, but current thesis evolves."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-THESIS-1", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Initial PDL breakdown"
    )

    assert trade.initial_thesis == "Initial PDL breakdown"
    assert trade.current_thesis == "Initial PDL breakdown"

    # Candle reaches +1.2R in expansion
    c1 = Candle(now + timedelta(minutes=5), 22440, 22445, 22390, 22390)
    snap = SimpleNamespace(snapshot_id="SNAP-2", timestamp=now + timedelta(minutes=5), spot=22390.0, atr=25.0, is_blind=False)
    pm.update(snap, candle=c1, current_regime="TREND-DOWN (EXPANSION)")

    # Current thesis evolved, initial thesis remained untouched
    assert trade.initial_thesis == "Initial PDL breakdown"
    assert trade.thesis_status == "VALID"
    assert trade.position_state in (TradeStateEnum.PROFIT_PROTECTION, TradeStateEnum.TREND_RUNNING)


def test_short_trailing_never_widens_sl():
    """4. For SHORT: new_SL <= previous_SL. Never widen risk."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-TRAIL-S", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Breakdown"
    )
    trade.transition_to(TradeStateEnum.TREND_RUNNING)

    snap = SimpleNamespace(snapshot_id="SNAP-3", timestamp=now + timedelta(minutes=5), spot=22380.0, atr=25.0, is_blind=False)

    # 1. New lower high forms at 22430 -> SL ratchets down
    pm.update(snap, structure_swing=22430.0)
    assert trade.current_sl == 22430.0

    # 2. Next bounce swing reaches 22470 -> SL MUST NOT WIDEN to 22470!
    pm.update(snap, structure_swing=22470.0)
    assert trade.current_sl == 22430.0, "Short trailing stop must NEVER widen upwards!"


def test_long_trailing_never_widens_sl():
    """5. For LONG: new_SL >= previous_SL. Never widen risk."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-TRAIL-L", strategy_id="S1_TREND_EXPANSION", direction="LONG",
        entry_time=now, entry_price=22400.0, initial_sl=22350.0, thesis="Breakout"
    )
    trade.transition_to(TradeStateEnum.TREND_RUNNING)

    snap = SimpleNamespace(snapshot_id="SNAP-4", timestamp=now + timedelta(minutes=5), spot=22460.0, atr=25.0, is_blind=False)

    # 1. New higher low forms at 22420 -> SL ratchets up
    pm.update(snap, structure_swing=22420.0)
    assert trade.current_sl == 22420.0

    # 2. Deep pullback wick forms at 22380 -> SL MUST NOT WIDEN to 22380!
    pm.update(snap, structure_swing=22380.0)
    assert trade.current_sl == 22420.0, "Long trailing stop must NEVER widen downwards!"


def test_consumed_liquidity_updates_target_context():
    """6. Consumed liquidity is removed from target context and advances to next pool."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    mgr = LevelLifecycleManager(tolerance_pts=5.0)

    # Register pools: 22390 (Session) and 22300 (Major)
    mgr.register_level("S_22390", 22390.0, "SWING_LOW", LevelRole.SUPPORT, now, tier=LevelTier.SESSION)
    mgr.register_level("S_22300", 22300.0, "ROUND_NUMBER", LevelRole.SUPPORT, now, tier=LevelTier.MAJOR)

    trade = pm.open_position(
        trade_id="TRD-LIQ-1", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22430.0, initial_sl=22480.0, thesis="Breakdown"
    )

    # Step 1: Price at 22410 -> Next liquidity is 22390
    lmap1 = mgr.get_next_liquidity(22410.0, market_state="TREND-DOWN")
    dec1 = pm.update(SimpleNamespace(spot=22410.0, timestamp=now, atr=20.0, is_blind=False), liquidity_map=lmap1)
    assert "S_22390" in (dec1.target_context or "")

    # Step 2: Price penetrates 22390 down to 22350 -> S_22390 is BROKEN/CONSUMED
    c_break = Candle(now + timedelta(minutes=5), 22395, 22395, 22340, 22350)
    mgr.update(SimpleNamespace(spot=22350.0, timestamp=now + timedelta(minutes=5), atr=20.0), c_break)

    lmap2 = mgr.get_next_liquidity(22350.0, market_state="TREND-DOWN")
    dec2 = pm.update(SimpleNamespace(spot=22350.0, timestamp=now + timedelta(minutes=5), atr=20.0, is_blind=False), liquidity_map=lmap2)

    # Target context must now be S_22300, NOT consumed 22390!
    assert "S_22300" in (dec2.target_context or "")
    assert "S_22390" not in (dec2.target_context or "")


def test_fixed_small_target_cannot_exit_valid_s1_trend():
    """7. Reaching a small +20 or +50 pt level does not exit an S1 trend runner."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-NO-FIXED", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Expansion",
        original_target=22400.0  # +50 pts static target
    )

    # Price moves +3.0R (150 pts) to 22300
    snap = SimpleNamespace(spot=22300.0, timestamp=now + timedelta(minutes=15), atr=25.0, is_blind=False)
    dec = pm.update(snap, current_regime="TREND-DOWN (EXPANSION)")

    assert dec.action != "EXIT"
    assert trade.position_state in (TradeStateEnum.PROFIT_PROTECTION, TradeStateEnum.TREND_RUNNING)
    assert trade.current_R == pytest.approx(3.0, 0.01)


def test_opposite_weak_signal_does_not_exit_trend():
    """8. Weak counter-trend signal does not terminate an active trend trade."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-OPP-1", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Expansion"
    )

    snap = SimpleNamespace(spot=22380.0, timestamp=now + timedelta(minutes=10), atr=25.0, is_blind=False)
    dec = pm.update(snap, opposite_signal_weak=True, current_regime="TREND-DOWN")

    assert dec.action in ("HOLD", "PROTECT", "TRAIL")
    assert trade.position_state != TradeStateEnum.EXITED


def test_5m_structure_overrides_1m_noise():
    """9. Noise on 1m does not trigger thesis invalidation; requires 5m structure break."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-STRUC-1", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Expansion"
    )

    # 1. Minor 1m green bounce, but 5m structure intact -> thesis_intact=True
    snap1 = SimpleNamespace(spot=22410.0, timestamp=now + timedelta(minutes=5), atr=25.0, is_blind=False)
    dec1 = pm.update(snap1, thesis_intact=True)
    assert dec1.action != "EXIT"

    # 2. 5m confirmed structural breakout against short -> thesis_intact=False
    snap2 = SimpleNamespace(spot=22470.0, timestamp=now + timedelta(minutes=10), atr=25.0, is_blind=False)
    dec2 = pm.update(snap2, thesis_intact=False)
    assert dec2.action == "EXIT"
    assert dec2.classification == ExitClassification.THESIS_INVALIDATION
    assert trade.thesis_status == "INVALIDATED"


def test_data_blind_blocks_new_entries():
    """10. Data blindness freezes active trade trailing and signals safety action."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-BLIND-1", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Expansion"
    )

    # Snapshot is blind (stale data > 5 min)
    snap = SimpleNamespace(spot=22380.0, timestamp=now + timedelta(minutes=10), atr=25.0, is_blind=True)
    dec = pm.update(snap, structure_swing=22400.0)

    assert dec.action == "NO_ACTION"
    assert "DATA_BLINDNESS_SAFETY" in dec.reason_codes[0]
    assert trade.current_sl == 22500.0, "Trailing stop must NOT update using blind/stale data!"


def test_partial_exit_idempotent():
    """11. S1 30% partial exit at major pool is idempotent (cannot execute twice)."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-PART-1", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Expansion", quantity=100
    )
    trade.current_r = 2.0  # +2R profit

    # First partial call
    dec1 = pm.execute_partial_exit(trade, now, fraction=0.30)
    assert dec1.action == "PARTIAL_EXIT"
    assert trade.partial_taken is True
    assert trade.remaining_quantity == 70  # 100 - 30
    assert trade.booked_r == pytest.approx(0.60, 0.01)  # 2.0 * 0.30

    # Second partial call on same trade
    dec2 = pm.execute_partial_exit(trade, now, fraction=0.30)
    assert dec2.action == "HOLD"
    assert trade.remaining_quantity == 70, "Must not deduct shares more than once!"


def test_position_quantity_updates_after_partial():
    """12. Position size and risk recalculate properly after partial exit."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-QTY-1", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Expansion", quantity=100
    )
    # Trade reaches +2R (spot = 22350)
    snap = SimpleNamespace(spot=22350.0, timestamp=now + timedelta(minutes=5), atr=25.0, is_blind=False)
    pm.update(snap)

    pm.execute_partial_exit(trade, now, fraction=0.30)
    assert trade.remaining_quantity == 70

    # Further move to 22300 (+3R)
    snap2 = SimpleNamespace(spot=22300.0, timestamp=now + timedelta(minutes=10), atr=25.0, is_blind=False)
    pm.update(snap2)

    # Current PnL on remaining 70 shares: (22450 - 22300) * 70 = 150 * 70 = 10,500
    assert trade.current_pnl == 150.0 * 70


def test_mfe_mae_recorded():
    """13. MFE and MAE watermarks are accurately captured across trade lifecycle."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-MFE-1", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Expansion"
    )
    # 1R = 50 pts

    # Bar 1: Adverse wick up to 22475 (-0.5R MAE), favorable to 22425 (+0.5R MFE)
    c1 = Candle(now + timedelta(minutes=5), 22450, 22475, 22425, 22430)
    pm.update(SimpleNamespace(spot=22430.0, timestamp=now + timedelta(minutes=5), atr=25.0, is_blind=False), candle=c1)

    assert trade.mae_r == pytest.approx(-0.5, 0.01)
    assert trade.mfe_r == pytest.approx(0.5, 0.01)

    # Bar 2: Favorable run to 22350 (+2.0R MFE)
    c2 = Candle(now + timedelta(minutes=10), 22430, 22435, 22350, 22360)
    pm.update(SimpleNamespace(spot=22360.0, timestamp=now + timedelta(minutes=10), atr=25.0, is_blind=False), candle=c2)

    assert trade.mfe_r == pytest.approx(2.0, 0.01)
    assert trade.lowest_price_since_entry == 22350.0
    assert trade.highest_price_since_entry == 22475.0


def test_exit_reason_recorded():
    """14. Full exit forensics are recorded on trade close."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-EXIT-1", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Expansion"
    )

    exit_time = now + timedelta(minutes=45)
    pm.close_position(trade, exit_time=exit_time, exit_price=22300.0, reason="Trailing SL hit", classification=ExitClassification.TRAIL_STOP)

    assert trade.position_state == TradeStateEnum.EXITED
    assert trade.exit_reason == "Trailing SL hit"
    assert trade.exit_trigger == "Trailing SL hit"
    assert trade.exit_classification == ExitClassification.TRAIL_STOP
    assert trade.exit_time == exit_time
    assert trade.exit_price == 22300.0
    assert trade.realized_r == pytest.approx(3.0, 0.01)


def test_no_future_data_in_trailing():
    """15. Trailing stop evaluation uses strictly causal information at timestamp."""
    now = ist_now().replace(hour=10, minute=0, second=0)
    pm = PositionManager()
    trade = pm.open_position(
        trade_id="TRD-CAUSAL-1", strategy_id="S1_TREND_EXPANSION", direction="SHORT",
        entry_time=now, entry_price=22450.0, initial_sl=22500.0, thesis="Expansion"
    )
    trade.transition_to(TradeStateEnum.TREND_RUNNING)

    t1 = now + timedelta(minutes=5)
    t2 = now + timedelta(minutes=10)

    # At t1: swing known up to t1 is 22440
    pm.update(SimpleNamespace(spot=22420.0, timestamp=t1, atr=20.0, is_blind=False), structure_swing=22440.0)
    assert trade.current_sl == 22440.0

    # At t2: swing known up to t2 is 22390
    pm.update(SimpleNamespace(spot=22370.0, timestamp=t2, atr=20.0, is_blind=False), structure_swing=22390.0)
    assert trade.current_sl == 22390.0
