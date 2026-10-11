"""Position Manager and Authoritative TradeState Machine (Phase 4).

Decouples active trade management from static entry-time assumptions.
The PositionManager evaluates:
CURRENT MARKET SNAPSHOT + CURRENT TRADE STATE + CURRENT LIQUIDITY MAP
on every decision cycle.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import settings
from engines.candles import Candle

log = logging.getLogger(__name__)


class TradeStateEnum(str, Enum):
    """Authoritative 12-state lifecycle machine for live/paper trades."""
    NO_POSITION = "NO_POSITION"
    ENTRY_PENDING = "ENTRY_PENDING"
    ENTERED = "ENTERED"
    PROFIT_PROTECTION = "PROFIT_PROTECTION"
    TREND_RUNNING = "TREND_RUNNING"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    TRAILING = "TRAILING"
    THESIS_WEAKENING = "THESIS_WEAKENING"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    EXIT_PENDING = "EXIT_PENDING"
    EXITED = "EXITED"
    COOLDOWN = "COOLDOWN"


# Formal deterministic state transition matrix (Section 2)
LEGAL_TRANSITIONS: dict[TradeStateEnum, set[TradeStateEnum]] = {
    TradeStateEnum.NO_POSITION: {
        TradeStateEnum.ENTRY_PENDING,
        TradeStateEnum.ENTERED,
    },
    TradeStateEnum.ENTRY_PENDING: {
        TradeStateEnum.ENTERED,
        TradeStateEnum.NO_POSITION,
        TradeStateEnum.EXITED,
    },
    TradeStateEnum.ENTERED: {
        TradeStateEnum.PROFIT_PROTECTION,
        TradeStateEnum.TREND_RUNNING,
        TradeStateEnum.PARTIAL_EXIT,
        TradeStateEnum.TRAILING,
        TradeStateEnum.THESIS_WEAKENING,
        TradeStateEnum.THESIS_INVALIDATED,
        TradeStateEnum.EXIT_PENDING,
        TradeStateEnum.EXITED,
    },
    TradeStateEnum.PROFIT_PROTECTION: {
        TradeStateEnum.TREND_RUNNING,
        TradeStateEnum.PARTIAL_EXIT,
        TradeStateEnum.TRAILING,
        TradeStateEnum.THESIS_WEAKENING,
        TradeStateEnum.THESIS_INVALIDATED,
        TradeStateEnum.EXIT_PENDING,
        TradeStateEnum.EXITED,
    },
    TradeStateEnum.TREND_RUNNING: {
        TradeStateEnum.PARTIAL_EXIT,
        TradeStateEnum.TRAILING,
        TradeStateEnum.THESIS_WEAKENING,
        TradeStateEnum.THESIS_INVALIDATED,
        TradeStateEnum.EXIT_PENDING,
        TradeStateEnum.EXITED,
    },
    TradeStateEnum.PARTIAL_EXIT: {
        TradeStateEnum.TREND_RUNNING,
        TradeStateEnum.TRAILING,
        TradeStateEnum.THESIS_WEAKENING,
        TradeStateEnum.THESIS_INVALIDATED,
        TradeStateEnum.EXIT_PENDING,
        TradeStateEnum.EXITED,
    },
    TradeStateEnum.TRAILING: {
        TradeStateEnum.TREND_RUNNING,
        TradeStateEnum.PARTIAL_EXIT,
        TradeStateEnum.THESIS_WEAKENING,
        TradeStateEnum.THESIS_INVALIDATED,
        TradeStateEnum.EXIT_PENDING,
        TradeStateEnum.EXITED,
    },
    TradeStateEnum.THESIS_WEAKENING: {
        TradeStateEnum.TRAILING,
        TradeStateEnum.TREND_RUNNING,
        TradeStateEnum.PROFIT_PROTECTION,
        TradeStateEnum.THESIS_INVALIDATED,
        TradeStateEnum.EXIT_PENDING,
        TradeStateEnum.EXITED,
    },
    TradeStateEnum.THESIS_INVALIDATED: {
        TradeStateEnum.EXIT_PENDING,
        TradeStateEnum.EXITED,
    },
    TradeStateEnum.EXIT_PENDING: {
        TradeStateEnum.EXITED,
    },
    TradeStateEnum.EXITED: {
        TradeStateEnum.COOLDOWN,
        TradeStateEnum.NO_POSITION,
    },
    TradeStateEnum.COOLDOWN: {
        TradeStateEnum.NO_POSITION,
        TradeStateEnum.ENTRY_PENDING,
        TradeStateEnum.ENTERED,
    },
}


class ExitClassification(str, Enum):
    """Standardized forensic taxonomy for completed trade exits (Section 15)."""
    CORRECT_EXIT = "CORRECT_EXIT"
    PREMATURE_EXIT = "PREMATURE_EXIT"
    THESIS_INVALIDATION = "THESIS_INVALIDATION"
    HARD_SL = "HARD_SL"
    TRAIL_STOP = "TRAIL_STOP"
    TIME_EXIT = "TIME_EXIT"
    SESSION_EXIT = "SESSION_EXIT"
    DATA_SAFETY_EXIT = "DATA_SAFETY_EXIT"
    RISK_GOVERNOR_EXIT = "RISK_GOVERNOR_EXIT"
    EXECUTION_EXIT = "EXECUTION_EXIT"


@dataclass
class TradeManagementDecision:
    """Deterministic output contract produced on every management cycle (Section 5)."""
    action: str                       # "HOLD" | "TRAIL" | "PROTECT" | "PARTIAL_EXIT" | "EXIT" | "NO_ACTION"
    position_state: TradeStateEnum
    reason_codes: list[str] = field(default_factory=list)
    current_R: float = 0.0
    thesis_status: str = "VALID"      # "VALID" | "WEAKENING" | "INVALIDATED"
    structure_status: str = ""
    liquidity_status: str = ""
    new_SL: float | None = None
    target_context: str | None = None
    snapshot_id: str = ""
    partial_fraction: float = 0.0
    exit_trigger: str | None = None
    exit_price: float | None = None
    classification: ExitClassification | None = None

    def __iter__(self):
        """Allows backwards-compatible 2-tuple unpacking: `pm_state, exit_reason = decision`."""
        yield self.position_state
        yield self.exit_trigger if self.action == "EXIT" else None


@dataclass
class TradeState:
    """Authoritative live position state with current-state lifecycle tracking (Section 1)."""
    trade_id: str
    strategy_id: str
    direction: str                                      # "LONG" | "SHORT"
    entry_time: datetime
    entry_price: float
    quantity: int = 75
    initial_sl: float = 0.0
    current_sl: float = 0.0

    entry_snapshot_id: str = ""
    latest_snapshot_id: str = ""

    entry_regime: str = ""
    current_regime: str = ""

    entry_structure: str = ""
    current_structure: str = ""

    entry_liquidity_map: Any = None
    current_liquidity_map: Any = None

    initial_thesis: str = ""
    current_thesis: str = ""
    thesis_status: str = "VALID"                        # "VALID" | "WEAKENING" | "INVALIDATED"

    current_price: float = 0.0
    current_r: float = 0.0
    current_pnl: float = 0.0

    mfe_r: float = 0.0
    mae_r: float = 0.0

    highest_price_since_entry: float = 0.0
    lowest_price_since_entry: float = 0.0

    time_in_trade_minutes: float = 0.0

    last_structure_update: datetime | None = None
    last_liquidity_update: datetime | None = None
    last_trailing_update: datetime | None = None

    position_state: TradeStateEnum = TradeStateEnum.ENTERED
    exit_reason: str | None = None
    exit_trigger: str | None = None
    exit_classification: ExitClassification | None = None
    exit_time: datetime | None = None
    exit_price: float | None = None
    realized_r: float | None = None

    # Auxiliary tracking
    risk_pts: float = 0.0
    original_target: float | None = None
    target_plan: str = "STRUCTURE_TRAIL"
    trail_stop: float | None = None
    booked_r: float = 0.0
    partial_taken: bool = False
    remaining_quantity: int = 75
    cooldown_until: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.risk_pts <= 0:
            self.risk_pts = abs(self.entry_price - self.initial_sl)
        if self.highest_price_since_entry == 0.0:
            self.highest_price_since_entry = self.entry_price
        if self.lowest_price_since_entry == 0.0:
            self.lowest_price_since_entry = self.entry_price
        if self.current_sl == 0.0:
            self.current_sl = self.initial_sl
        if not self.current_thesis:
            self.current_thesis = self.initial_thesis
        self.remaining_quantity = self.quantity

    # Backward compatibility aliases
    @property
    def initial_SL(self) -> float:
        return self.initial_sl

    @initial_SL.setter
    def initial_SL(self, val: float):
        self.initial_sl = val

    @property
    def current_SL(self) -> float:
        return self.current_sl

    @current_SL.setter
    def current_SL(self, val: float):
        self.current_sl = val

    @property
    def current_R(self) -> float:
        return self.current_r

    @current_R.setter
    def current_R(self, val: float):
        self.current_r = val

    @property
    def current_PnL(self) -> float:
        return self.current_pnl

    @current_PnL.setter
    def current_PnL(self, val: float):
        self.current_pnl = val

    @property
    def MFE(self) -> float:
        return self.mfe_r

    @property
    def MAE(self) -> float:
        return self.mae_r

    @property
    def time_in_trade(self) -> float:
        return self.time_in_trade_minutes

    @property
    def lifecycle_state(self) -> TradeStateEnum:
        return self.position_state

    @lifecycle_state.setter
    def lifecycle_state(self, val: TradeStateEnum):
        self.position_state = val

    @property
    def one_r_pts(self) -> float:
        return self.risk_pts if self.risk_pts > 0 else 1.0

    def transition_to(self, new_state: TradeStateEnum) -> bool:
        """Enforces legal deterministic transitions (Section 2)."""
        if new_state == self.position_state:
            return True
        allowed = LEGAL_TRANSITIONS.get(self.position_state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Illegal state transition from {self.position_state} to {new_state}"
            )
        log.info("Trade %s state transition: %s -> %s", self.trade_id, self.position_state, new_state)
        self.position_state = new_state
        return True


class PositionManager:
    """Manages active position, trailing, and exits based on CURRENT market state."""

    def __init__(self, hard_loss_cap_r: float = -1.1):
        self.hard_loss_cap_r = hard_loss_cap_r
        self.active_trade: TradeState | None = None
        self.last_alert_state: TradeStateEnum | None = None

    def open_position(
        self,
        trade_id: str,
        strategy_id: str,
        direction: str,
        entry_time: datetime,
        entry_price: float,
        initial_sl: float,
        thesis: str,
        regime: str = "NORMAL",
        target_plan: str = "STRUCTURE_TRAIL",
        original_target: float | None = None,
        quantity: int = 75,
        snapshot_id: str = "",
        liquidity_map: Any = None,
        structure: str = "",
    ) -> TradeState:
        trade = TradeState(
            trade_id=trade_id,
            strategy_id=strategy_id,
            direction=direction,
            entry_time=entry_time,
            entry_price=entry_price,
            current_price=entry_price,
            initial_sl=initial_sl,
            current_sl=initial_sl,
            entry_snapshot_id=snapshot_id,
            latest_snapshot_id=snapshot_id,
            entry_regime=regime,
            current_regime=regime,
            entry_structure=structure,
            current_structure=structure,
            entry_liquidity_map=liquidity_map,
            current_liquidity_map=liquidity_map,
            initial_thesis=thesis,
            current_thesis=thesis,
            thesis_status="VALID",
            target_plan=target_plan,
            original_target=original_target,
            quantity=quantity,
            position_state=TradeStateEnum.ENTERED,
        )
        self.active_trade = trade
        self.last_alert_state = TradeStateEnum.ENTERED
        log.info("Position opened: %s %s %s @ %.2f (SL: %.2f)", trade_id, strategy_id, direction, entry_price, initial_sl)
        return trade

    def update(
        self,
        snapshot,
        candle: Candle | None = None,
        current_regime: str = "",
        structure_swing: float | None = None,
        thesis_intact: bool = True,
        liquidity_map: Any = None,
        opposite_signal_weak: bool = False,
    ) -> TradeManagementDecision:
        """Update active trade against CURRENT snapshot, structure, and liquidity map.
        
        Evaluates the strict risk hierarchy:
        1. Hard SL
        2. Data blindness safety
        3. Catastrophic risk (-1.1R)
        4. Session square-off (15:10 IST)
        5. Thesis invalidation
        6. Strategy management
        7. Trailing
        8. Profit protection
        """
        trade = self.active_trade
        if not trade or trade.position_state in (TradeStateEnum.EXITED, TradeStateEnum.NO_POSITION):
            return TradeManagementDecision(
                action="NO_ACTION",
                position_state=TradeStateEnum.NO_POSITION,
                reason_codes=["NO_ACTIVE_TRADE"],
            )

        now = snapshot.timestamp
        current_spot = float(snapshot.spot)
        trade.current_price = current_spot
        trade.latest_snapshot_id = getattr(snapshot, "snapshot_id", "")
        trade.current_regime = current_regime or trade.current_regime
        trade.time_in_trade_minutes = (now - trade.entry_time).total_seconds() / 60.0

        if candle is not None:
            c_src = getattr(candle, "source", "chart")
            trade.metadata["managed_by"] = c_src
            trade.metadata["last_candle_time"] = candle.start.isoformat()

        if liquidity_map is not None:
            trade.current_liquidity_map = liquidity_map
            trade.last_liquidity_update = now

        # Update high/low watermark
        trade.highest_price_since_entry = max(trade.highest_price_since_entry, current_spot, (candle.high if candle else current_spot))
        trade.lowest_price_since_entry = min(trade.lowest_price_since_entry, current_spot, (candle.low if candle else current_spot))

        # Calculate current R, MFE, MAE
        if trade.direction == "LONG":
            cur_pts = current_spot - trade.entry_price
            high_pts = (candle.high - trade.entry_price) if candle else cur_pts
            low_pts = (candle.low - trade.entry_price) if candle else cur_pts
        else:
            cur_pts = trade.entry_price - current_spot
            high_pts = (trade.entry_price - candle.low) if candle else cur_pts
            low_pts = (trade.entry_price - candle.high) if candle else cur_pts

        trade.current_r = cur_pts / trade.one_r_pts
        trade.mfe_r = max(trade.mfe_r, high_pts / trade.one_r_pts)
        trade.mae_r = min(trade.mae_r, low_pts / trade.one_r_pts)
        trade.current_pnl = cur_pts * trade.remaining_quantity

        # Compute next target context from CURRENT liquidity map (Section 8)
        target_ctx = None
        if trade.current_liquidity_map:
            lmap = trade.current_liquidity_map
            if trade.direction == "SHORT":
                if hasattr(lmap, "nearest_downside_liquidity") and lmap.nearest_downside_liquidity:
                    target_ctx = f"{lmap.nearest_downside_liquidity.level_id} ({lmap.nearest_downside_liquidity.price:.1f}) [Room: {lmap.room_down:.1f} pts]"
            else:
                if hasattr(lmap, "nearest_upside_liquidity") and lmap.nearest_upside_liquidity:
                    target_ctx = f"{lmap.nearest_upside_liquidity.level_id} ({lmap.nearest_upside_liquidity.price:.1f}) [Room: {lmap.room_up:.1f} pts]"

        # -------------------------------------------------------------
        # PRIORITY 1: HARD SL CHECK (Section 6)
        # -------------------------------------------------------------
        hard_hit = False
        if candle:
            if trade.direction == "LONG" and candle.low <= trade.current_sl:
                hard_hit = True
            elif trade.direction == "SHORT" and candle.high >= trade.current_sl:
                hard_hit = True
        else:
            if trade.direction == "LONG" and current_spot <= trade.current_sl:
                hard_hit = True
            elif trade.direction == "SHORT" and current_spot >= trade.current_sl:
                hard_hit = True

        if hard_hit:
            reason = "Hard SL hit" if trade.current_sl == trade.initial_sl else "Trailing SL hit"
            classification = ExitClassification.HARD_SL if trade.current_sl == trade.initial_sl else ExitClassification.TRAIL_STOP
            slippage = 2.0
            fill_px = (trade.current_sl - slippage) if trade.direction == "LONG" else (trade.current_sl + slippage)
            if candle:
                if trade.direction == "LONG" and candle.open < trade.current_sl:
                    fill_px = candle.open
                elif trade.direction == "SHORT" and candle.open > trade.current_sl:
                    fill_px = candle.open
            return self._close_and_decide(trade, now, fill_px, reason, classification, target_ctx)

        # -------------------------------------------------------------
        # PRIORITY 2: DATA BLINDNESS SAFETY (Section 20)
        # -------------------------------------------------------------
        if getattr(snapshot, "is_blind", False):
            log.warning("Data blindness detected during active trade %s! Halting trailing updates.", trade.trade_id)
            return TradeManagementDecision(
                action="NO_ACTION",
                position_state=trade.position_state,
                reason_codes=["DATA_BLINDNESS_SAFETY (Freeze trailing stops)"],
                current_R=trade.current_r,
                thesis_status=trade.thesis_status,
                snapshot_id=trade.latest_snapshot_id,
            )

        # -------------------------------------------------------------
        # PRIORITY 3: CATASTROPHIC RISK (-1.1R CAP) (Section 6)
        # -------------------------------------------------------------
        if trade.current_r <= self.hard_loss_cap_r:
            return self._close_and_decide(
                trade, now, current_spot,
                f"Hard -1.1R cap breached ({trade.current_r:.2f}R)",
                ExitClassification.HARD_SL,
                target_ctx,
            )

        # -------------------------------------------------------------
        # PRIORITY 4: SESSION SQUARE-OFF (15:10 IST) (Section 6)
        # -------------------------------------------------------------
        if now.time() >= settings.SQUARE_OFF_TIME:
            return self._close_and_decide(
                trade, now, current_spot,
                "15:10 forced square-off",
                ExitClassification.SESSION_EXIT,
                target_ctx,
            )

        # -------------------------------------------------------------
        # PRIORITY 5: THESIS INVALIDATION (Section 3 & 18)
        # -------------------------------------------------------------
        if not thesis_intact:
            trade.thesis_status = "INVALIDATED"
            trade.transition_to(TradeStateEnum.THESIS_INVALIDATED)
            return self._close_and_decide(
                trade, now, current_spot,
                "Thesis invalidated by market structure",
                ExitClassification.THESIS_INVALIDATION,
                target_ctx,
            )

        # Weak opposite signal suppression (Section 17)
        if opposite_signal_weak:
            log.info("Ignoring weak opposite signal for active trend trade %s", trade.trade_id)

        # -------------------------------------------------------------
        # PRIORITY 6: PARTIAL PROFIT AT MAJOR POOL (Section 7, 21, 22)
        # S1 Project Bible Rule: 30% partial at major pool, 70% trails
        # -------------------------------------------------------------
        major_pool_hit = False
        if trade.current_liquidity_map:
            lmap = trade.current_liquidity_map
            major_pool = lmap.next_major_downside if trade.direction == "SHORT" else lmap.next_major_upside
            if major_pool:
                if trade.direction == "SHORT" and current_spot <= (major_pool.price + 5.0):
                    major_pool_hit = True
                elif trade.direction == "LONG" and current_spot >= (major_pool.price - 5.0):
                    major_pool_hit = True

        if major_pool_hit and not trade.partial_taken and trade.mfe_r >= 1.5:
            return self.execute_partial_exit(trade, now, fraction=0.30, target_ctx=target_ctx)

        # -------------------------------------------------------------
        # PRIORITY 7 & 8: PROFIT DEFENSE & DYNAMIC STRUCTURE TRAILING
        # -------------------------------------------------------------
        decision_action = "HOLD"
        reasons = []

        # Breakeven defense at +1.0R
        if trade.mfe_r >= 1.0 and trade.position_state == TradeStateEnum.ENTERED:
            trade.current_sl = trade.entry_price
            trade.booked_r = max(trade.booked_r, 0.5)
            trade.transition_to(TradeStateEnum.PROFIT_PROTECTION)
            trade.current_thesis = "Breakeven locked (+1.0R achieved)"
            decision_action = "PROTECT"
            reasons.append("Reached +1.0R -> SL moved to breakeven")

        # Promote to TREND_RUNNING in expansion
        is_expansion = "EXPANSION" in trade.current_regime or "TREND" in trade.current_regime
        if is_expansion and trade.mfe_r >= 1.2 and trade.position_state in (TradeStateEnum.ENTERED, TradeStateEnum.PROFIT_PROTECTION):
            trade.transition_to(TradeStateEnum.TREND_RUNNING)
            reasons.append("Trend expansion sustained -> promoted to TREND_RUNNING")

        # Dynamic 5m Structure Trailing (Section 10 & 11)
        if structure_swing is not None and trade.position_state in (TradeStateEnum.TREND_RUNNING, TradeStateEnum.PROFIT_PROTECTION, TradeStateEnum.TRAILING, TradeStateEnum.PARTIAL_EXIT):
            # Directional constraint: trailing MUST ONLY move in favorable direction
            if trade.direction == "SHORT":
                proposed_sl = structure_swing
                if proposed_sl < trade.current_sl:
                    prev_sl = trade.current_sl
                    trade.current_sl = proposed_sl
                    trade.trail_stop = proposed_sl
                    trade.last_trailing_update = now
                    trade.transition_to(TradeStateEnum.TRAILING)
                    decision_action = "TRAIL"
                    reasons.append(f"Structure trail ratcheted down to {proposed_sl:.2f} (prev: {prev_sl:.2f})")
                    log.info("Trade %s structure trail ratcheted to %.2f", trade.trade_id, proposed_sl)
            elif trade.direction == "LONG":
                proposed_sl = structure_swing
                if proposed_sl > trade.current_sl:
                    prev_sl = trade.current_sl
                    trade.current_sl = proposed_sl
                    trade.trail_stop = proposed_sl
                    trade.last_trailing_update = now
                    trade.transition_to(TradeStateEnum.TRAILING)
                    decision_action = "TRAIL"
                    reasons.append(f"Structure trail ratcheted up to {proposed_sl:.2f} (prev: {prev_sl:.2f})")
                    log.info("Trade %s structure trail ratcheted to %.2f", trade.trade_id, proposed_sl)

        return TradeManagementDecision(
            action=decision_action,
            position_state=trade.position_state,
            reason_codes=reasons or ["Holding active position with valid thesis"],
            current_R=trade.current_r,
            thesis_status=trade.thesis_status,
            structure_status=trade.current_structure or ("5m bearish" if trade.direction == "SHORT" else "5m bullish"),
            liquidity_status=target_ctx or "Tracking next liquidity",
            new_SL=trade.current_sl,
            target_context=target_ctx,
            snapshot_id=trade.latest_snapshot_id,
        )

    def execute_partial_exit(
        self,
        trade: TradeState,
        now: datetime,
        fraction: float = 0.30,
        target_ctx: str | None = None,
    ) -> TradeManagementDecision:
        """Idempotent partial exit of position (Section 21 & 22)."""
        if trade.partial_taken:
            return TradeManagementDecision(
                action="HOLD",
                position_state=trade.position_state,
                reason_codes=["Partial exit already executed (idempotent)"],
                current_R=trade.current_r,
                target_context=target_ctx,
            )

        trade.partial_taken = True
        exit_qty = int(trade.quantity * fraction)
        trade.remaining_quantity = trade.quantity - exit_qty
        trade.booked_r += trade.current_r * fraction
        trade.transition_to(TradeStateEnum.PARTIAL_EXIT)

        reason = f"Idempotent 30% partial booked at major pool (+{trade.current_r:.2f}R)"
        log.info("Trade %s: %s. Remaining qty: %d", trade.trade_id, reason, trade.remaining_quantity)

        return TradeManagementDecision(
            action="PARTIAL_EXIT",
            position_state=TradeStateEnum.PARTIAL_EXIT,
            reason_codes=[reason],
            current_R=trade.current_r,
            thesis_status=trade.thesis_status,
            target_context=target_ctx,
            partial_fraction=fraction,
        )

    def _close_and_decide(
        self,
        trade: TradeState,
        exit_time: datetime,
        exit_price: float,
        reason: str,
        classification: ExitClassification,
        target_ctx: str | None = None,
    ) -> TradeManagementDecision:
        self.close_position(trade, exit_time, exit_price, reason, classification)
        return TradeManagementDecision(
            action="EXIT",
            position_state=TradeStateEnum.EXITED,
            reason_codes=[reason],
            current_R=trade.realized_r or trade.current_r,
            thesis_status=trade.thesis_status,
            exit_trigger=reason,
            exit_price=exit_price,
            target_context=target_ctx,
            classification=classification,
        )

    def close_position(
        self,
        trade: TradeState,
        exit_time: datetime,
        exit_price: float,
        reason: str,
        classification: ExitClassification = ExitClassification.CORRECT_EXIT,
    ) -> tuple[TradeStateEnum, str]:
        trade.exit_time = exit_time
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.exit_trigger = reason
        trade.exit_classification = classification
        source = trade.metadata.get("managed_by", "chart")
        trade.metadata["closed_by"] = source
        trade.transition_to(TradeStateEnum.EXITED)

        pts = (exit_price - trade.entry_price) if trade.direction == "LONG" else (trade.entry_price - exit_price)
        calc_r = pts / trade.one_r_pts
        # Cap loss at -1.1R
        trade.realized_r = max(self.hard_loss_cap_r, calc_r)

        # Audit premature exit (Section 15)
        if trade.mfe_r >= 3.0 and (trade.realized_r or 0.0) < 1.0 and classification not in (ExitClassification.HARD_SL, ExitClassification.THESIS_INVALIDATION):
            trade.exit_classification = ExitClassification.PREMATURE_EXIT

        # Set 20-min cooldown
        cooldown_mins = 20 if "TREND" in trade.current_regime else 30
        trade.cooldown_until = exit_time + timedelta(minutes=cooldown_mins)

        log.info("Position closed: %s @ %.2f (%+.2fR) Reason: %s [Class: %s]",
                 trade.trade_id, exit_price, trade.realized_r, reason, trade.exit_classification.value)
        return TradeStateEnum.EXITED, reason
