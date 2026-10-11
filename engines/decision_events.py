"""Decision Event Model, Diagnostic Codes, and Deduplication Bus (Phase 5).

Provides:
1. Structured DecisionEvent with 17 allowed event types and 5 severity levels.
2. Deterministic decision hash for deduplication.
3. Diagnostic reason generators: NO_TRADE_REASON, TRADE_REASON, EXIT_REASON.
4. Opportunity capture report structure.
5. Candle engine health monitor (data blindness detector).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Any

import settings
from utils import ist_now

log = logging.getLogger(__name__)


class DecisionEventType(str, Enum):
    """The 17 authoritative decision event types (Phase 5 Section 2)."""
    MARKET_STATE_CHANGE = "MARKET_STATE_CHANGE"
    LIQUIDITY_CHANGE = "LIQUIDITY_CHANGE"
    STRATEGY_ACTIVATED = "STRATEGY_ACTIVATED"
    STRATEGY_REJECTED = "STRATEGY_REJECTED"
    ARBITER_DECISION = "ARBITER_DECISION"
    RISK_DECISION = "RISK_DECISION"
    TRADE_INTENT = "TRADE_INTENT"
    TRADE_ENTRY = "TRADE_ENTRY"
    TRADE_UPDATE = "TRADE_UPDATE"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    TRAIL_UPDATE = "TRAIL_UPDATE"
    THESIS_WEAKENING = "THESIS_WEAKENING"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    TRADE_EXIT = "TRADE_EXIT"
    DATA_BLIND = "DATA_BLIND"
    SYSTEM_ALERT = "SYSTEM_ALERT"
    EOD_SUMMARY = "EOD_SUMMARY"


class EventSeverity(str, Enum):
    """Standardized event severity levels (Phase 5 Section 3)."""
    DEBUG = "DEBUG"          # Normal candle processing - internal only
    INFO = "INFO"            # Strategy evaluations, candidate selection
    IMPORTANT = "IMPORTANT"  # Trade entry, trade exit, material updates
    WARNING = "WARNING"      # Data failure, stale feed, blind period
    CRITICAL = "CRITICAL"    # Risk breach, safety halt, systemic failure


class NoTradeReason(str, Enum):
    """Deterministic diagnostic blocker codes for why the engine did not trade."""
    NO_SETUP = "NO_SETUP"
    INSUFFICIENT_SCORE = "INSUFFICIENT_SCORE"
    CONFLICTING_STRATEGIES = "CONFLICTING_STRATEGIES"
    INSUFFICIENT_ROOM = "INSUFFICIENT_ROOM"
    RISK_LIMIT = "RISK_LIMIT"
    COOLDOWN = "COOLDOWN"
    STALE_DATA = "STALE_DATA"
    OUTSIDE_ENTRY_WINDOW = "OUTSIDE_ENTRY_WINDOW"
    EVENT_DAY = "EVENT_DAY"
    EXPIRY_RESTRICTION = "EXPIRY_RESTRICTION"
    OPPOSING_LIQUIDITY = "OPPOSING_LIQUIDITY"
    EXTENDED_MOVE = "EXTENDED_MOVE"
    TREND_UNCLEAR = "TREND_UNCLEAR"
    POSITION_ALREADY_ACTIVE = "POSITION_ALREADY_ACTIVE"
    SYSTEM_SAFETY = "SYSTEM_SAFETY"


class ExitReasonCode(str, Enum):
    """Deterministic exit reason codes (Phase 5 Section 17)."""
    HARD_SL = "HARD_SL"
    STRUCTURE_INVALIDATION = "STRUCTURE_INVALIDATION"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    TRAIL_STOP = "TRAIL_STOP"
    MAJOR_LIQUIDITY_REJECTION = "MAJOR_LIQUIDITY_REJECTION"
    TIME_STOP = "TIME_STOP"
    SESSION_CLOSE = "SESSION_CLOSE"
    DATA_SAFETY = "DATA_SAFETY"
    RISK_GOVERNOR = "RISK_GOVERNOR"
    MANUAL = "MANUAL"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"


def calculate_decision_hash(
    timestamp: datetime | str,
    snapshot_id: str,
    strategy: str,
    direction: str,
    market_state: str,
    liquidity_map_id: str,
    score: float | int,
    risk_state: str,
) -> str:
    """Computes a deterministic SHA256 decision hash (Phase 5 Section 19).

    Ensures that identical decision states evaluated twice yield the exact same hash,
    preventing duplicate alert firing or redundant journal entries.
    """
    ts_str = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
    payload = (
        f"{ts_str}|{snapshot_id}|{strategy}|{direction}|"
        f"{market_state}|{liquidity_map_id}|{score:.1f}|{risk_state}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class DecisionEvent:
    """Unified structured decision event record (Phase 5 Section 1 & 2)."""
    event_id: str
    timestamp: datetime
    event_type: DecisionEventType
    severity: EventSeverity
    snapshot_id: str | None = None
    market_state_id: str | None = None
    liquidity_map_id: str | None = None
    strategy_decision_id: str | None = None
    arbiter_decision_id: str | None = None
    risk_decision_id: str | None = None
    trade_state_id: str | None = None
    trade_id: str | None = None
    strategy: str | None = None
    direction: str | None = None
    score: float | None = None
    reason_code: str | None = None
    message: str | None = None
    decision_hash: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["event_type"] = self.event_type.value
        data["severity"] = self.severity.value
        data["timestamp"] = self.timestamp.isoformat()
        return data


def get_no_trade_reason(
    eligible_count: int,
    scores: dict[str, float],
    arbiter_conflict: bool,
    risk_blocked: bool,
    risk_reason: str | None = None,
    is_blind: bool = False,
    is_cooldown: bool = False,
    has_active_position: bool = False,
    room_sufficient: bool = True,
    current_time: datetime | None = None,
) -> tuple[str, list[str]]:
    """Generates the primary NO_TRADE_REASON and secondary blockers (Section 15).

    Returns:
        tuple[str, list[str]]: (primary_reason, [secondary_reasons])
    """
    secondary: list[str] = []

    if is_blind:
        return NoTradeReason.STALE_DATA.value, ["Candle engine blind / lag > 5 min"]

    if has_active_position:
        return NoTradeReason.POSITION_ALREADY_ACTIVE.value, ["Active trade already running"]

    if is_cooldown:
        return NoTradeReason.COOLDOWN.value, ["Post-exit cooldown active"]

    if current_time:
        t = current_time.time() if hasattr(current_time, "time") else ist_now().time()
        if t < settings.TRADE_START_TIME or t > settings.TRADE_STOP_TIME:
            return NoTradeReason.OUTSIDE_ENTRY_WINDOW.value, [f"Current time {t} outside entry window"]

    if risk_blocked and risk_reason:
        if "CONSECUTIVE" in risk_reason or "SHUTDOWN" in risk_reason:
            secondary.append(risk_reason)
            return NoTradeReason.RISK_LIMIT.value, secondary
        secondary.append(risk_reason)
        return NoTradeReason.SYSTEM_SAFETY.value, secondary

    if arbiter_conflict:
        secondary.append("Multi-strategy directional conflict")
        return NoTradeReason.CONFLICTING_STRATEGIES.value, secondary

    if eligible_count == 0:
        return NoTradeReason.NO_SETUP.value, ["No strategy criteria met"]

    if not room_sufficient:
        return NoTradeReason.INSUFFICIENT_ROOM.value, ["Next liquidity wall too close (<1.5R room)"]

    max_score = max(scores.values()) if scores else 0
    if max_score < 75:
        return NoTradeReason.INSUFFICIENT_SCORE.value, [f"Top score {max_score:.1f} below qualifying threshold"]

    return NoTradeReason.NO_SETUP.value, []


def get_trade_reason(
    strategy: str,
    direction: str,
    regime: str,
    structure: str,
    liquidity: str,
    room: float | str,
    risk: float | str,
    invalidation: str,
) -> dict[str, Any]:
    """Generates structured TRADE_REASON fields without hindsight (Section 16)."""
    return {
        "strategy": strategy,
        "direction": direction,
        "regime": regime,
        "structure": structure,
        "liquidity": liquidity,
        "room": room,
        "risk": risk,
        "invalidation": invalidation,
    }


def get_exit_reason(
    trigger: str,
    current_r: float,
    thesis_valid: bool = True,
    is_time_stop: bool = False,
    is_data_blind: bool = False,
) -> tuple[str, str]:
    """Resolves forensic exit reason code and thesis classification (Section 17).

    Returns:
        tuple[str, str]: (exit_reason_code, thesis_status)
    """
    if is_data_blind:
        return ExitReasonCode.DATA_SAFETY.value, "DATA"
    if is_time_stop:
        return ExitReasonCode.SESSION_CLOSE.value, "TIME"
    if "HARD_SL" in trigger or current_r <= -1.0:
        return ExitReasonCode.HARD_SL.value, "INVALIDATED"
    if "TRAIL" in trigger:
        return ExitReasonCode.TRAIL_STOP.value, "VALID"
    if "STRUCTURE" in trigger:
        return ExitReasonCode.STRUCTURE_INVALIDATION.value, "INVALIDATED"
    if "LIQUIDITY" in trigger or "REJECTION" in trigger:
        return ExitReasonCode.MAJOR_LIQUIDITY_REJECTION.value, "VALID"
    if not thesis_valid:
        return ExitReasonCode.THESIS_INVALIDATED.value, "INVALIDATED"
    return ExitReasonCode.RISK_GOVERNOR.value, "RISK"


@dataclass
class OpportunityCaptureReport:
    """Diagnostic report capturing trade opportunity vs actual execution (Section 22)."""
    move_id: str
    direction: str
    start_time: datetime
    end_time: datetime
    available_move_pts: float
    best_strategy: str
    first_valid_signal_time: datetime | None = None
    actual_signal_time: datetime | None = None
    actual_entry_price: float | None = None
    mfe_pts: float = 0.0
    realized_r: float = 0.0
    capture_ratio: float = 0.0
    missed_reason: str | None = None

    def calculate_metrics(self) -> None:
        if self.available_move_pts > 0 and self.mfe_pts > 0:
            self.capture_ratio = min(1.0, max(0.0, self.mfe_pts / self.available_move_pts))
        else:
            self.capture_ratio = 0.0


def check_candle_engine_health(
    last_5m_end: datetime | None,
    now: datetime,
    is_market_hours: bool = True,
) -> tuple[str, bool, str]:
    """Monitors candle engine freshness and enforces Data Blindness Halt (Section 23).

    Returns:
        tuple[str, bool, str]: (health_state, is_blind, detail)
        health_state: "HEALTHY" | "WARNING" | "BLIND"
    """
    if last_5m_end is None:
        if is_market_hours:
            return "BLIND", True, "No 5m candles received during market hours"
        return "HEALTHY", False, "Pre-market / off-market initialization"

    age_sec = (now - last_5m_end).total_seconds()
    age_min = age_sec / 60.0

    if not is_market_hours:
        return "HEALTHY", False, f"Market closed (age: {age_min:.1f} min)"

    if age_min > 5.0:
        return "BLIND", True, f"5m candle age {age_min:.1f} min exceeds 5-minute freshness limit"
    if age_min > 3.0:
        return "WARNING", False, f"5m candle age {age_min:.1f} min approaching threshold"
    return "HEALTHY", False, f"5m candle fresh ({age_min:.1f} min ago)"


class DecisionEventBus:
    """In-memory event bus with deterministic deduplication and rate control."""

    def __init__(self):
        self._seen_event_ids: set[str] = set()
        self._seen_hashes: set[str] = set()
        self._events: list[DecisionEvent] = []

    def record(self, event: DecisionEvent) -> bool:
        """Records an event if it is unique. Returns True if accepted, False if duplicate."""
        if event.event_id in self._seen_event_ids:
            log.debug("Event bus rejected duplicate event_id: %s", event.event_id)
            return False

        if event.decision_hash and event.decision_hash in self._seen_hashes:
            log.debug("Event bus rejected duplicate decision_hash: %s", event.decision_hash)
            return False

        self._seen_event_ids.add(event.event_id)
        if event.decision_hash:
            self._seen_hashes.add(event.decision_hash)

        self._events.append(event)
        return True

    @property
    def events(self) -> list[DecisionEvent]:
        return list(self._events)

    def count_by_type(self, event_type: DecisionEventType) -> int:
        return sum(1 for e in self._events if e.event_type == event_type)

    def clear(self) -> None:
        self._seen_event_ids.clear()
        self._seen_hashes.clear()
        self._events.clear()
