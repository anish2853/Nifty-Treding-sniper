"""Replay data types (no circular imports)."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ReplayTrade:
    day: str
    family: str
    direction: str
    entry_time: datetime
    entry: float
    sl: float
    trigger: float | None
    exit_time: datetime | None = None
    exit_spot: float | None = None
    exit_reason: str = ""
    result_r: float | None = None
    zone_id: str = ""
    notes: str = ""
    sl_prem: float | None = None
    risk_units: float = 1.0

    def __post_init__(self):
        if self.sl_prem is None:
            self.sl_prem = 75.0


@dataclass
class DayReplay:
    date: str
    gaps: list[str] = field(default_factory=list)
    mtf_trades: list[ReplayTrade] = field(default_factory=list)
    orb_signals: list[dict] = field(default_factory=list)
    zone_log: list[str] = field(default_factory=list)
    run_id: str = ""  # Unique run identifier (timestamp + mode)
