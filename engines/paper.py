"""Paper trade model + accounting (Prompt B modules 3/4). No orders anywhere:
a PaperTrade is an alert + journal rows + mark-to-market arithmetic."""
import logging
from dataclasses import dataclass, field
from datetime import datetime

import settings

log = logging.getLogger(__name__)


@dataclass
class PaperTrade:
    trade_id: str
    direction: str                 # LONG (BUY CE) / SHORT (BUY PE)
    strike: int
    expiry: str                    # weekly expiry label
    entry_spot: float
    entry_prem: float              # chain LTP at signal time
    sl_spot: float                 # ORB midpoint
    broken_level: float            # the level whose break triggered the entry
    level_name: str                # 'ORB high' / 'PDH' / 'ORB low' / 'PDL'
    entry_time: datetime
    score: int
    components: list = field(default_factory=list)   # [(name, ok, points, detail)]
    candle_start: datetime | None = None             # signal candle window start
    extreme_spot: float | None = None                  # best spot seen (1R/2R touches)
    runner_armed: bool = False     # True after TGT 1: half booked, SL at breakeven
    booked_prem: float | None = None                 # premium booked on the 1R half
    last_prem: float | None = None                   # latest mark-to-market LTP
    family: str = "BREAKOUT"       # setup family: BREAKOUT | SWEEP-FADE (Phase B+)
    tgt1_override: float | None = None   # set by WALL-UNWINDING target re-arm
    tgt2_override: float | None = None
    orig_tgt1_spot: float | None = None  # ORIGINAL plan (post-exit shadow tracks it)
    orig_tgt2_spot: float | None = None
    reentry_granted: bool = False  # WALL-UNWINDING granted one re-entry for the direction
    flip_times: list = field(default_factory=list)   # OI-flip timestamps (second within 30 min = full exit)
    trail_stop: float | None = None  # structure trail (max(broken level, last higher low))
    runway_pts: float | None = None  # room-to-run at signal time (Trend-Day v2.1)
    runway_wall: str | None = None
    strategy: str = "BASE"
    zone_id: str | None = None
    trigger_line: float | None = None
    booked_fraction: float | None = None
    risk_units: float = 1.0
    oi_flip_pending: bool = False

    # -- derived levels -----------------------------------------------------
    @property
    def option(self) -> str:
        return f"{self.strike} {'CE' if self.direction == 'LONG' else 'PE'}"

    @property
    def one_r(self) -> float:
        return abs(self.entry_spot - self.sl_spot)

    @property
    def tgt1_spot(self) -> float:
        if self.tgt1_override is not None:
            return self.tgt1_override
        return self.entry_spot + self.one_r if self.direction == "LONG" \
            else self.entry_spot - self.one_r

    @property
    def tgt2_spot(self) -> float:
        if self.tgt2_override is not None:
            return self.tgt2_override
        return self.entry_spot + 2 * self.one_r if self.direction == "LONG" \
            else self.entry_spot - 2 * self.one_r

    def rearm_targets(self, spot: float) -> None:
        """WALL-UNWINDING grading: keep the same 1R distance, re-base both targets
        on the current spot so the runner keeps trailing with fresh levels."""
        self.tgt1_override = spot + self.one_r if self.direction == "LONG" \
            else spot - self.one_r
        self.tgt2_override = spot + 2 * self.one_r if self.direction == "LONG" \
            else spot - 2 * self.one_r

    def capture_original_plan(self) -> None:
        """Freeze the entry plan for the post-exit shadow (graded exits may
        re-arm targets afterwards; the shadow always tracks the ORIGINAL)."""
        self.orig_tgt1_spot = self.tgt1_spot
        self.orig_tgt2_spot = self.tgt2_spot

    @property
    def effective_stop_spot(self) -> float:
        """Pre-runner: ORB midpoint. Structure-trailed: the trail stop. Otherwise
        (legacy runner): breakeven (entry spot)."""
        if self.trail_stop is not None:
            return self.trail_stop
        if self.runner_armed:
            return self.entry_spot
        return self.sl_spot

    def structure_trail(self, broken_level=None, structure_level=None) -> float:
        """'OI whispers, price decides': the runner trail = max(broken level,
        last higher low) for LONG / min(...) for SHORT - NOT premium breakeven."""
        base = self.broken_level if broken_level is None else broken_level
        if self.direction == "LONG":
            candidates = [base] + ([structure_level] if structure_level is not None else [])
            self.trail_stop = max(candidates)
        else:
            candidates = [base] + ([structure_level] if structure_level is not None else [])
            self.trail_stop = min(candidates)
        return self.trail_stop

    @property
    def effective_stop_prem(self) -> float:
        return self.entry_prem if self.runner_armed \
            else self.entry_prem * settings.SL_PREMIUM_FRACTION

    # -- 1R / 2R touch detection ---------------------------------------------
    def update_extreme(self, spot: float) -> None:
        if spot is None:
            return
        spot = float(spot)
        if self.extreme_spot is None:
            self.extreme_spot = spot
        elif self.direction == "LONG":
            self.extreme_spot = max(self.extreme_spot, spot)
        else:
            self.extreme_spot = min(self.extreme_spot, spot)

    def _best_seen(self, spot) -> float | None:
        candidates = [x for x in (self.extreme_spot, spot) if x is not None]
        if not candidates:
            return None
        return max(candidates) if self.direction == "LONG" else min(candidates)

    def hit_tgt1(self, spot: float | None = None) -> bool:
        best = self._best_seen(spot)
        if best is None:
            return False
        return best >= self.tgt1_spot if self.direction == "LONG" \
            else best <= self.tgt1_spot

    def hit_tgt2(self, spot: float | None = None) -> bool:
        best = self._best_seen(spot)
        if best is None:
            return False
        return best >= self.tgt2_spot if self.direction == "LONG" \
            else best <= self.tgt2_spot

    # -- accounting ------------------------------------------------------------
    def blended_points(self, exit_prem: float) -> float:
        """Premium points of the whole position: half booked at TGT 1 once the
        runner is armed, otherwise the full position at exit_prem."""
        if self.runner_armed and self.booked_prem is not None:
            fraction = (self.booked_fraction if self.booked_fraction is not None
                else settings.PARTIAL_BOOK_FRACTION)
            return (fraction * (self.booked_prem - self.entry_prem)
                + (1 - fraction) * (exit_prem - self.entry_prem))
        return exit_prem - self.entry_prem

    def result_r(self, exit_prem: float):
        risk = settings.RISK_PREM_FRACTION * self.entry_prem
        return self.blended_points(exit_prem) / risk if risk else None

    def pnl_rupees(self, exit_prem: float):
        return self.blended_points(exit_prem) * settings.LOT_SIZE * self.risk_units

    # -- journal round-trip (crash recovery) ------------------------------------
    def to_reasons(self) -> dict:
        return {
            "trade_id": self.trade_id, "direction": self.direction,
            "strike": self.strike, "expiry": self.expiry,
            "entry_spot": self.entry_spot, "entry_prem": self.entry_prem,
            "sl_spot": self.sl_spot, "broken_level": self.broken_level,
            "level_name": self.level_name, "family": self.family,
            "orig_tgt1_spot": self.orig_tgt1_spot,
            "orig_tgt2_spot": self.orig_tgt2_spot,
            "tgt1_override": self.tgt1_override,
            "tgt2_override": self.tgt2_override,
            "reentry_granted": self.reentry_granted,
            "runway_pts": self.runway_pts,
            "runway_wall": self.runway_wall,
            "strategy": self.strategy, "zone_id": self.zone_id,
            "trigger_line": self.trigger_line,
            "booked_fraction": self.booked_fraction,
            "risk_units": self.risk_units,
            "oi_flip_pending": self.oi_flip_pending,
            "entry_time": self.entry_time.isoformat(timespec="seconds"),
            "score": self.score, "components": self.components,
            "candle_start": self.candle_start.isoformat(timespec="seconds")
                            if self.candle_start else None,
        }

    @classmethod
    def from_reasons(cls, data: dict) -> "PaperTrade":
        return cls(
            trade_id=data["trade_id"], direction=data["direction"],
            strike=int(data["strike"]), expiry=data.get("expiry", ""),
            entry_spot=data["entry_spot"], entry_prem=data["entry_prem"],
            sl_spot=data["sl_spot"], broken_level=data["broken_level"],
            level_name=data.get("level_name", ""),
            entry_time=datetime.fromisoformat(data["entry_time"]),
            score=data.get("score", 0),
            components=data.get("components", []),
            candle_start=datetime.fromisoformat(data["candle_start"])
                         if data.get("candle_start") else None,
            family=data.get("family", "BREAKOUT"),
            tgt1_override=data.get("tgt1_override"),
            tgt2_override=data.get("tgt2_override"),
            orig_tgt1_spot=data.get("orig_tgt1_spot"),
            orig_tgt2_spot=data.get("orig_tgt2_spot"),
            reentry_granted=bool(data.get("reentry_granted")),
            runway_pts=data.get("runway_pts"), runway_wall=data.get("runway_wall"),
            strategy=data.get("strategy", "BASE"), zone_id=data.get("zone_id"),
            trigger_line=data.get("trigger_line"),
            booked_fraction=data.get("booked_fraction"),
            risk_units=data.get("risk_units", 1.0),
            oi_flip_pending=bool(data.get("oi_flip_pending", False)))
