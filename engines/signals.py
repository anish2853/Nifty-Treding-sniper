"""Signal Lifecycle Management (Phase 3).

Every signal has an explicit validity horizon (valid_until).
A signal that is not executed within its validity window is DEAD (EXPIRED).
Never revive an expired signal simply because price later reaches its old entry.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class SignalStatus(str, Enum):
    DETECTED = "DETECTED"
    QUALIFIED = "QUALIFIED"
    ENTRY_PENDING = "ENTRY_PENDING"
    EXECUTED = "EXECUTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    INVALIDATED = "INVALIDATED"


@dataclass
class SignalLifecycle:
    """A trade signal with strict lifecycle tracking and expiration."""
    signal_id: str
    strategy_id: str
    created_at: datetime
    valid_until: datetime
    market_snapshot_id: str
    direction: str                     # "LONG" | "SHORT"
    entry_zone: tuple[float, float]     # (min_entry, max_entry)
    sl_spot: float
    target_plan: str
    score: int
    reasons: list[str] = field(default_factory=list)
    regime: str = ""
    status: SignalStatus = SignalStatus.DETECTED
    executed_at: datetime | None = None
    execution_price: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, current_time: datetime) -> bool:
        return current_time > self.valid_until

    def check_expiration(self, current_time: datetime) -> bool:
        if self.status in (SignalStatus.DETECTED, SignalStatus.QUALIFIED, SignalStatus.ENTRY_PENDING):
            if self.is_expired(current_time):
                self.status = SignalStatus.EXPIRED
                log.info("Signal %s (%s %s) EXPIRED at %s", self.signal_id, self.strategy_id, self.direction, current_time)
                return True
        return False

    def can_execute(self, current_time: datetime, current_spot: float) -> bool:
        """Only an active ENTRY_PENDING signal that has NOT expired and is within entry zone can execute."""
        if self.check_expiration(current_time):
            return False
        if self.status != SignalStatus.ENTRY_PENDING:
            return False
        min_entry, max_entry = self.entry_zone
        return min_entry <= current_spot <= max_entry

    def mark_executed(self, fill_time: datetime, fill_price: float) -> None:
        self.status = SignalStatus.EXECUTED
        self.executed_at = fill_time
        self.execution_price = fill_price

    def mark_invalidated(self, reason: str = "") -> None:
        self.status = SignalStatus.INVALIDATED
        if reason:
            self.metadata["invalidation_reason"] = reason
