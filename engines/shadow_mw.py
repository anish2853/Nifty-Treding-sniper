"""SHADOW ARM: M/W LIQUIDITY SNIPER (Parallel Measurement Engine).

FROZEN-v1 UNTOUCHED — Pure measurement and shadow execution.

Mechanics:
1. Level Set (09:15 daily):
   - PDH / PDL
   - Yesterday's two highest-volume swing points (from closed candle history)
   - OI walls at those zones (from chain)
   - Confluence tag: level + OI + swing = STRONG
2. W Pattern (LONG) / M Pattern (SHORT):
   - First touch of STRONG level: candle closes back beyond level (rejection #1)
   - Pullback (the middle) -> neckline = middle high (W) / middle low (M)
   - Second touch: may sweep beyond (the stop hunt)
   - CONFIRMATION: candle closes back beyond level, body >= 50%
   - MSS: neckline break with displacement
   - ENTRY: retest of neckline/level + rejection candle
   - SL: below W lows / above M highs, max 33 pts
   - TARGET: projected pattern height (TGT1) + opposite liquidity (TGT2)
   - PURE CANDLE LOGIC: No EMA, no VWAP gates in this arm (that IS the experiment)
3. Management:
   - Existing stack: Intrabar/candle SL, structural trail, 15:10 IST square-off
4. Measurement:
   - Journal tag: SHADOW-MW (separate from FROZEN-v1)
   - Every candidate logged: pattern -> confirmed/failed -> entry -> result
   - Side-by-side scorecard: FROZEN-v1 vs SHADOW-MW (trades, WR, expectancy)
   - Experiment analysis: Did dropping EMA/VWAP gates help or hurt?
   - 20+ shadow signals -> data decides promotion to an EXP doc
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

import settings
from engines.candles import Candle
from utils import ist_now

log = logging.getLogger("shadow_mw")


@dataclass
class MWLevel:
    name: str                   # "PDH", "PDL", "SWING_HIGH_1", "SWING_LOW_1", etc.
    price: float
    level_type: str             # "SUPPORT" (for W) or "RESISTANCE" (for M)
    volume: float = 0.0
    has_oi_wall: bool = False
    oi_strike: float | None = None
    is_swing: bool = False
    is_pd: bool = False
    tag: str = "NORMAL"         # "STRONG" when confluence criteria met

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MWPatternCandidate:
    candidate_id: str
    level_name: str
    level_price: float
    pattern: str                # "W" (LONG) or "M" (SHORT)
    state: str = "ARMED"        # ARMED, TOUCH_1, PULLBACK, TOUCH_2, CONFIRMED, MSS, RETEST_ARMED, ENTERED, FAILED
    touch1_price: float = 0.0
    touch1_time: str | None = None
    neckline_price: float = 0.0
    neckline_time: str | None = None
    touch2_price: float = 0.0
    touch2_time: str | None = None
    confirmed_time: str | None = None
    mss_time: str | None = None
    retest_time: str | None = None
    candles_tracked: int = 0
    failure_reason: str | None = None
    w_lows: float = 0.0
    m_highs: float = 0.0
    rejection_body_pct: float = 0.0
    # Fibonacci Levels
    fib_50: float = 0.0
    fib_618: float = 0.0
    fib_705: float = 0.0
    fib_target_272: float = 0.0
    fib_target_618: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShadowMWTrade:
    trade_id: str
    pattern: str                # "W" or "M"
    direction: str              # "LONG" or "SHORT"
    level_name: str
    level_price: float
    entry_price: float
    initial_sl: float
    current_sl: float
    risk_pts: float
    tgt1_price: float
    tgt2_price: float
    entry_time: str
    status: str = "OPEN"        # "OPEN", "PARTIAL_TGT1", "CLOSED"
    exit_price: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    realized_r: float = 0.0
    half_booked: bool = False
    trail_sl: float | None = None
    # Fibonacci Levels
    fib_50: float = 0.0
    fib_618: float = 0.0
    fib_705: float = 0.0
    fib_target_272: float = 0.0
    fib_target_618: float = 0.0
    # EMA / VWAP gates experiment measurement
    ema20_at_entry: float | None = None
    vwap_at_entry: float | None = None
    ema_aligned: bool = False
    vwap_aligned: bool = False
    counter_gate: bool = False  # True if entered against EMA or VWAP
    notes: str = ""
    # Option Chain Execution Fields
    strike: int = 0
    option_type: str = ""       # "CE" or "PE"
    option_symbol: str = ""     # e.g. "NIFTY 22550 CE"
    expiry: str = ""            # e.g. "15-OCT-2026"
    entry_prem: float = 0.0     # Option Buy Price / LTP
    sl_prem: float = 0.0        # Option Stop Loss Premium
    tgt1_prem: float = 0.0      # Option Target 1 Premium
    tgt2_prem: float = 0.0      # Option Target 2 Premium
    exit_prem: float | None = None
    setup_name: str = ""

    def __post_init__(self):
        if not self.setup_name and self.pattern and self.level_name:
            self.setup_name = f"{self.pattern} Liquidity Sweep ({self.level_name})"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ShadowMWEngine:
    """Parallel Shadow Arm for M/W Liquidity Sniper."""

    def __init__(self, store=None, today_iso: str | None = None, data_file: Path | None = None, isolated: bool = False, notifier: Any = None):
        self.store = store
        self.today_iso = today_iso or ist_now().date().isoformat()
        self.isolated = isolated
        self.notifier = notifier
        self.chain_snapshot: Any = None
        if isolated:
            self.data_file = None
        elif data_file is not None:
            self.data_file = data_file
        elif store and hasattr(store, "db_path") and store.db_path and Path(store.db_path).parent != settings.DATA_DIR:
            self.data_file = Path(store.db_path).parent / "shadow_mw_ledger.json"
        else:
            self.data_file = settings.DATA_DIR / "shadow_mw_ledger.json"

        self.levels: list[MWLevel] = []
        self.candidates: list[MWPatternCandidate] = []
        self.trades: list[ShadowMWTrade] = []
        self.levels_initialized: bool = False

        if not self.isolated:
            self._load()

    # -- Persistence -------------------------------------------------------------
    def _load(self) -> None:
        if self.isolated or self.data_file is None or not self.data_file.exists():
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            file_today = data.get("today_iso")
            if file_today == self.today_iso:
                self.trades = [ShadowMWTrade(**t) for t in data.get("trades", [])]
                self.candidates = [MWPatternCandidate(**c) for c in data.get("candidates", [])]
                self.levels = [MWLevel(**lvl) for lvl in data.get("levels", [])]
                if self.levels:
                    self.levels_initialized = True
            else:
                # Prior day data: only keep closed historical trades for running stats;
                # NEVER load stale levels or active candidates from previous sessions!
                self.trades = [ShadowMWTrade(**t) for t in data.get("trades", []) if t.get("status") == "CLOSED"]
                self.candidates = []
                self.levels = []
                self.levels_initialized = False
        except Exception as exc:
            log.warning("Could not load shadow MW ledger: %s", exc)

    def save(self) -> None:
        if self.isolated or self.data_file is None:
            return
        try:
            payload = {
                "updated_at": ist_now().isoformat(),
                "today_iso": self.today_iso,
                "levels": [lvl.to_dict() for lvl in self.levels],
                "candidates": [c.to_dict() for c in self.candidates],
                "trades": [t.to_dict() for t in self.trades],
            }
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            log.warning("Could not save shadow MW ledger: %s", exc)

    # -- 1. Level Set (09:15 Daily) ----------------------------------------------
    def initialize_levels(self, market, today_iso: str | None = None) -> list[MWLevel]:
        """Compute PDH/PDL + yesterday's 2 highest-volume swing points + OI walls.
        
        Tags confluence: level + OI + swing = STRONG.
        """
        self.today_iso = today_iso or self.today_iso
        levels: list[MWLevel] = []

        pdh = getattr(market, "pdh", None)
        pdl = getattr(market, "pdl", None)
        if pdh is None and getattr(market, "prev_day", None):
            pdh = market.prev_day.value.get("pdh")
        if pdl is None and getattr(market, "prev_day", None):
            pdl = market.prev_day.value.get("pdl")

        # 1. Fetch yesterday's closed candles to find high-volume swing points
        yesterday_candles = self._fetch_yesterday_candles()
        swing_highs, swing_lows = self._find_swing_points(yesterday_candles)

        # 2. Extract key OI walls from chain
        call_wall, put_wall = self._extract_oi_walls(market)

        # 3. Add PDH (Resistance) & PDL (Support)
        if pdh is not None and pdh > 0:
            has_oi = bool(call_wall and abs(pdh - call_wall) <= 25.0)
            levels.append(MWLevel(
                name="PDH",
                price=float(pdh),
                level_type="RESISTANCE",
                has_oi_wall=has_oi,
                oi_strike=call_wall if has_oi else None,
                is_pd=True,
                tag="STRONG" if has_oi else "NORMAL",
            ))

        if pdl is not None and pdl > 0:
            has_oi = bool(put_wall and abs(pdl - put_wall) <= 25.0)
            levels.append(MWLevel(
                name="PDL",
                price=float(pdl),
                level_type="SUPPORT",
                has_oi_wall=has_oi,
                oi_strike=put_wall if has_oi else None,
                is_pd=True,
                tag="STRONG" if has_oi else "NORMAL",
            ))

        # 4. Add Top 2 Highest-Volume Swing Points
        all_swings = []
        for sh in swing_highs:
            all_swings.append(("SWING_HIGH", sh[0], sh[1], "RESISTANCE"))
        for sl in swing_lows:
            all_swings.append(("SWING_LOW", sl[0], sl[1], "SUPPORT"))

        # Sort by volume descending
        all_swings.sort(key=lambda x: x[2], reverse=True)
        top_swings = all_swings[:2]

        for idx, (s_name, price, vol, l_type) in enumerate(top_swings, start=1):
            wall_match = call_wall if l_type == "RESISTANCE" else put_wall
            has_oi = bool(wall_match and abs(price - wall_match) <= 25.0)
            # Confluence: level + OI + swing
            near_pd = bool((pdh and abs(price - pdh) <= 15.0) or (pdl and abs(price - pdl) <= 15.0))
            is_strong = has_oi or near_pd

            levels.append(MWLevel(
                name=f"{s_name}_{idx}",
                price=float(price),
                level_type=l_type,
                volume=float(vol),
                has_oi_wall=has_oi,
                oi_strike=wall_match if has_oi else None,
                is_swing=True,
                tag="STRONG" if is_strong else "NORMAL",
            ))

        # Ensure at least PDH and PDL exist as STRONG if no swing points available
        if not any(lvl.tag == "STRONG" for lvl in levels) and levels:
            for lvl in levels:
                lvl.tag = "STRONG"

        self.levels = levels
        self.levels_initialized = True
        self.save()

        strong_names = [f"{l.name} ({l.price:.1f})" for l in levels if l.tag == "STRONG"]
        log.info("[SHADOW-MW] Levels initialized for %s. STRONG: %s", self.today_iso, ", ".join(strong_names))
        return self.levels

    def _fetch_yesterday_candles(self) -> list[dict]:
        """Fetch closed candles of the previous trading day from SQLite store."""
        if not self.store:
            return []
        try:
            rows = self.store.conn.execute(
                "SELECT start, open, high, low, close, volume FROM candles "
                "WHERE substr(start, 1, 10) < ? ORDER BY start DESC LIMIT 150",
                (self.today_iso,)
            ).fetchall()
            if not rows:
                return []
            # Group by most recent date
            prev_date = rows[0][0][:10]
            day_rows = [r for r in rows if r[0][:10] == prev_date]
            day_rows.reverse()
            candles = []
            for r in day_rows:
                candles.append({
                    "start": r[0], "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4]),
                    "volume": float(r[5]) if r[5] is not None else 0.0,
                })
            return candles
        except Exception as exc:
            log.warning("Could not fetch yesterday candles: %s", exc)
            return []

    def _find_swing_points(self, candles: list[dict]) -> tuple[list[tuple], list[tuple]]:
        """Find fractal swing highs and swing lows weighted by volume."""
        swing_highs = []
        swing_lows = []
        if len(candles) < 3:
            return [], []

        for i in range(1, len(candles) - 1):
            prev_c = candles[i - 1]
            cur_c = candles[i]
            next_c = candles[i + 1]

            # Swing High
            if cur_c["high"] > prev_c["high"] and cur_c["high"] > next_c["high"]:
                swing_highs.append((cur_c["high"], cur_c.get("volume", 0.0), cur_c["start"]))

            # Swing Low
            if cur_c["low"] < prev_c["low"] and cur_c["low"] < next_c["low"]:
                swing_lows.append((cur_c["low"], cur_c.get("volume", 0.0), cur_c["start"]))

        return swing_highs, swing_lows

    def _extract_oi_walls(self, market) -> tuple[float | None, float | None]:
        """Identify nearest Call and Put OI walls from market option chain."""
        chain = getattr(market, "chain", None)
        if not chain or not getattr(chain, "rows", None):
            return None, None

        call_wall = None
        put_wall = None
        max_call_oi = 0
        max_put_oi = 0

        for r in chain.rows:
            if r.ce_oi and r.ce_oi > max_call_oi:
                max_call_oi = r.ce_oi
                call_wall = float(r.strike)
            if r.pe_oi and r.pe_oi > max_put_oi:
                max_put_oi = r.pe_oi
                put_wall = float(r.strike)

        return call_wall, put_wall

    # -- 2. Candle Evaluation & State Machine ------------------------------------
    def on_candle(
        self,
        candle: Candle,
        spot: float | None = None,
        chain: Any = None,
        now: datetime | None = None,
        day_state: Any = None,
    ) -> list[ShadowMWTrade]:
        """Process 5-minute candle close across active candidates and open trades."""
        now = now or ist_now()

        # 1. Update open shadow trades against candle
        self._update_open_trades(candle, now)

        # 2. Check if levels need initialization
        if not self.levels_initialized and self.store:
            # Re-attempt level initialization if not yet done
            fake_market = type("M", (), {
                "pdh": candle.high, "pdl": candle.low, "chain": chain
            })()
            self.initialize_levels(fake_market, candle.start.date().isoformat())

        # 3. Evaluate pattern candidates against STRONG levels
        strong_levels = [lvl for lvl in self.levels if lvl.tag == "STRONG"]
        for lvl in strong_levels:
            if lvl.level_type == "SUPPORT":
                self._evaluate_w_pattern(lvl, candle, day_state)
            elif lvl.level_type == "RESISTANCE":
                self._evaluate_m_pattern(lvl, candle, day_state)

        self.save()
        return [t for t in self.trades if t.status == "OPEN"]

    def _update_open_trades(self, candle: Candle, now: datetime) -> None:
        """Manage active shadow positions: intrabar SL, structural trail, targets."""
        for trade in self.trades:
            if trade.status == "CLOSED":
                continue

            c_time_str = candle.start.strftime("%H:%M")

            if trade.direction == "LONG":
                # Check Stop Loss
                if candle.low <= trade.current_sl:
                    if trade.half_booked:
                        trade.realized_r = 0.50
                        trade.exit_reason = "BE_SL"
                        trade.notes = "Partial TGT1 (+1.0R) + Breakeven Stop Locked (+0.50R)"
                        trade.exit_prem = trade.entry_prem
                    else:
                        trade.realized_r = -1.00
                        trade.exit_reason = "HARD_SL"
                        trade.notes = "Initial Stop Loss Hit (-1.0R)"
                        trade.exit_prem = trade.sl_prem
                    trade.status = "CLOSED"
                    trade.exit_price = trade.current_sl
                    trade.exit_time = candle.start.isoformat()
                    self._journal_event("SHADOW_EXIT", trade)
                    if self.notifier:
                        self.notifier("EXIT", trade)
                    log.info("[SHADOW-MW] Trade %s SL hit at %.1f (%.2fR)", trade.trade_id, trade.exit_price, trade.realized_r)
                    continue

                # Check TGT1
                if not trade.half_booked and candle.high >= trade.tgt1_price:
                    trade.half_booked = True
                    trade.status = "PARTIAL_TGT1"
                    trade.current_sl = trade.entry_price  # Move SL to Breakeven
                    trade.notes = f"50% booked at TGT1 ({trade.tgt1_price:.1f}), SL to BE"
                    self._journal_event("SHADOW_MW", trade, notes=f"[SHADOW-MW] {trade.trade_id} TGT1 hit! 50% booked, SL moved to BE")
                    if self.notifier:
                        self.notifier("TGT1", trade)
                    log.info("[SHADOW-MW] Trade %s reached TGT1 at %.1f. SL moved to Breakeven.", trade.trade_id, trade.tgt1_price)

                # Check TGT2
                if trade.half_booked and candle.high >= trade.tgt2_price:
                    t1_r = max(1.0, (trade.tgt1_price - trade.entry_price) / trade.risk_pts)
                    t2_r = max(1.5, (trade.tgt2_price - trade.entry_price) / trade.risk_pts)
                    trade.realized_r = round(0.50 * t1_r + 0.50 * t2_r, 2)
                    trade.status = "CLOSED"
                    trade.exit_price = trade.tgt2_price
                    trade.exit_time = candle.start.isoformat()
                    trade.exit_reason = "TGT2_HIT"
                    trade.notes = f"Full Target 2 Hit (+{trade.realized_r:.2f}R)"
                    trade.exit_prem = trade.tgt2_prem
                    self._journal_event("SHADOW_EXIT", trade)
                    if self.notifier:
                        self.notifier("EXIT", trade)
                    log.info("[SHADOW-MW] Trade %s reached TGT2 at %.1f (+%.2fR)", trade.trade_id, trade.tgt2_price, trade.realized_r)
                    continue

                # Structural Trailing
                if trade.half_booked and candle.low > trade.current_sl:
                    trade.current_sl = max(trade.current_sl, candle.low - 2.0)

            else:  # SHORT
                # Check Stop Loss
                if candle.high >= trade.current_sl:
                    if trade.half_booked:
                        trade.realized_r = 0.50
                        trade.exit_reason = "BE_SL"
                        trade.notes = "Partial TGT1 (+1.0R) + Breakeven Stop Locked (+0.50R)"
                        trade.exit_prem = trade.entry_prem
                    else:
                        trade.realized_r = -1.00
                        trade.exit_reason = "HARD_SL"
                        trade.notes = "Initial Stop Loss Hit (-1.0R)"
                        trade.exit_prem = trade.sl_prem
                    trade.status = "CLOSED"
                    trade.exit_price = trade.current_sl
                    trade.exit_time = candle.start.isoformat()
                    self._journal_event("SHADOW_EXIT", trade)
                    if self.notifier:
                        self.notifier("EXIT", trade)
                    log.info("[SHADOW-MW] Trade %s SL hit at %.1f (%.2fR)", trade.trade_id, trade.exit_price, trade.realized_r)
                    continue

                # Check TGT1
                if not trade.half_booked and candle.low <= trade.tgt1_price:
                    trade.half_booked = True
                    trade.status = "PARTIAL_TGT1"
                    trade.current_sl = trade.entry_price  # Move SL to Breakeven
                    trade.notes = f"50% booked at TGT1 ({trade.tgt1_price:.1f}), SL to BE"
                    self._journal_event("SHADOW_MW", trade, notes=f"[SHADOW-MW] {trade.trade_id} TGT1 hit! 50% booked, SL moved to BE")
                    if self.notifier:
                        self.notifier("TGT1", trade)
                    log.info("[SHADOW-MW] Trade %s reached TGT1 at %.1f. SL moved to Breakeven.", trade.trade_id, trade.tgt1_price)

                # Check TGT2
                if trade.half_booked and candle.low <= trade.tgt2_price:
                    t1_r = max(1.0, (trade.entry_price - trade.tgt1_price) / trade.risk_pts)
                    t2_r = max(1.5, (trade.entry_price - trade.tgt2_price) / trade.risk_pts)
                    trade.realized_r = round(0.50 * t1_r + 0.50 * t2_r, 2)
                    trade.status = "CLOSED"
                    trade.exit_price = trade.tgt2_price
                    trade.exit_time = candle.start.isoformat()
                    trade.exit_reason = "TGT2_HIT"
                    trade.notes = f"Full Target 2 Hit (+{trade.realized_r:.2f}R)"
                    trade.exit_prem = trade.tgt2_prem
                    self._journal_event("SHADOW_EXIT", trade)
                    if self.notifier:
                        self.notifier("EXIT", trade)
                    log.info("[SHADOW-MW] Trade %s reached TGT2 at %.1f (+%.2fR)", trade.trade_id, trade.tgt2_price, trade.realized_r)
                    continue

                # Structural Trailing
                if trade.half_booked and candle.high < trade.current_sl:
                    trade.current_sl = min(trade.current_sl, candle.high + 2.0)

    # -- W Pattern State Machine (LONG) ------------------------------------------
    def _evaluate_w_pattern(self, level: MWLevel, candle: Candle, day_state: Any) -> None:
        """Evaluate W pattern development at a STRONG support level."""
        L = level.price
        c_range = max(0.1, candle.high - candle.low)
        body = abs(candle.close - candle.open)
        body_pct = body / c_range

        # Find or create candidate for this level
        cand = next((c for c in self.candidates if c.level_name == level.name and c.pattern == "W" and c.state != "FAILED" and c.state != "ENTERED"), None)

        if not cand:
            # Check for TOUCH_1: Dips to support zone and closes back above (rejection #1)
            if candle.low <= L + 10.0 and candle.close > L and (candle.close - candle.low) >= 3.0:
                c_id = f"W-{level.name}-{candle.start.strftime('%H%M')}"
                cand = MWPatternCandidate(
                    candidate_id=c_id,
                    level_name=level.name,
                    level_price=L,
                    pattern="W",
                    state="TOUCH_1",
                    touch1_price=candle.low,
                    touch1_time=candle.start.isoformat(),
                    w_lows=candle.low,
                    notes=f"Touch #1 on {level.name} at {candle.low:.1f}",
                )
                self.candidates.append(cand)
                self._journal_candidate(cand, f"Touch #1 at {level.name} ({candle.low:.1f})")
                log.info("[SHADOW-MW] New W candidate %s: Touch #1 at %.1f", c_id, candle.low)
            return

        cand.candles_tracked += 1

        # Timeout invalidation: if trapped > 14 candles without progressing
        if cand.candles_tracked > 14 and cand.state in ("TOUCH_1", "PULLBACK", "TOUCH_2"):
            cand.state = "FAILED"
            cand.failure_reason = "Timeout: Pattern stalled > 70m"
            self._journal_candidate(cand, "FAILED: Timeout")
            return

        # Hard invalidation: breakdown > 35 pts below support
        if candle.close < L - 35.0:
            cand.state = "FAILED"
            cand.failure_reason = f"Support breakdown: close {candle.close:.1f} < L-35"
            self._journal_candidate(cand, "FAILED: Support broken")
            return

        # State 1: TOUCH_1 -> Look for Pullback (middle of the W)
        if cand.state == "TOUCH_1":
            if candle.high > L + 8.0:
                cand.state = "PULLBACK"
                cand.neckline_price = candle.high
                cand.neckline_time = candle.start.isoformat()
                cand.notes = f"Pullback forming: Neckline at {candle.high:.1f}"
                log.info("[SHADOW-MW] %s: Pullback high at %.1f", cand.candidate_id, cand.neckline_price)

        # State 2: PULLBACK -> Update neckline or look for Touch 2
        elif cand.state == "PULLBACK":
            cand.neckline_price = max(cand.neckline_price, candle.high)
            # Second touch: returns to support level zone (may sweep beyond)
            if candle.low <= L + 12.0 and (cand.neckline_price - L) >= 8.0:
                cand.touch2_price = candle.low
                cand.touch2_time = candle.start.isoformat()
                cand.w_lows = min(cand.touch1_price, cand.touch2_price)

                # Check if this candle also qualifies as CONFIRMATION (close > L, body >= 50%)
                if candle.close > L and candle.close > candle.open and body_pct >= 0.50:
                    cand.state = "CONFIRMED"
                    cand.confirmed_time = candle.start.isoformat()
                    cand.rejection_body_pct = body_pct
                    cand.notes = f"Confirmed on Touch 2! Close {candle.close:.1f} > L, Body {body_pct:.1%}"
                    self._journal_candidate(cand, f"CONFIRMED on Touch 2 (Body: {body_pct:.1%})")
                    log.info("[SHADOW-MW] %s: CONFIRMED on Touch 2 (Body: %.1f%%)", cand.candidate_id, body_pct * 100)
                else:
                    cand.state = "TOUCH_2"
                    cand.notes = f"Touch #2 at {candle.low:.1f} (sweep: {candle.low < cand.touch1_price})"
                    log.info("[SHADOW-MW] %s: Touch #2 at %.1f (sweep: %s)", cand.candidate_id, candle.low, candle.low < cand.touch1_price)

        # State 3: TOUCH_2 -> Waiting for CONFIRMATION candle
        elif cand.state == "TOUCH_2":
            cand.touch2_price = min(cand.touch2_price, candle.low)
            cand.w_lows = min(cand.touch1_price, cand.touch2_price)
            if candle.close > L and candle.close > candle.open and body_pct >= 0.50:
                cand.state = "CONFIRMED"
                cand.confirmed_time = candle.start.isoformat()
                cand.rejection_body_pct = body_pct
                cand.notes = f"Confirmed! Close {candle.close:.1f} > L, Body {body_pct:.1%}"
                self._journal_candidate(cand, f"CONFIRMED (Body: {body_pct:.1%})")
                log.info("[SHADOW-MW] %s: CONFIRMED (Body: %.1f%%)", cand.candidate_id, body_pct * 100)

        # State 4: CONFIRMED -> Waiting for MSS (Neckline break with displacement)
        elif cand.state == "CONFIRMED":
            if candle.low < cand.w_lows - 5.0:
                cand.state = "FAILED"
                cand.failure_reason = "Broke below W lows before MSS"
                self._journal_candidate(cand, "FAILED: W lows violated")
                return

            if candle.close > cand.neckline_price and candle.close > candle.open:
                cand.state = "MSS"
                cand.mss_time = candle.start.isoformat()
                # Compute Fibonacci levels on Impulse Leg: [sweep_low (1.0) -> mss_high (0.0)]
                sweep_low = cand.w_lows if cand.w_lows > 0 else (cand.touch1_price or candle.low)
                mss_high = candle.close
                H = max(10.0, mss_high - sweep_low)
                cand.fib_50 = round(mss_high - (0.50 * H), 2)
                cand.fib_618 = round(mss_high - (0.618 * H), 2)
                cand.fib_705 = round(mss_high - (0.705 * H), 2)
                cand.fib_target_272 = round(mss_high + (0.272 * H), 2)
                cand.fib_target_618 = round(mss_high + (0.618 * H), 2)
                cand.notes = f"MSS: Neckline {cand.neckline_price:.1f} broken to {candle.close:.1f} | Fib Golden Pocket: [{cand.fib_705:.1f} - {cand.fib_50:.1f}]"
                self._journal_candidate(cand, f"MSS: Neckline {cand.neckline_price:.1f} broken")
                log.info("[SHADOW-MW] %s: MSS triggered! Fib Golden Pocket: %.1f - %.1f | Fib Targets: %.1f / %.1f",
                         cand.candidate_id, cand.fib_705, cand.fib_50, cand.fib_target_272, cand.fib_target_618)

        # State 5: MSS -> Waiting for Retest of neckline/Fibonacci OTE + rejection candle
        elif cand.state in ("MSS", "RETEST_ARMED"):
            cand.state = "RETEST_ARMED"
            neck = cand.neckline_price
            # Retest criteria: touches Fibonacci Golden Pocket or neckline zone and closes back above with rejection
            fib_test = bool(cand.fib_50 > 0 and candle.low <= cand.fib_50 + 5.0 and candle.low >= cand.w_lows - 2.0)
            neck_test = bool(candle.low <= neck + 8.0 and candle.low >= neck - 20.0)
            tested = fib_test or neck_test
            rejected = candle.close >= neck or (candle.close > candle.open and (candle.close - candle.low) >= 0.4 * c_range)

            if tested and rejected:
                # ENTRY TRIGGERED!
                cand.state = "ENTERED"
                cand.retest_time = candle.start.isoformat()
                self._execute_shadow_entry(
                    candidate=cand,
                    direction="LONG",
                    candle=candle,
                    day_state=day_state,
                )

    # -- M Pattern State Machine (SHORT) -----------------------------------------
    def _evaluate_m_pattern(self, level: MWLevel, candle: Candle, day_state: Any) -> None:
        """Evaluate M pattern development at a STRONG resistance level."""
        L = level.price
        c_range = max(0.1, candle.high - candle.low)
        body = abs(candle.close - candle.open)
        body_pct = body / c_range

        cand = next((c for c in self.candidates if c.level_name == level.name and c.pattern == "M" and c.state != "FAILED" and c.state != "ENTERED"), None)

        if not cand:
            # Check for TOUCH_1: Rallies to resistance zone and closes back below (rejection #1)
            if candle.high >= L - 10.0 and candle.close < L and (candle.high - candle.close) >= 3.0:
                c_id = f"M-{level.name}-{candle.start.strftime('%H%M')}"
                cand = MWPatternCandidate(
                    candidate_id=c_id,
                    level_name=level.name,
                    level_price=L,
                    pattern="M",
                    state="TOUCH_1",
                    touch1_price=candle.high,
                    touch1_time=candle.start.isoformat(),
                    m_highs=candle.high,
                    notes=f"Touch #1 on {level.name} at {candle.high:.1f}",
                )
                self.candidates.append(cand)
                self._journal_candidate(cand, f"Touch #1 at {level.name} ({candle.high:.1f})")
                log.info("[SHADOW-MW] New M candidate %s: Touch #1 at %.1f", c_id, candle.high)
            return

        cand.candles_tracked += 1

        if cand.candles_tracked > 14 and cand.state in ("TOUCH_1", "PULLBACK", "TOUCH_2"):
            cand.state = "FAILED"
            cand.failure_reason = "Timeout: Pattern stalled > 70m"
            self._journal_candidate(cand, "FAILED: Timeout")
            return

        if candle.close > L + 35.0:
            cand.state = "FAILED"
            cand.failure_reason = f"Resistance breakout: close {candle.close:.1f} > L+35"
            self._journal_candidate(cand, "FAILED: Resistance broken")
            return

        # State 1: TOUCH_1 -> Look for Pullback (middle of the M)
        if cand.state == "TOUCH_1":
            if candle.low < L - 8.0:
                cand.state = "PULLBACK"
                cand.neckline_price = candle.low
                cand.neckline_time = candle.start.isoformat()
                cand.notes = f"Pullback forming: Neckline at {candle.low:.1f}"
                log.info("[SHADOW-MW] %s: Pullback low at %.1f", cand.candidate_id, cand.neckline_price)

        # State 2: PULLBACK -> Update neckline or look for Touch 2
        elif cand.state == "PULLBACK":
            cand.neckline_price = min(cand.neckline_price, candle.low)
            if candle.high >= L - 12.0 and (L - cand.neckline_price) >= 8.0:
                cand.touch2_price = candle.high
                cand.touch2_time = candle.start.isoformat()
                cand.m_highs = max(cand.touch1_price, cand.touch2_price)

                if candle.close < L and candle.close < candle.open and body_pct >= 0.50:
                    cand.state = "CONFIRMED"
                    cand.confirmed_time = candle.start.isoformat()
                    cand.rejection_body_pct = body_pct
                    cand.notes = f"Confirmed on Touch 2! Close {candle.close:.1f} < L, Body {body_pct:.1%}"
                    self._journal_candidate(cand, f"CONFIRMED on Touch 2 (Body: {body_pct:.1%})")
                    log.info("[SHADOW-MW] %s: CONFIRMED on Touch 2 (Body: %.1f%%)", cand.candidate_id, body_pct * 100)
                else:
                    cand.state = "TOUCH_2"
                    cand.notes = f"Touch #2 at {candle.high:.1f} (sweep: {candle.high > cand.touch1_price})"
                    log.info("[SHADOW-MW] %s: Touch #2 at %.1f (sweep: %s)", cand.candidate_id, candle.high, candle.high > cand.touch1_price)

        # State 3: TOUCH_2 -> Waiting for CONFIRMATION candle
        elif cand.state == "TOUCH_2":
            cand.touch2_price = max(cand.touch2_price, candle.high)
            cand.m_highs = max(cand.touch1_price, cand.touch2_price)
            if candle.close < L and candle.close < candle.open and body_pct >= 0.50:
                cand.state = "CONFIRMED"
                cand.confirmed_time = candle.start.isoformat()
                cand.rejection_body_pct = body_pct
                cand.notes = f"Confirmed! Close {candle.close:.1f} < L, Body {body_pct:.1%}"
                self._journal_candidate(cand, f"CONFIRMED (Body: {body_pct:.1%})")
                log.info("[SHADOW-MW] %s: CONFIRMED (Body: %.1f%%)", cand.candidate_id, body_pct * 100)

        # State 4: CONFIRMED -> Waiting for MSS
        elif cand.state == "CONFIRMED":
            if candle.high > cand.m_highs + 5.0:
                cand.state = "FAILED"
                cand.failure_reason = "Broke above M highs before MSS"
                self._journal_candidate(cand, "FAILED: M highs violated")
                return

            if candle.close < cand.neckline_price and candle.close < candle.open:
                cand.state = "MSS"
                cand.mss_time = candle.start.isoformat()
                # Compute Fibonacci levels on Impulse Leg: [sweep_high (1.0) -> mss_low (0.0)]
                sweep_high = cand.m_highs if cand.m_highs > 0 else (cand.touch1_price or candle.high)
                mss_low = candle.close
                H = max(10.0, sweep_high - mss_low)
                cand.fib_50 = round(mss_low + (0.50 * H), 2)
                cand.fib_618 = round(mss_low + (0.618 * H), 2)
                cand.fib_705 = round(mss_low + (0.705 * H), 2)
                cand.fib_target_272 = round(mss_low - (0.272 * H), 2)
                cand.fib_target_618 = round(mss_low - (0.618 * H), 2)
                cand.notes = f"MSS: Neckline {cand.neckline_price:.1f} broken to {candle.close:.1f} | Fib Golden Pocket: [{cand.fib_50:.1f} - {cand.fib_705:.1f}]"
                self._journal_candidate(cand, f"MSS: Neckline {cand.neckline_price:.1f} broken")
                log.info("[SHADOW-MW] %s: MSS triggered! Fib Golden Pocket: %.1f - %.1f | Fib Targets: %.1f / %.1f",
                         cand.candidate_id, cand.fib_50, cand.fib_705, cand.fib_target_272, cand.fib_target_618)

        # State 5: MSS -> Retest + rejection candle
        elif cand.state in ("MSS", "RETEST_ARMED"):
            cand.state = "RETEST_ARMED"
            neck = cand.neckline_price
            fib_test = bool(cand.fib_50 > 0 and candle.high >= cand.fib_50 - 5.0 and candle.high <= cand.m_highs + 2.0)
            neck_test = bool(candle.high >= neck - 8.0 and candle.high <= neck + 20.0)
            tested = fib_test or neck_test
            rejected = candle.close <= neck or (candle.close < candle.open and (candle.high - candle.close) >= 0.4 * c_range)

            if tested and rejected:
                cand.state = "ENTERED"
                cand.retest_time = candle.start.isoformat()
                self._execute_shadow_entry(
                    candidate=cand,
                    direction="SHORT",
                    candle=candle,
                    day_state=day_state,
                )

    # -- 3. Trade Entry & Experiment Sizing --------------------------------------
    def _execute_shadow_entry(
        self,
        candidate: MWPatternCandidate,
        direction: str,
        candle: Candle,
        day_state: Any,
    ) -> None:
        """Create and track shadow trade without EMA or VWAP gates (The Experiment)."""
        if getattr(self, "blocked", False):
            return
        if any(t.status in ("OPEN", "PARTIAL_TGT1") for t in self.trades):
            return

        # TIME GATE: Strict institutional entry cutoff
        # No new entries after 14:00 IST (avoids late-session chop and EOD theta crush; 15:10 is square-off)
        if candle.start.time() >= dt_time(14, 0):
            log.info("[SHADOW-MW] Entry blocked by TIME GATE (candle %s >= 14:00 IST)", candle.start.strftime("%H:%M"))
            return

        entry_price = candle.close
        trade_num = len(self.trades) + 1
        trade_id = f"SMW-{trade_num:03d}"

        if direction == "LONG":
            w_low = candidate.w_lows if candidate.w_lows > 0 else (candle.low - 5.0)
            raw_sl = min(candle.low - 3.0, w_low - 2.0)
            # Anchor to retest candle low with tight 20 pt max to compress MAE
            initial_sl = max(raw_sl, entry_price - 20.0)
            risk_pts = max(6.0, entry_price - initial_sl)
            # Fibonacci Targets (-0.272 and -0.618 institutional expansion):
            if candidate.fib_target_272 > entry_price:
                tgt1 = candidate.fib_target_272
            else:
                tgt1 = entry_price + max(15.0, candidate.neckline_price - w_low)
            if candidate.fib_target_618 > tgt1:
                tgt2 = candidate.fib_target_618
            else:
                tgt2 = entry_price + (2.5 * risk_pts)
        else:
            m_high = candidate.m_highs if candidate.m_highs > 0 else (candle.high + 5.0)
            raw_sl = max(candle.high + 3.0, m_high + 2.0)
            # Anchor to retest candle high with tight 20 pt max to compress MAE
            initial_sl = min(raw_sl, entry_price + 20.0)
            risk_pts = max(6.0, initial_sl - entry_price)
            # Fibonacci Targets (-0.272 and -0.618 institutional expansion):
            if candidate.fib_target_272 < entry_price and candidate.fib_target_272 > 0:
                tgt1 = candidate.fib_target_272
            else:
                tgt1 = entry_price - max(15.0, m_high - candidate.neckline_price)
            if candidate.fib_target_618 < tgt1 and candidate.fib_target_618 > 0:
                tgt2 = candidate.fib_target_618
            else:
                tgt2 = entry_price - (2.5 * risk_pts)

        # Experiment Measurement: Was this trade aligned with EMA20 and VWAP?
        ema20 = getattr(day_state, "ema20", None) if day_state else None
        vwap = getattr(day_state, "vwap", None) if day_state else None
        if direction == "LONG":
            ema_aligned = bool(ema20 and entry_price >= ema20)
            vwap_aligned = bool(vwap and entry_price >= vwap)
        else:
            ema_aligned = bool(ema20 and entry_price <= ema20)
            vwap_aligned = bool(vwap and entry_price <= vwap)

        counter_gate = not (ema_aligned and vwap_aligned)

        opt_leg = compute_option_leg(
            spot=entry_price,
            direction=direction,
            risk_pts=risk_pts,
            tgt1_price=tgt1,
            tgt2_price=tgt2,
            chain_snapshot=getattr(self, "chain_snapshot", None),
        )

        trade = ShadowMWTrade(
            trade_id=trade_id,
            pattern=candidate.pattern,
            direction=direction,
            level_name=candidate.level_name,
            level_price=candidate.level_price,
            entry_price=entry_price,
            initial_sl=initial_sl,
            current_sl=initial_sl,
            risk_pts=risk_pts,
            tgt1_price=tgt1,
            tgt2_price=tgt2,
            entry_time=candle.start.isoformat(),
            fib_50=candidate.fib_50,
            fib_618=candidate.fib_618,
            fib_705=getattr(candidate, "fib_705", 0.0),
            fib_target_272=candidate.fib_target_272,
            fib_target_618=candidate.fib_target_618,
            ema20_at_entry=ema20,
            vwap_at_entry=vwap,
            ema_aligned=ema_aligned,
            vwap_aligned=vwap_aligned,
            counter_gate=counter_gate,
            notes=f"Fibonacci Liquidity Entry {candidate.pattern} at {candidate.level_name} | Fib TGT1: {tgt1:.1f}, TGT2: {tgt2:.1f}",
            strike=opt_leg["strike"],
            option_type=opt_leg["option_type"],
            option_symbol=opt_leg["option_symbol"],
            expiry=opt_leg.get("expiry", ""),
            entry_prem=opt_leg["entry_prem"],
            sl_prem=opt_leg["sl_prem"],
            tgt1_prem=opt_leg["tgt1_prem"],
            tgt2_prem=opt_leg["tgt2_prem"],
            setup_name=f"{candidate.pattern} Liquidity Sweep ({candidate.level_name})",
        )
        self.trades.append(trade)
        self._journal_event("SHADOW_ENTRY", trade)
        if self.notifier:
            self.notifier("ENTRY", trade)
        log.info(
            "[SHADOW-MW] FIRED %s: %s %s @ %.1f | SL: %.1f (%.1f pts) | Fib TGT1: %.1f | Fib TGT2: %.1f | Counter-Gate: %s",
            trade_id, candidate.pattern, direction, entry_price, initial_sl, risk_pts, tgt1, tgt2, counter_gate,
        )

    # -- 4. Square-Off (15:10 IST) -----------------------------------------------
    def square_off(self, exit_price: float | None = None, exit_time: datetime | None = None) -> list[ShadowMWTrade]:
        """Forced 15:10 IST square-off for any active shadow positions."""
        exit_time = exit_time or ist_now()
        closed_trades = []
        for trade in self.trades:
            if trade.status == "CLOSED":
                continue
            px = exit_price or trade.entry_price
            pts = (px - trade.entry_price) if trade.direction == "LONG" else (trade.entry_price - px)
            runner_r = pts / trade.risk_pts
            if trade.half_booked:
                t1_r = max(1.0, abs(trade.tgt1_price - trade.entry_price) / trade.risk_pts)
                trade.realized_r = round(0.50 * t1_r + 0.50 * runner_r, 2)
            else:
                trade.realized_r = round(runner_r, 2)

            trade.status = "CLOSED"
            trade.exit_price = px
            trade.exit_time = exit_time.isoformat()
            trade.exit_reason = "SQUARE_OFF_1510"
            trade.notes = f"Forced 15:10 IST Square-Off (+{trade.realized_r:.2f}R)"
            beta = getattr(settings, "PREMIUM_BETA", 0.50)
            trade.exit_prem = max(0.5, round(trade.entry_prem + (pts * beta), 1))
            self._journal_event("SHADOW_EXIT", trade)
            if self.notifier:
                self.notifier("EXIT", trade)
            closed_trades.append(trade)
            log.info("[SHADOW-MW] %s 15:10 Square-Off @ %.1f (%+.2fR)", trade.trade_id, px, trade.realized_r)

        self.save()
        return closed_trades

    # -- 5. Journal Logging ------------------------------------------------------
    def _journal_candidate(self, cand: MWPatternCandidate, note: str) -> None:
        if not self.store:
            return
        try:
            self.store.journal_event(
                "SHADOW_MW",
                direction="LONG" if cand.pattern == "W" else "SHORT",
                spot=cand.level_price,
                family="SHADOW-MW",
                notes=f"[SHADOW-MW] Candidate {cand.candidate_id}: {note}",
                reasons=cand.to_dict(),
            )
        except Exception as exc:
            log.warning("Could not journal shadow candidate: %s", exc)

    def _journal_event(self, event_type: str, trade: ShadowMWTrade, notes: str | None = None) -> None:
        if not self.store:
            return
        try:
            event_ts = trade.exit_time if (event_type == "SHADOW_EXIT" and trade.exit_time) else trade.entry_time
            self.store.journal_event(
                event_type,
                direction=trade.direction,
                spot=trade.exit_price if event_type == "SHADOW_EXIT" else trade.entry_price,
                trade_id=trade.trade_id,
                result_r=trade.realized_r if event_type == "SHADOW_EXIT" else None,
                exit_reason=trade.exit_reason if event_type == "SHADOW_EXIT" else None,
                family="SHADOW-MW",
                notes=notes or f"[SHADOW-MW] {trade.trade_id} {event_type} | {trade.notes}",
                reasons=trade.to_dict(),
                ts=event_ts,
            )
        except Exception as exc:
            log.warning("Could not journal shadow trade event: %s", exc)

    # -- 6. Summary & Side-by-Side Analytics -------------------------------------
    def get_summary(self, today_iso: str | None = None) -> dict[str, Any]:
        """Generate comprehensive scorecard comparing FROZEN-v1 vs SHADOW-MW."""
        today_iso = today_iso or self.today_iso

        # Filter trades for today or cumulative
        closed = [t for t in self.trades if t.status == "CLOSED"]
        total_shadow_trades = len(closed)
        wins = [t for t in closed if t.realized_r > 0]
        losses = [t for t in closed if t.realized_r < 0]
        be = [t for t in closed if t.realized_r == 0]

        wr_pct = (len(wins) / total_shadow_trades * 100.0) if total_shadow_trades > 0 else 0.0
        exp_r = (sum(t.realized_r for t in closed) / total_shadow_trades) if total_shadow_trades > 0 else 0.0
        total_r = sum(t.realized_r for t in closed)

        # Max Drawdown in R
        running_r = 0.0
        peak_r = 0.0
        max_dd_r = 0.0
        for t in closed:
            running_r += t.realized_r
            if running_r > peak_r:
                peak_r = running_r
            dd = peak_r - running_r
            if dd > max_dd_r:
                max_dd_r = dd

        # Experiment Analysis: Did dropping EMA/VWAP gates help or hurt?
        counter_trades = [t for t in closed if t.counter_gate]
        counter_count = len(counter_trades)
        counter_wins = sum(1 for t in counter_trades if t.realized_r > 0)
        counter_r = sum(t.realized_r for t in counter_trades)
        counter_exp = (counter_r / counter_count) if counter_count > 0 else 0.0

        if counter_count == 0:
            verdict = "INCONCLUSIVE (0 counter-trend signals taken yet)"
        elif counter_r > 0:
            verdict = f"HELPED (+{counter_r:+.2f}R from {counter_count} counter-trend signals)"
        elif counter_r < 0:
            verdict = f"HURT ({counter_r:+.2f}R drag from {counter_count} counter-trend signals)"
        else:
            verdict = f"NEUTRAL (0.00R from {counter_count} counter-trend signals)"

        # FROZEN-v1 comparison numbers from store journal
        frozen_trades_count = 0
        frozen_wins = 0
        frozen_r = 0.0
        frozen_exp = 0.0
        if self.store:
            try:
                f_rows = self.store.conn.execute(
                    "SELECT result_r FROM journal WHERE type = 'EXIT' AND (family IS NULL OR family != 'SHADOW-MW') "
                    "AND result_r IS NOT NULL"
                ).fetchall()
                frozen_trades_count = len(f_rows)
                if frozen_trades_count > 0:
                    f_rs = [float(r[0]) for r in f_rows]
                    frozen_wins = sum(1 for r in f_rs if r > 0)
                    frozen_r = sum(f_rs)
                    frozen_exp = frozen_r / frozen_trades_count
            except Exception:
                pass

        frozen_wr = (frozen_wins / frozen_trades_count * 100.0) if frozen_trades_count > 0 else 0.0

        # Active candidates summary
        active_candidates = [c for c in self.candidates if c.state not in ("ENTERED", "FAILED")]

        return {
            "today_iso": today_iso,
            "levels": [lvl.to_dict() for lvl in self.levels],
            "strong_levels_count": sum(1 for lvl in self.levels if lvl.tag == "STRONG"),
            "active_candidates_count": len(active_candidates),
            "candidates_total": len(self.candidates),
            "open_trades_count": sum(1 for t in self.trades if t.status in ("OPEN", "PARTIAL_TGT1")),
            "closed_trades_count": total_shadow_trades,
            "shadow_stats": {
                "trades": total_shadow_trades,
                "wins": len(wins),
                "losses": len(losses),
                "win_rate_pct": round(wr_pct, 1),
                "expectancy_r": round(exp_r, 2),
                "total_r": round(total_r, 2),
                "max_dd_r": round(max_dd_r, 2),
            },
            "frozen_baseline": {
                "trades": frozen_trades_count,
                "win_rate_pct": round(frozen_wr, 1),
                "expectancy_r": round(frozen_exp, 2),
                "total_r": round(frozen_r, 2),
            },
            "experiment_analysis": {
                "counter_gate_trades": counter_count,
                "counter_gate_wins": counter_wins,
                "counter_gate_r": round(counter_r, 2),
                "counter_gate_expectancy": round(counter_exp, 2),
                "verdict": verdict,
                "promotion_progress": f"{total_shadow_trades} / 20",
                "promotion_ready": total_shadow_trades >= 20,
            },
            "recent_trades": [t.to_dict() for t in self.trades[-10:]],
            "active_candidates": [c.to_dict() for c in active_candidates],
        }


# =============================================================================
# ARM 2: LIQUIDITY TRENDLINE & FIBONACCI RETRACEMENT ENGINE
# =============================================================================

@dataclass
class LiquidityTrendlineTrade:
    trade_id: str
    direction: str              # "LONG" or "SHORT"
    entry_price: float
    initial_sl: float
    current_sl: float
    risk_pts: float
    tgt1_price: float           # Previous High / Low (Checkpoint Only)
    tgt2_price: float           # 1.272xH Fib Extension (Ride Target)
    entry_time: str
    fib_50: float = 0.0
    fib_618: float = 0.0
    fib_705: float = 0.0
    status: str = "OPEN"        # "OPEN", "CHECKPOINT_HIT", "WALL_PARTIAL", "CLOSED"
    exit_price: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    realized_r: float = 0.0
    half_booked: bool = False
    checkpoint_hit: bool = False
    wall_booked: bool = False
    booked_fraction: float = 0.0
    setup_name: str = ""
    notes: str = ""
    # Option Chain Execution Fields
    strike: int = 0
    option_type: str = ""       # "CE" or "PE"
    option_symbol: str = ""     # e.g. "NIFTY 22550 CE"
    expiry: str = ""            # e.g. "15-OCT-2026"
    entry_prem: float = 0.0     # Option Buy Price / LTP
    sl_prem: float = 0.0        # Option Stop Loss Premium
    tgt1_prem: float = 0.0      # Checkpoint Premium
    tgt2_prem: float = 0.0      # Ride Target Premium
    exit_prem: float | None = None
    delta: float = 0.65
    ride_target_prem_pts: float = 0.0
    confluence_bonus: int = 0
    last_higher_low: float | None = None
    last_lower_high: float | None = None
    highest_spot: float = 0.0
    lowest_spot: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_option_leg(
    spot: float,
    direction: str,
    risk_pts: float,
    tgt1_price: float,
    tgt2_price: float,
    chain_snapshot: Any = None,
    expiry: str | None = None,
    account_capital: float = 100000.0,
) -> dict[str, Any]:
    """Calculates slightly ITM option strike (delta 0.65-0.75), entry premium, and target premiums.
    
    Rule (Phase 15.1):
    - Strike = delta 0.65-0.75 (slightly ITM).
    - Live delta from chain IV / empirical; replay uses 0.65.
    - Capital gate: premium * 75 <= 70% of account.
    - All premium conversions use actual delta - flat 0.5 banned.
    """
    from engines.delta_execution import select_delta_aware_strike
    strike_res = select_delta_aware_strike(
        spot=spot,
        direction=direction,
        chain_snapshot=chain_snapshot,
        expiry=expiry or "",
    )
    strike = strike_res["strike"]
    opt_type = strike_res["option_type"]
    symbol = strike_res["option_symbol"]
    resolved_expiry = strike_res.get("expiry") or expiry or ""
    entry_prem = strike_res["entry_prem"]

    if chain_snapshot is None:
        delta = 0.65
    else:
        delta = strike_res.get("delta", 0.65)
        if delta < 0.50 or delta > 0.90:
            delta = 0.65

    # Capital Gate: premium * 75 <= 70% of account
    lot_size = getattr(settings, "LOT_SIZE", 75)
    total_cap = getattr(settings, "TOTAL_CAPITAL", account_capital)
    max_capital = 0.70 * total_cap
    capital_req = entry_prem * lot_size
    capital_passed = (capital_req <= max_capital)

    # Actual delta conversions (flat 0.5 banned)
    prem_risk = round(risk_pts * delta, 1)
    sl_prem = max(5.0, round(entry_prem - prem_risk, 1))
    tgt1_gain = round(abs(tgt1_price - spot) * delta, 1)
    tgt2_gain = round(abs(tgt2_price - spot) * delta, 1)
    tgt1_prem = round(entry_prem + tgt1_gain, 1)
    tgt2_prem = round(entry_prem + tgt2_gain, 1)

    return {
        "strike": strike,
        "option_type": opt_type,
        "option_symbol": symbol,
        "expiry": resolved_expiry,
        "entry_prem": entry_prem,
        "sl_prem": sl_prem,
        "tgt1_prem": tgt1_prem,
        "tgt2_prem": tgt2_prem,
        "delta": delta,
        "capital_passed": capital_passed,
        "capital_req": capital_req,
        "max_capital": max_capital,
    }


_DISPATCHED_SNIPER_EVENTS: set[str] = set()


def dispatch_telegram_alert(event_type: str, trade: Any) -> None:
    """Dispatches real-time Telegram alert for Model A sniper trade events."""
    try:
        from alerts.telegram_bot import TelegramSender
        from alerts.messages import format_sniper_entry_alert, format_sniper_tgt1_alert, format_sniper_exit_alert
        sender = TelegramSender()
        if not sender.configured:
            return

        trade_id = getattr(trade, "trade_id", "")
        dedup_key = f"{event_type}:{trade_id}"
        if dedup_key in _DISPATCHED_SNIPER_EVENTS:
            log.info("Telegram alert suppressed (duplicate event %s already sent)", dedup_key)
            return

        # STALE CANDLE GUARD: Never dispatch entry alerts for stale/completed past candles
        # Note: entry_time is candle START time (e.g. 13:55). The candle closes at 14:00 (5m later).
        # Age must be measured from candle close/end time, not from candle start time.
        if event_type == "ENTRY":
            entry_time_str = getattr(trade, "entry_time", "")
            if entry_time_str:
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str)
                    candle_end_dt = entry_dt + timedelta(minutes=5)
                    age_sec = (ist_now() - candle_end_dt).total_seconds()
                    max_age = getattr(settings, "STALE_CANDLE_MAX_AGE_SEC", 360)
                    if age_sec > max_age:
                        log.warning(
                            "Telegram ENTRY alert suppressed (candle %s closed %.0f s ago > %d s)",
                            entry_time_str, age_sec, max_age
                        )
                        return
                except Exception:
                    pass

        # NO SILENT TRADES GUARD: Never announce TGT1, TGT2, or EXIT unless ENTRY was announced to Telegram!
        if event_type in ("TGT1", "TGT2", "EXIT"):
            entry_key = f"ENTRY:{trade_id}"
            if entry_key not in _DISPATCHED_SNIPER_EVENTS:
                log.warning(
                    "Telegram %s alert suppressed for %s (NO_SILENT_TRADES: ENTRY was not announced)",
                    event_type, trade_id
                )
                return

        _DISPATCHED_SNIPER_EVENTS.add(dedup_key)

        expiry = getattr(trade, "expiry", "")
        if event_type == "ENTRY":
            msg = format_sniper_entry_alert(
                trade_id=trade.trade_id,
                direction=trade.direction,
                setup_name=getattr(trade, "setup_name", "Sniper Setup"),
                entry_price=trade.entry_price,
                initial_sl=trade.initial_sl,
                risk_pts=trade.risk_pts,
                tgt1_price=trade.tgt1_price,
                tgt2_price=trade.tgt2_price,
                fib_50=getattr(trade, "fib_50", 0.0),
                fib_618=getattr(trade, "fib_618", 0.0),
                notes=getattr(trade, "notes", ""),
                strike=getattr(trade, "strike", 0),
                option_type=getattr(trade, "option_type", ""),
                option_symbol=getattr(trade, "option_symbol", ""),
                entry_prem=getattr(trade, "entry_prem", 0.0),
                sl_prem=getattr(trade, "sl_prem", 0.0),
                tgt1_prem=getattr(trade, "tgt1_prem", 0.0),
                tgt2_prem=getattr(trade, "tgt2_prem", 0.0),
                expiry=expiry,
                delta=getattr(trade, "delta", 0.65),
                confluence_bonus=bool(getattr(trade, "confluence_bonus", 0)),
                ride_target_prem_pts=getattr(trade, "ride_target_prem_pts", 0.0),
            )
            sender.send(msg)
        elif event_type == "TGT1":
            msg = format_sniper_tgt1_alert(
                trade_id=trade.trade_id,
                direction=trade.direction,
                entry_price=trade.entry_price,
                tgt1_price=trade.tgt1_price,
                tgt2_price=trade.tgt2_price,
                strike=getattr(trade, "strike", 0),
                option_type=getattr(trade, "option_type", ""),
                option_symbol=getattr(trade, "option_symbol", ""),
                entry_prem=getattr(trade, "entry_prem", 0.0),
                tgt1_prem=getattr(trade, "tgt1_prem", 0.0),
                tgt2_prem=getattr(trade, "tgt2_prem", 0.0),
                expiry=expiry,
            )
            sender.send(msg)
        elif event_type == "EXIT":
            exit_prem = getattr(trade, "exit_prem", None)
            if exit_prem is None:
                if trade.exit_reason in ("SL_HIT", "HARD_SL"):
                    exit_prem = getattr(trade, "sl_prem", trade.entry_prem * 0.75)
                elif "TGT2" in (trade.exit_reason or ""):
                    exit_prem = getattr(trade, "tgt2_prem", trade.entry_prem * 1.5)
                elif trade.exit_reason == "BE_SL":
                    exit_prem = getattr(trade, "entry_prem", 0.0)
                else:
                    exit_prem = getattr(trade, "entry_prem", 0.0)

            msg = format_sniper_exit_alert(
                trade_id=trade.trade_id,
                direction=trade.direction,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price or trade.entry_price,
                realized_r=trade.realized_r,
                exit_reason=trade.exit_reason,
                notes=getattr(trade, "notes", ""),
                strike=getattr(trade, "strike", 0),
                option_type=getattr(trade, "option_type", ""),
                option_symbol=getattr(trade, "option_symbol", ""),
                entry_prem=getattr(trade, "entry_prem", 0.0),
                exit_prem=exit_prem,
                expiry=expiry,
            )
            sender.send(msg)
    except Exception as exc:
        log.warning("Telegram sniper alert failed: %s", exc)


class LiquidityTrendlineFibEngine:
    """Master Liquidity & Fibonacci Trendline Continuation Engine (Arm 2).
    
    Identifies intraday counter-moves into the Fibonacci Golden Pocket [0.50, 0.618, 0.705]
    that do NOT cross previous liquidity high/low, targeting previous swing pivots (TGT1)
    and Fibonacci extensions (TGT2).
    """
    def __init__(
        self,
        pdh: float | None = None,
        pdl: float | None = None,
        store=None,
        vah: float | None = None,
        val: float | None = None,
        poc: float | None = None,
        notifier: Any = None,
    ):
        self.pdh = pdh or 0.0
        self.pdl = pdl or 0.0
        self.vah = vah or 0.0
        self.val = val or 0.0
        self.poc = poc or 0.0
        self.store = store
        self.notifier = notifier
        self.chain_snapshot: Any = None
        self.candles: list[Candle] = []
        self.swing_highs: list[tuple[float, int, datetime]] = []
        self.swing_lows: list[tuple[float, int, datetime]] = []
        self.trades: list[LiquidityTrendlineTrade] = []
        self.active_trade: LiquidityTrendlineTrade | None = None
        self.last_traded_pivots: tuple[int, int] | None = None
        self.blocked: bool = False

    def _journal_event(self, event_type: str, trade: LiquidityTrendlineTrade, notes: str | None = None) -> None:
        if not self.store:
            return
        try:
            event_ts = trade.exit_time if (event_type == "EXIT" and trade.exit_time) else trade.entry_time
            self.store.journal_event(
                event_type,
                direction=trade.direction,
                strike=trade.strike,
                spot=trade.exit_price if event_type == "EXIT" else trade.entry_price,
                option_ltp=trade.exit_prem if event_type == "EXIT" else trade.entry_prem,
                trade_id=trade.trade_id,
                result_r=trade.realized_r if event_type == "EXIT" else None,
                exit_reason=trade.exit_reason if event_type == "EXIT" else None,
                family="TL-FIB",
                notes=notes or f"[TL-FIB] {trade.trade_id} {event_type} | {trade.setup_name} | {trade.notes}",
                reasons=trade.to_dict(),
                ts=event_ts,
            )
        except Exception as exc:
            log.warning("Could not journal trendline fib trade event: %s", exc)

    def on_candle(self, c: Candle, day_state: Any = None, chain_snapshot: Any = None) -> list[LiquidityTrendlineTrade]:
        if day_state is not None:
            self.day_state = day_state
        if chain_snapshot is not None:
            self.chain_snapshot = chain_snapshot
        self.candles.append(c)
        c_idx = len(self.candles) - 1

        self._update_trade(c)

        if c_idx >= 2:
            prev2 = self.candles[c_idx - 2]
            prev1 = self.candles[c_idx - 1]
            curr = self.candles[c_idx]

            if prev1.high > prev2.high and prev1.high > curr.high:
                if not self.swing_highs or abs(prev1.high - self.swing_highs[-1][0]) > 2.0:
                    self.swing_highs.append((prev1.high, c_idx - 1, prev1.start))

            if prev1.low < prev2.low and prev1.low < curr.low:
                if not self.swing_lows or abs(prev1.low - self.swing_lows[-1][0]) > 2.0:
                    self.swing_lows.append((prev1.low, c_idx - 1, prev1.start))

        if not getattr(self, "blocked", False) and self.active_trade is None and len(self.swing_highs) >= 1 and len(self.swing_lows) >= 1:
            self._check_setup(c, c_idx)

        return self.trades

    def _update_trade(self, c: Candle) -> None:
        t = self.active_trade
        if not t:
            return

        # 15:10 IST EOD SQUARE-OFF
        if c.start.time() >= dt_time(15, 10):
            self.square_off(exit_price=c.close, exit_time=c.start)
            return

        if t.direction == "SHORT":
            t.lowest_spot = min(getattr(t, "lowest_spot", t.entry_price) or t.entry_price, c.low)
            t.highest_spot = max(getattr(t, "highest_spot", t.entry_price) or t.entry_price, c.high)

            # Intrabar Hard SL Protection (Initial SL is max emergency risk boundary)
            if c.high >= t.initial_sl and t.current_sl >= t.initial_sl:
                t.status = "CLOSED"
                t.exit_price = t.initial_sl
                t.exit_time = c.start.isoformat()
                t.exit_reason = "SL_HIT"
                t.realized_r = -1.00
                t.notes = f"Initial Stop Loss Hit at {t.initial_sl:.1f} (-1.0R)"
                t.exit_prem = t.sl_prem
                self.trades.append(t)
                self.active_trade = None
                self._journal_event("EXIT", t)
                if self.notifier:
                    self.notifier("EXIT", t)
                return

            # Checkpoint only: First Pivot reached (never auto-exit)
            if not t.checkpoint_hit and c.low <= t.tgt1_price:
                t.checkpoint_hit = True
                t.status = "CHECKPOINT_HIT"
                self._journal_event(
                    "UPDATE",
                    t,
                    notes=f"[TL-FIB] {t.trade_id} First Pivot Checkpoint cleared ({t.tgt1_price:.1f}). Structure trailing active (No auto-exit).",
                )
                if self.notifier:
                    self.notifier("TGT1", t)

            # Optional single 30% partial booking ONLY at major wall with profit >= 15 prem pts
            gain_prem = (t.entry_price - c.low) * t.delta
            at_major_wall = (self.val > 0 and c.low <= self.val)
            if not t.wall_booked and gain_prem >= 15.0 and at_major_wall:
                t.wall_booked = True
                t.booked_fraction = 0.30
                t.status = "WALL_PARTIAL"
                t.current_sl = min(t.current_sl, t.entry_price)
                self._journal_event(
                    "UPDATE",
                    t,
                    notes=f"[TL-FIB] {t.trade_id} 30% booked at major wall ({c.low:.1f}). Trailing structure with 70% runner.",
                )

            # Ratchet 5-min Structure Lower High
            # In a downtrend, a valid lower high candle is bearish (close < open) with high < previous LH and low <= lowest_spot
            last_lh = getattr(t, "last_lower_high", None)
            if (last_lh is None or c.high < last_lh) and c.close < c.open and c.low <= t.lowest_spot:
                t.last_lower_high = c.high
                new_trail = round(c.high + 2.0, 1)
                if new_trail < t.current_sl:
                    t.current_sl = new_trail

            # Structure Break Exit: 5-min candle closes above trailed SL
            if c.close > t.current_sl:
                t.status = "CLOSED"
                t.exit_price = c.close
                t.exit_time = c.start.isoformat()
                is_trail = (t.current_sl < t.initial_sl)
                t.exit_reason = "5M_STRUCTURE_TRAIL" if is_trail else "SL_HIT"
                spot_pts = t.entry_price - c.close
                t.exit_prem = max(0.5, round(t.entry_prem + (spot_pts * t.delta), 1))
                if t.wall_booked:
                    t.realized_r = round(0.30 * (15.0 / (t.risk_pts * t.delta)) + 0.70 * (spot_pts / t.risk_pts), 2)
                else:
                    t.realized_r = round(spot_pts / t.risk_pts, 2)
                t.notes = f"5-min close {c.close:.1f} broke structure trail {t.current_sl:.1f}" if is_trail else "Stop Loss Hit (-1.0R)"
                self.trades.append(t)
                self.active_trade = None
                self._journal_event("EXIT", t)
                if self.notifier:
                    self.notifier("EXIT", t)
                return

        else:  # LONG
            t.highest_spot = max(getattr(t, "highest_spot", t.entry_price) or t.entry_price, c.high)
            t.lowest_spot = min(getattr(t, "lowest_spot", t.entry_price) or t.entry_price, c.low)

            # Intrabar Hard SL Protection (Initial SL is max emergency risk boundary)
            if c.low <= t.initial_sl and t.current_sl <= t.initial_sl:
                t.status = "CLOSED"
                t.exit_price = t.initial_sl
                t.exit_time = c.start.isoformat()
                t.exit_reason = "SL_HIT"
                t.realized_r = -1.00
                t.notes = f"Initial Stop Loss Hit at {t.initial_sl:.1f} (-1.0R)"
                t.exit_prem = t.sl_prem
                self.trades.append(t)
                self.active_trade = None
                self._journal_event("EXIT", t)
                if self.notifier:
                    self.notifier("EXIT", t)
                return

            # Checkpoint only: First Pivot reached (never auto-exit)
            if not t.checkpoint_hit and c.high >= t.tgt1_price:
                t.checkpoint_hit = True
                t.status = "CHECKPOINT_HIT"
                self._journal_event(
                    "UPDATE",
                    t,
                    notes=f"[TL-FIB] {t.trade_id} First Pivot Checkpoint cleared ({t.tgt1_price:.1f}). Structure trailing active (No auto-exit).",
                )
                if self.notifier:
                    self.notifier("TGT1", t)

            # Optional single 30% partial booking ONLY at major wall with profit >= 15 prem pts
            gain_prem = (c.high - t.entry_price) * t.delta
            at_major_wall = (self.vah > 0 and c.high >= self.vah)
            if not t.wall_booked and gain_prem >= 15.0 and at_major_wall:
                t.wall_booked = True
                t.booked_fraction = 0.30
                t.status = "WALL_PARTIAL"
                t.current_sl = max(t.current_sl, t.entry_price)
                self._journal_event(
                    "UPDATE",
                    t,
                    notes=f"[TL-FIB] {t.trade_id} 30% booked at major wall ({c.high:.1f}). Trailing structure with 70% runner.",
                )

            # Ratchet 5-min Structure Higher Low
            # In an uptrend, a valid higher low candle is bullish (close > open) with low > previous HL and high >= highest_spot
            last_hl = getattr(t, "last_higher_low", None)
            if (last_hl is None or c.low > last_hl) and c.close > c.open and c.high >= t.highest_spot:
                t.last_higher_low = c.low
                new_trail = round(c.low - 2.0, 1)
                if new_trail > t.current_sl:
                    t.current_sl = new_trail

            # Structure Break Exit: 5-min candle closes below trailed SL
            if c.close < t.current_sl:
                t.status = "CLOSED"
                t.exit_price = c.close
                t.exit_time = c.start.isoformat()
                is_trail = (t.current_sl > t.initial_sl)
                t.exit_reason = "5M_STRUCTURE_TRAIL" if is_trail else "SL_HIT"
                spot_pts = c.close - t.entry_price
                t.exit_prem = max(0.5, round(t.entry_prem + (spot_pts * t.delta), 1))
                if t.wall_booked:
                    t.realized_r = round(0.30 * (15.0 / (t.risk_pts * t.delta)) + 0.70 * (spot_pts / t.risk_pts), 2)
                else:
                    t.realized_r = round(spot_pts / t.risk_pts, 2)
                t.notes = f"5-min close {c.close:.1f} broke structure trail {t.current_sl:.1f}" if is_trail else "Stop Loss Hit (-1.0R)"
                self.trades.append(t)
                self.active_trade = None
                self._journal_event("EXIT", t)
                if self.notifier:
                    self.notifier("EXIT", t)
                return

    def get_intraday_trend(self) -> str:
        """Determines active intraday trend: 'DOWNTREND', 'UPTREND', or 'RANGE'.
        
        Evaluates:
        1. Master DayState authoritative alignment (if provided by live engine).
        2. Swing pivot structure: lower highs & lower lows vs higher highs & higher lows.
        3. Session open price relation.
        """
        if not self.candles:
            return "RANGE"

        # 1. Master DayState authoritative alignment
        if getattr(self, "day_state", None):
            ds_state = getattr(self.day_state, "state", "")
            ds_bias = getattr(self.day_state, "bias", "")
            if ds_state == "TREND-UP" or "CE" in ds_bias:
                return "UPTREND"
            if ds_state == "TREND-DOWN" or "PE" in ds_bias:
                return "DOWNTREND"

        day_open = self.candles[0].open
        curr_c = self.candles[-1]

        if len(self.swing_highs) >= 2 and len(self.swing_lows) >= 2:
            sh_falling = self.swing_highs[-1][0] < self.swing_highs[-2][0]
            sl_falling = self.swing_lows[-1][0] < self.swing_lows[-2][0]
            sh_rising = self.swing_highs[-1][0] > self.swing_highs[-2][0]
            sl_rising = self.swing_lows[-1][0] > self.swing_lows[-2][0]

            if (curr_c.close < day_open or sh_falling) and sl_falling:
                return "DOWNTREND"
            if (curr_c.close > day_open or sh_rising) and sl_rising:
                return "UPTREND"

        if curr_c.close < day_open - 15.0:
            return "DOWNTREND"
        elif curr_c.close > day_open + 15.0:
            return "UPTREND"

        return "RANGE"

    def get_trendline_points(self) -> tuple[tuple[datetime, float] | None, tuple[datetime, float] | None]:
        """Returns (start_point, end_point) for the dominant session trendline."""
        trend = self.get_intraday_trend()
        if trend == "DOWNTREND" and len(self.swing_highs) >= 2:
            sorted_sh = sorted(self.swing_highs, key=lambda x: x[0], reverse=True)
            p1 = sorted_sh[0]
            p2 = self.swing_highs[-1]
            if p1[1] != p2[1]:
                return (p1[2], p1[0]), (p2[2], p2[0])
            elif len(self.swing_highs) >= 2:
                return (self.swing_highs[0][2], self.swing_highs[0][0]), (self.swing_highs[-1][2], self.swing_highs[-1][0])
        elif trend == "UPTREND" and len(self.swing_lows) >= 2:
            sorted_sl = sorted(self.swing_lows, key=lambda x: x[0])
            p1 = sorted_sl[0]
            p2 = self.swing_lows[-1]
            if p1[1] != p2[1]:
                return (p1[2], p1[0]), (p2[2], p2[0])
            elif len(self.swing_lows) >= 2:
                return (self.swing_lows[0][2], self.swing_lows[0][0]), (self.swing_lows[-1][2], self.swing_lows[-1][0])
        return None, None

    def _check_setup(self, c: Candle, c_idx: int) -> None:
        if getattr(self, "blocked", False):
            return

        # TIME GATE: No new entries after 14:00 IST (avoids late-session chop and EOD theta crush)
        if c.start.hour >= 14:
            return

        last_sh = self.swing_highs[-1]
        last_sl = self.swing_lows[-1]

        # ONE TRADE PER SWING IMPULSE: Prevent re-entries on the identical swing pivot leg
        if self.last_traded_pivots == (last_sh[1], last_sl[1]):
            return

        c_range = max(0.1, c.high - c.low)
        trend = self.get_intraday_trend()

        # BEARISH SETUP (Downtrend Retracement into Golden Pocket without crossing prior high)
        if last_sh[1] < last_sl[1] and (c_idx - last_sl[1]) >= 1:
            # STRICT TREND VERIFICATION: In an established uptrend, never short against the trend!
            if trend == "UPTREND":
                return

            sh_px = last_sh[0]
            sl_px = last_sl[0]
            H = sh_px - sl_px

            # IMPULSE PRE-FILTER: H >= 30.0 pts (Phase 15.1 Fix 2)
            if H >= 30.0 and c.high < sh_px:
                fib_50 = sl_px + 0.50 * H
                fib_618 = sl_px + 0.618 * H
                fib_705 = sl_px + 0.705 * H

                tested_fib = (c.high >= fib_50 - 3.0 and c.high <= fib_705 + 5.0)
                upper_wick = c.high - max(c.open, c.close)
                rejected = (c.close < c.open) or (upper_wick >= 0.35 * c_range)

                # FRVP Confluence Detection (VAH or POC alignment)
                has_poc_conf = (self.poc > 0 and abs(fib_618 - self.poc) <= 15.0)
                has_vah_conf = (self.vah > 0 and abs(fib_618 - self.vah) <= 15.0)
                conf_suffix = " (POC Confluence)" if has_poc_conf else (" (VAH Confluence)" if has_vah_conf else "")

                if tested_fib and rejected:
                    entry = c.close
                    # CHASE FILTER: Reject entries that have chased > 20 pts away from Golden Pocket (protects MAE)
                    if abs(entry - fib_618) > 20.0:
                        return
                    sl = min(sh_px + 2.0, entry + 33.0)
                    risk = max(10.0, sl - entry)
                    tgt1 = sl_px  # First pivot checkpoint
                    # Computed ride target: 1.272xH Fib extension
                    fib_ext_ride = sl_px - 0.272 * H
                    tgt2 = min(self.val, fib_ext_ride) if (self.val > 0 and self.val < sl_px) else fib_ext_ride

                    opt_leg = compute_option_leg(
                        spot=entry,
                        direction="SHORT",
                        risk_pts=risk,
                        tgt1_price=tgt1,
                        tgt2_price=tgt2,
                        chain_snapshot=getattr(self, "chain_snapshot", None),
                    )

                    # CAPITAL GATE: premium * 75 <= 70% of account
                    if not opt_leg.get("capital_passed", True):
                        return

                    # SCALP-KILLER GATE: Computed ride target must be >= 20 PREMIUM pts at live delta
                    delta_used = opt_leg.get("delta", 0.65)
                    spot_ride_pts = abs(tgt2 - entry)
                    prem_ride_pts = round(spot_ride_pts * delta_used, 1)
                    if prem_ride_pts < 20.0:
                        return

                    # ZONE CONFLUENCE BONUS: golden pocket coinciding with an OI wall = +10 priority (live only)
                    confluence_bonus = 0
                    if getattr(self, "chain_snapshot", None) and hasattr(self.chain_snapshot, "rows"):
                        for r in self.chain_snapshot.rows:
                            strk = float(getattr(r, "strike", 0))
                            if abs(strk - fib_618) <= 15.0:
                                if float(getattr(r, "ce_oi", 0) or 0) > 100_000:
                                    confluence_bonus = 10
                                    break
                    if confluence_bonus > 0:
                        conf_suffix += " [OI Wall Confluence +10]"

                    self.last_traded_pivots = (last_sh[1], last_sl[1])
                    self.active_trade = LiquidityTrendlineTrade(
                        trade_id=f"TL-FIB-S-{len(self.trades)+1:03d}",
                        direction="SHORT",
                        entry_price=entry,
                        initial_sl=sl,
                        current_sl=sl,
                        risk_pts=risk,
                        tgt1_price=tgt1,
                        tgt2_price=tgt2,
                        entry_time=c.start.isoformat(),
                        fib_50=fib_50,
                        fib_618=fib_618,
                        fib_705=fib_705,
                        setup_name=f"Downtrend Fib Golden Pocket Pullback{conf_suffix}",
                        notes=f"Retracement into Fib Golden Pocket [{fib_50:.1f} - {fib_705:.1f}] below prior high {sh_px:.1f}{conf_suffix}",
                        strike=opt_leg["strike"],
                        option_type=opt_leg["option_type"],
                        option_symbol=opt_leg["option_symbol"],
                        expiry=opt_leg.get("expiry", ""),
                        entry_prem=opt_leg["entry_prem"],
                        sl_prem=opt_leg["sl_prem"],
                        tgt1_prem=opt_leg["tgt1_prem"],
                        tgt2_prem=opt_leg["tgt2_prem"],
                        delta=delta_used,
                        ride_target_prem_pts=prem_ride_pts,
                        confluence_bonus=confluence_bonus,
                        highest_spot=entry,
                        lowest_spot=entry,
                        last_lower_high=entry,
                    )
                    self._journal_event("SIGNAL", self.active_trade)
                    if self.notifier:
                        self.notifier("ENTRY", self.active_trade)
                    return

        # BULLISH SETUP (Uptrend Retracement into Golden Pocket without crossing prior low)
        if last_sl[1] < last_sh[1] and (c_idx - last_sl[1]) >= 1:
            # STRICT TREND VERIFICATION: In an established downtrend, never buy calls against the trend!
            if trend == "DOWNTREND":
                return

            sh_px = last_sh[0]
            sl_px = last_sl[0]
            H = sh_px - sl_px

            # IMPULSE PRE-FILTER: H >= 30.0 pts (Phase 15.1 Fix 2)
            if H >= 30.0 and c.low > sl_px:
                fib_50 = sh_px - 0.50 * H
                fib_618 = sh_px - 0.618 * H
                fib_705 = sh_px - 0.705 * H

                tested_fib = (c.low <= fib_50 + 3.0 and c.low >= fib_705 - 5.0)
                lower_wick = min(c.open, c.close) - c.low
                rejected = (c.close > c.open) or (lower_wick >= 0.35 * c_range)

                # FRVP Confluence Detection (VAL or POC alignment)
                has_poc_conf = (self.poc > 0 and abs(fib_618 - self.poc) <= 15.0)
                has_val_conf = (self.val > 0 and abs(fib_618 - self.val) <= 15.0)
                conf_suffix = " (POC Confluence)" if has_poc_conf else (" (VAL Confluence)" if has_val_conf else "")

                if tested_fib and rejected:
                    entry = c.close
                    # CHASE FILTER: Reject entries that have chased > 20 pts away from Golden Pocket (protects MAE)
                    if abs(entry - fib_618) > 20.0:
                        return
                    sl = max(sl_px - 2.0, entry - 33.0)
                    risk = max(10.0, entry - sl)
                    tgt1 = sh_px  # First pivot checkpoint
                    # Computed ride target: 1.272xH Fib extension
                    fib_ext_ride = sh_px + 0.272 * H
                    tgt2 = max(self.vah, fib_ext_ride) if (self.vah > 0 and self.vah > sh_px) else fib_ext_ride

                    opt_leg = compute_option_leg(
                        spot=entry,
                        direction="LONG",
                        risk_pts=risk,
                        tgt1_price=tgt1,
                        tgt2_price=tgt2,
                        chain_snapshot=getattr(self, "chain_snapshot", None),
                    )

                    # CAPITAL GATE: premium * 75 <= 70% of account
                    if not opt_leg.get("capital_passed", True):
                        return

                    # SCALP-KILLER GATE: Computed ride target must be >= 20 PREMIUM pts at live delta
                    delta_used = opt_leg.get("delta", 0.65)
                    spot_ride_pts = abs(tgt2 - entry)
                    prem_ride_pts = round(spot_ride_pts * delta_used, 1)
                    if prem_ride_pts < 20.0:
                        return

                    # ZONE CONFLUENCE BONUS: golden pocket coinciding with an OI wall = +10 priority (live only)
                    confluence_bonus = 0
                    if getattr(self, "chain_snapshot", None) and hasattr(self.chain_snapshot, "rows"):
                        for r in self.chain_snapshot.rows:
                            strk = float(getattr(r, "strike", 0))
                            if abs(strk - fib_618) <= 15.0:
                                if float(getattr(r, "pe_oi", 0) or 0) > 100_000:
                                    confluence_bonus = 10
                                    break
                    if confluence_bonus > 0:
                        conf_suffix += " [OI Wall Confluence +10]"

                    self.last_traded_pivots = (last_sh[1], last_sl[1])
                    self.active_trade = LiquidityTrendlineTrade(
                        trade_id=f"TL-FIB-L-{len(self.trades)+1:03d}",
                        direction="LONG",
                        entry_price=entry,
                        initial_sl=sl,
                        current_sl=sl,
                        risk_pts=risk,
                        tgt1_price=tgt1,
                        tgt2_price=tgt2,
                        entry_time=c.start.isoformat(),
                        fib_50=fib_50,
                        fib_618=fib_618,
                        fib_705=fib_705,
                        setup_name=f"Uptrend Fib Golden Pocket Pullback{conf_suffix}",
                        notes=f"Retracement into Fib Golden Pocket [{fib_705:.1f} - {fib_50:.1f}] above prior low {sl_px:.1f}{conf_suffix}",
                        strike=opt_leg["strike"],
                        option_type=opt_leg["option_type"],
                        option_symbol=opt_leg["option_symbol"],
                        expiry=opt_leg.get("expiry", ""),
                        entry_prem=opt_leg["entry_prem"],
                        sl_prem=opt_leg["sl_prem"],
                        tgt1_prem=opt_leg["tgt1_prem"],
                        tgt2_prem=opt_leg["tgt2_prem"],
                        delta=delta_used,
                        ride_target_prem_pts=prem_ride_pts,
                        confluence_bonus=confluence_bonus,
                        highest_spot=entry,
                        lowest_spot=entry,
                        last_higher_low=entry,
                    )
                    self._journal_event("SIGNAL", self.active_trade)
                    if self.notifier:
                        self.notifier("ENTRY", self.active_trade)
                    return

    def square_off(self, exit_price: float | None = None, exit_time: datetime | None = None) -> list[LiquidityTrendlineTrade]:
        exit_time = exit_time or ist_now()
        t = self.active_trade
        if not t or t.status == "CLOSED":
            return []
        px = exit_price or t.entry_price
        pts = (px - t.entry_price) if t.direction == "LONG" else (t.entry_price - px)
        runner_r = pts / max(1.0, t.risk_pts)
        delta_val = getattr(t, "delta", 0.65)
        if getattr(t, "wall_booked", False):
            t.realized_r = round(0.30 * (15.0 / (t.risk_pts * delta_val)) + 0.70 * runner_r, 2)
        else:
            t.realized_r = round(runner_r, 2)
        t.status = "CLOSED"
        t.exit_price = px
        t.exit_time = exit_time.isoformat()
        t.exit_reason = "SQUARE_OFF_1510"
        t.exit_prem = max(0.5, round(t.entry_prem + (pts * delta_val), 1))
        self.trades.append(t)
        self.active_trade = None
        self._journal_event("EXIT", t)
        if self.notifier:
            self.notifier("EXIT", t)
        return [t]


# =============================================================================
# ARM 3: LIQUIDITY ZONE REVERSAL ENGINE (2-Candle Rule)
# =============================================================================

@dataclass
class ZoneReversalTrade:
    """Trade record for Arm 3: Liquidity Zone Reversal."""
    trade_id: str
    direction: str              # "LONG" or "SHORT"
    entry_price: float
    initial_sl: float
    current_sl: float
    risk_pts: float
    tgt1_price: float           # Nearest opposing level (checkpoint)
    tgt2_price: float           # Far target (~2x or next zone)
    entry_time: str
    level_name: str             # "PDH", "PDL", "OI_CALL", "OI_PUT", "TRENDLINE_HI", "TRENDLINE_LO"
    level_price: float
    setup_name: str = ""
    notes: str = ""
    status: str = "OPEN"        # "OPEN", "CLOSED"
    exit_price: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    realized_r: float = 0.0
    # Structural trail tracking
    last_structure_point: float = 0.0   # last higher-low (LONG) or lower-high (SHORT)
    # Option fields (mirror Arm 2)
    strike: int = 0
    option_type: str = ""
    option_symbol: str = ""
    expiry: str = ""
    entry_prem: float = 0.0
    sl_prem: float = 0.0
    tgt1_prem: float = 0.0
    tgt2_prem: float = 0.0
    exit_prem: float | None = None
    delta: float = 0.65
    confluence_bonus: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _ZoneLevel:
    """Internal: a marked liquidity level for Arm 3."""
    name: str           # "PDH", "PDL", "OI_CALL", "OI_PUT"
    price: float
    level_type: str     # "RESISTANCE" or "SUPPORT"
    tag: str = "NORMAL" # "STRONG" if OI + structure confluence


@dataclass
class _ZoneState:
    """State machine for a single level's 2-candle confirmation."""
    level: _ZoneLevel
    # After candle-1 closes through the level
    candle1_close: float = 0.0
    candle1_time: str = ""
    candle1_direction: str = ""     # "UP_BREAK" or "DOWN_BREAK"
    waiting_confirm: bool = False   # True after candle-1 fires
    # Number of bars we allow for candle-2 reversal confirmation
    CONFIRM_TIMEOUT: int = 3

    def reset(self) -> None:
        self.candle1_close = 0.0
        self.candle1_time = ""
        self.candle1_direction = ""
        self.waiting_confirm = False


class LiquidityZoneReversalEngine:
    """ARM 3: Clean 2-Candle Liquidity Zone Reversal Engine.

    Core Logic (production-grade, based on user strategy spec):
    ─────────────────────────────────────────────────────────
    LEVELS (set at 09:15 each day):
      • PDH  → Resistance
      • PDL  → Support
      • Top Call-OI strike (>50k OI) → Resistance
      • Top Put-OI strike  (>50k OI) → Support

    SIGNAL SEQUENCE (2-candle rule):
      1. CANDLE-1: 5-min candle closes BEYOND a marked level.
           - Resistance break: close > level  (UP_BREAK)
           - Support   break: close < level   (DOWN_BREAK)
      2. CANDLE-2: Next candle confirms REVERSAL back through the level.
           - After UP_BREAK:   next candle close < level → SHORT signal  (buy PE)
           - After DOWN_BREAK: next candle close > level → LONG  signal  (buy CE)
         • If no reversal within 3 bars → state reset (was a real breakout)

    TRENDLINE REJECTION (secondary, runs in parallel):
      • Swing highs/lows updated every candle (rolling 20-bar pivot)
      • Resistance trendline from last 2 swing highs
      • Support    trendline from last 2 swing lows
      • Price touches trendline AND candle closes back with body >= 50% of range → ENTRY

    EXIT (structural trail — same as Arm 2):
      • For LONG:  SL trails to last 5-min higher-low
      • For SHORT: SL trails to last 5-min lower-high
      • Optional single 30% partial if profit >= 15 prem pts AND far target reached
      • 15:10 square-off
      • Max SL: 33 spot pts
      • Max 3 trades per session

    INSTRUMENT: delta 0.65-0.75 ITM (same as Arm 2 via compute_option_leg)
    """

    MAX_DAILY_TRADES: int = 3
    MAX_SL_PTS: float = 33.0
    MIN_TARGET_PTS: float = 20.0     # minimum ride distance in spot pts
    TRAIL_LOOKBACK: int = 3          # bars to look back for structure point
    SWING_LOOKBACK: int = 20         # bars for trendline pivot detection
    CONFIRM_TIMEOUT: int = 3         # max bars to wait for candle-2 reversal

    def __init__(
        self,
        pdh: float | None = None,
        pdl: float | None = None,
        oi_call_level: float | None = None,
        oi_put_level: float | None = None,
        store: Any = None,
        notifier: Any = None,
    ):
        self.pdh = pdh
        self.pdl = pdl
        self.oi_call_level = oi_call_level
        self.oi_put_level = oi_put_level
        self.store = store
        self.notifier = notifier
        self.chain_snapshot: Any = None
        self.blocked: bool = False

        self.levels: list[_ZoneLevel] = []
        self._zone_states: list[_ZoneState] = []
        self.active_trade: ZoneReversalTrade | None = None
        self.trades: list[ZoneReversalTrade] = []
        self._trade_counter: int = 0
        self._daily_trade_count: int = 0

        # Candle history for trendline & structural trail
        self._candle_history: list[Candle] = []
        self._swing_highs: list[tuple[int, float]] = []   # (index, price)
        self._swing_lows: list[tuple[int, float]] = []    # (index, price)

        # Confirm-timeout counters per zone state
        self._confirm_bars: list[int] = []

        if pdh is not None and pdl is not None:
            self._build_levels()

    # ── Level Construction ────────────────────────────────────────────────────

    def _build_levels(self) -> None:
        """Build the level set from PDH/PDL and optional OI strikes."""
        self.levels = []
        if self.pdh and self.pdh > 0:
            self.levels.append(_ZoneLevel(name="PDH", price=self.pdh, level_type="RESISTANCE", tag="STRONG"))
        if self.pdl and self.pdl > 0:
            self.levels.append(_ZoneLevel(name="PDL", price=self.pdl, level_type="SUPPORT", tag="STRONG"))
        if self.oi_call_level and self.oi_call_level > 0:
            self.levels.append(_ZoneLevel(name="OI_CALL", price=self.oi_call_level, level_type="RESISTANCE", tag="STRONG"))
        if self.oi_put_level and self.oi_put_level > 0:
            self.levels.append(_ZoneLevel(name="OI_PUT", price=self.oi_put_level, level_type="SUPPORT", tag="STRONG"))
        # Initialise zone states
        self._zone_states = [_ZoneState(level=lvl) for lvl in self.levels]
        self._confirm_bars = [0] * len(self.levels)

    def initialize_levels(
        self,
        pdh: float,
        pdl: float,
        oi_call_level: float | None = None,
        oi_put_level: float | None = None,
    ) -> None:
        """Call at 09:15 each session with fresh levels."""
        self.pdh = pdh
        self.pdl = pdl
        self.oi_call_level = oi_call_level
        self.oi_put_level = oi_put_level
        self._daily_trade_count = 0
        self._candle_history = []
        self._swing_highs = []
        self._swing_lows = []
        self._build_levels()

    # ── Swing / Trendline Helpers ────────────────────────────────────────────

    def _update_swings(self) -> None:
        """Update rolling swing highs/lows from recent candle history."""
        n = len(self._candle_history)
        if n < 5:
            return
        lb = min(self.SWING_LOOKBACK, n)
        look = self._candle_history[-lb:]
        half = 2  # pivot bars each side

        # Detect pivot high: high > all 2 bars before & after
        new_highs: list[tuple[int, float]] = []
        new_lows: list[tuple[int, float]] = []
        for i in range(half, len(look) - half):
            h = look[i].high
            l = look[i].low
            if all(look[i-k].high <= h for k in range(1, half+1)) and \
               all(look[i+k].high <= h for k in range(1, half+1)):
                new_highs.append((n - lb + i, h))
            if all(look[i-k].low >= l for k in range(1, half+1)) and \
               all(look[i+k].low >= l for k in range(1, half+1)):
                new_lows.append((n - lb + i, l))

        if new_highs:
            self._swing_highs = new_highs[-3:]
        if new_lows:
            self._swing_lows = new_lows[-3:]

    def _get_trendline_value(self, swing_points: list[tuple[int, float]], current_idx: int) -> float | None:
        """Project a trendline from 2 swing points to the current bar index."""
        if len(swing_points) < 2:
            return None
        (x1, y1), (x2, y2) = swing_points[-2], swing_points[-1]
        if x2 == x1:
            return None
        slope = (y2 - y1) / (x2 - x1)
        return round(y1 + slope * (current_idx - x1), 2)

    # ── Trade Construction ────────────────────────────────────────────────────

    def _build_trade(
        self,
        c: Candle,
        direction: str,
        level_name: str,
        level_price: float,
        setup_name: str,
        notes: str,
        confluence_bonus: int = 0,
    ) -> ZoneReversalTrade | None:
        """Build a ZoneReversalTrade. Returns None if capital/SL gate fails."""
        self._trade_counter += 1
        tag = "L" if direction == "LONG" else "S"
        tid = f"ZR-{tag}-{self._trade_counter:03d}"

        entry = c.close
        # SL: beyond the sweep candle's extreme (+ 3pt buffer), capped at MAX_SL_PTS
        if direction == "LONG":
            raw_sl = c.low - 3.0
            sl = max(entry - self.MAX_SL_PTS, raw_sl)
        else:
            raw_sl = c.high + 3.0
            sl = min(entry + self.MAX_SL_PTS, raw_sl)

        risk_pts = abs(entry - sl)
        if risk_pts < 5.0:
            risk_pts = 10.0
            sl = (entry - 10.0) if direction == "LONG" else (entry + 10.0)

        # Target: next opposing level or 2× risk (min MIN_TARGET_PTS)
        target_pts = max(self.MIN_TARGET_PTS, risk_pts * 2.0)
        if direction == "LONG":
            tgt1 = entry + target_pts * 0.5
            tgt2 = entry + target_pts
        else:
            tgt1 = entry - target_pts * 0.5
            tgt2 = entry - target_pts

        # Option leg
        try:
            opt = compute_option_leg(
                spot=entry,
                direction=direction,
                risk_pts=risk_pts,
                tgt1_price=tgt1,
                tgt2_price=tgt2,
                chain_snapshot=self.chain_snapshot,
            )
        except Exception:
            delta = 0.65
            opt = {
                "strike": int(round(entry / 50) * 50),
                "option_type": "CE" if direction == "LONG" else "PE",
                "option_symbol": f"NIFTY {int(round(entry/50)*50)} {'CE' if direction=='LONG' else 'PE'}",
                "expiry": "",
                "entry_prem": round(max(5.0, risk_pts * delta), 1),
                "sl_prem": round(max(5.0, risk_pts * delta * 0.5), 1),
                "tgt1_prem": round(risk_pts * delta * 1.5, 1),
                "tgt2_prem": round(risk_pts * delta * 2.5, 1),
                "delta": delta,
                "capital_passed": True,
            }

        return ZoneReversalTrade(
            trade_id=tid,
            direction=direction,
            entry_price=entry,
            initial_sl=sl,
            current_sl=sl,
            risk_pts=risk_pts,
            tgt1_price=tgt1,
            tgt2_price=tgt2,
            entry_time=c.start.isoformat(),
            level_name=level_name,
            level_price=level_price,
            setup_name=setup_name,
            notes=notes,
            last_structure_point=c.low if direction == "LONG" else c.high,
            strike=opt["strike"],
            option_type=opt["option_type"],
            option_symbol=opt["option_symbol"],
            expiry=opt.get("expiry", ""),
            entry_prem=opt["entry_prem"],
            sl_prem=opt["sl_prem"],
            tgt1_prem=opt["tgt1_prem"],
            tgt2_prem=opt["tgt2_prem"],
            delta=opt.get("delta", 0.65),
            confluence_bonus=confluence_bonus,
        )

    # ── Active Trade Management ────────────────────────────────────────────────

    def _manage_active(self, c: Candle) -> bool:
        """Returns True if trade was closed on this candle."""
        t = self.active_trade
        if not t:
            return False

        delta = getattr(t, "delta", 0.65)

        if t.direction == "LONG":
            # Update structure: trail SL to last higher-low
            if c.close > t.entry_price:  # only trail in profit
                bars = self._candle_history[-self.TRAIL_LOOKBACK:] if self._candle_history else []
                if bars:
                    new_hl = min(b.low for b in bars)
                    if new_hl > t.current_sl:
                        t.current_sl = round(new_hl - 1.0, 1)
                        t.last_structure_point = new_hl

            if c.low <= t.current_sl:
                exit_pts = t.entry_price - t.current_sl
                t.realized_r = round(-exit_pts / max(1.0, t.risk_pts), 2) if exit_pts > 0 else round((t.entry_price - c.low) / max(1.0, t.risk_pts), 2)
                # Check if SL was moved to breakeven (profit side)
                if t.current_sl >= t.entry_price:
                    t.exit_reason = "BE_SL"
                    pts_gain = t.current_sl - t.entry_price
                    t.realized_r = round(pts_gain / max(1.0, t.risk_pts), 2)
                else:
                    t.exit_reason = "SL_HIT"
                    t.realized_r = round(-exit_pts / max(1.0, t.risk_pts), 2)
                t.exit_price = t.current_sl
                t.exit_prem = round(t.entry_prem - exit_pts * delta, 1)
                t.exit_time = c.start.isoformat()
                t.status = "CLOSED"
                self.trades.append(t)
                self.active_trade = None
                self._daily_trade_count += 1
                if self.notifier:
                    self.notifier("EXIT", t)
                return True

            if c.high >= t.tgt2_price:
                pts = t.tgt2_price - t.entry_price
                t.realized_r = round(pts / max(1.0, t.risk_pts), 2)
                t.exit_reason = "TGT2"
                t.exit_price = t.tgt2_price
                t.exit_prem = round(t.entry_prem + pts * delta, 1)
                t.exit_time = c.start.isoformat()
                t.status = "CLOSED"
                self.trades.append(t)
                self.active_trade = None
                self._daily_trade_count += 1
                if self.notifier:
                    self.notifier("EXIT", t)
                return True

        else:  # SHORT
            # Update structure: trail SL to last lower-high
            if c.close < t.entry_price:  # only trail in profit
                bars = self._candle_history[-self.TRAIL_LOOKBACK:] if self._candle_history else []
                if bars:
                    new_lh = max(b.high for b in bars)
                    if new_lh < t.current_sl:
                        t.current_sl = round(new_lh + 1.0, 1)
                        t.last_structure_point = new_lh

            if c.high >= t.current_sl:
                if t.current_sl <= t.entry_price:
                    t.exit_reason = "BE_SL"
                    pts_gain = t.entry_price - t.current_sl
                    t.realized_r = round(pts_gain / max(1.0, t.risk_pts), 2)
                else:
                    t.exit_reason = "SL_HIT"
                    exit_pts = t.current_sl - t.entry_price
                    t.realized_r = round(-exit_pts / max(1.0, t.risk_pts), 2)
                t.exit_price = t.current_sl
                exit_pts2 = abs(t.entry_price - t.current_sl)
                t.exit_prem = round(t.entry_prem - exit_pts2 * delta, 1)
                t.exit_time = c.start.isoformat()
                t.status = "CLOSED"
                self.trades.append(t)
                self.active_trade = None
                self._daily_trade_count += 1
                if self.notifier:
                    self.notifier("EXIT", t)
                return True

            if c.low <= t.tgt2_price:
                pts = t.entry_price - t.tgt2_price
                t.realized_r = round(pts / max(1.0, t.risk_pts), 2)
                t.exit_reason = "TGT2"
                t.exit_price = t.tgt2_price
                t.exit_prem = round(t.entry_prem + pts * delta, 1)
                t.exit_time = c.start.isoformat()
                t.status = "CLOSED"
                self.trades.append(t)
                self.active_trade = None
                self._daily_trade_count += 1
                if self.notifier:
                    self.notifier("EXIT", t)
                return True

        return False

    # ── Main Candle Handler ────────────────────────────────────────────────────

    def on_candle(self, c: Candle, chain: Any = None, day_state: Any = None) -> None:
        """Process one closed 5-min candle."""
        if chain is not None:
            self.chain_snapshot = chain

        self._candle_history.append(c)
        self._update_swings()
        current_idx = len(self._candle_history) - 1

        # 1. Manage open position
        if self.active_trade:
            closed = self._manage_active(c)
            if not closed:
                return  # still in trade, don't look for new signals
            # trade just closed — fall through to look for next signal

        # 2. Check daily trade limit
        if self._daily_trade_count >= self.MAX_DAILY_TRADES:
            return

        if self.blocked:
            return

        # Time gate: no new entries after 14:00 IST
        try:
            ct = c.start.time()
            if ct >= dt_time(14, 0):
                return
        except Exception:
            pass

        # 3. Check trendline rejection (primary secondary signal)
        tr = self._check_trendline_rejection(c, current_idx)
        if tr is not None:
            trade = self._build_trade(
                c=c,
                direction=tr["direction"],
                level_name=tr["level_name"],
                level_price=tr["level_price"],
                setup_name=tr["setup_name"],
                notes=tr["notes"],
                confluence_bonus=tr.get("confluence_bonus", 0),
            )
            if trade:
                self.active_trade = trade
                if self.notifier:
                    self.notifier("ENTRY", trade)
            return

        # 4. Process zone states (2-candle rule)
        for i, zs in enumerate(self._zone_states):
            lvl = zs.level
            ZONE_TOL = 2.0  # point tolerance for "at level"

            if zs.waiting_confirm:
                # Candle-2 check: did price reverse back through the level?
                self._confirm_bars[i] += 1
                confirmed = False
                direction = ""
                if zs.candle1_direction == "UP_BREAK":
                    # We broke up → expecting reversal DOWN through level (SHORT)
                    if c.close < lvl.price - ZONE_TOL:
                        confirmed = True
                        direction = "SHORT"
                elif zs.candle1_direction == "DOWN_BREAK":
                    # We broke down → expecting reversal UP through level (LONG)
                    if c.close > lvl.price + ZONE_TOL:
                        confirmed = True
                        direction = "LONG"

                if confirmed:
                    # Candle-2 body check: body >= 40% of candle range
                    body = abs(c.close - c.open)
                    rng = max(0.1, c.high - c.low)
                    if body / rng >= 0.40:
                        bonus = 10 if lvl.tag == "STRONG" else 0
                        trade = self._build_trade(
                            c=c,
                            direction=direction,
                            level_name=lvl.name,
                            level_price=lvl.price,
                            setup_name=f"Zone Reversal ({lvl.name})",
                            notes=f"2-Candle reversal at {lvl.name} ({lvl.price:.1f}). "
                                  f"Candle-1 {zs.candle1_direction} → Candle-2 confirms {direction}.",
                            confluence_bonus=bonus,
                        )
                        if trade:
                            self.active_trade = trade
                            zs.reset()
                            self._confirm_bars[i] = 0
                            if self.notifier:
                                self.notifier("ENTRY", trade)
                            return  # take first valid signal only
                    else:
                        # Weak body — skip this confirm, keep waiting if not timed out
                        pass

                # Timeout: if too many bars pass without confirm, reset
                if self._confirm_bars[i] >= self.CONFIRM_TIMEOUT:
                    zs.reset()
                    self._confirm_bars[i] = 0
                continue  # don't re-arm this zone while waiting

            # Fresh candle-1 detection
            # UP_BREAK: resistance level — candle closes above
            if lvl.level_type == "RESISTANCE" and c.close > lvl.price + ZONE_TOL:
                zs.candle1_close = c.close
                zs.candle1_time = c.start.isoformat()
                zs.candle1_direction = "UP_BREAK"
                zs.waiting_confirm = True
                self._confirm_bars[i] = 0

            # DOWN_BREAK: support level — candle closes below
            elif lvl.level_type == "SUPPORT" and c.close < lvl.price - ZONE_TOL:
                zs.candle1_close = c.close
                zs.candle1_time = c.start.isoformat()
                zs.candle1_direction = "DOWN_BREAK"
                zs.waiting_confirm = True
                self._confirm_bars[i] = 0

    # ── Trendline Rejection Check ─────────────────────────────────────────────

    def _check_trendline_rejection(self, c: Candle, current_idx: int) -> dict | None:
        """Check for trendline touch + rejection candle.

        Returns signal dict or None.
        Resistance TL (from swing highs): price touches from below → close back down (SHORT).
        Support    TL (from swing lows):  price touches from above → close back up  (LONG).
        """
        TOL = 5.0       # points: "touching" the trendline
        BODY_RATIO = 0.50

        body = abs(c.close - c.open)
        rng = max(0.1, c.high - c.low)

        # Resistance trendline (from swing highs) → SHORT signal
        r_tl = self._get_trendline_value(self._swing_highs, current_idx)
        if r_tl is not None and r_tl > 0:
            if c.high >= r_tl - TOL and c.close < r_tl and c.close < c.open and (body / rng) >= BODY_RATIO:
                return {
                    "direction": "SHORT",
                    "level_name": "TRENDLINE_HI",
                    "level_price": round(r_tl, 1),
                    "setup_name": "Trendline Resistance Rejection",
                    "notes": f"Price touched resistance TL ({r_tl:.1f}) and closed bearish with {body/rng*100:.0f}% body.",
                    "confluence_bonus": 5,
                }

        # Support trendline (from swing lows) → LONG signal
        s_tl = self._get_trendline_value(self._swing_lows, current_idx)
        if s_tl is not None and s_tl > 0:
            if c.low <= s_tl + TOL and c.close > s_tl and c.close > c.open and (body / rng) >= BODY_RATIO:
                return {
                    "direction": "LONG",
                    "level_name": "TRENDLINE_LO",
                    "level_price": round(s_tl, 1),
                    "setup_name": "Trendline Support Bounce",
                    "notes": f"Price touched support TL ({s_tl:.1f}) and closed bullish with {body/rng*100:.0f}% body.",
                    "confluence_bonus": 5,
                }

        return None

    # ── Square Off ────────────────────────────────────────────────────────────

    def square_off(self, exit_price: float, exit_time: datetime) -> None:
        t = self.active_trade
        if not t or t.status == "CLOSED":
            return
        px = exit_price or t.entry_price
        delta = getattr(t, "delta", 0.65)
        pts = (px - t.entry_price) if t.direction == "LONG" else (t.entry_price - px)
        t.realized_r = round(pts / max(1.0, t.risk_pts), 2)
        t.exit_price = px
        t.exit_time = exit_time.isoformat()
        t.exit_reason = "SQUARE_OFF_1510"
        t.exit_prem = round(t.entry_prem + abs(pts) * delta * (1 if pts >= 0 else -1), 1)
        t.status = "CLOSED"
        self.trades.append(t)
        self._daily_trade_count += 1
        self.active_trade = None
        if self.notifier:
            self.notifier("EXIT", t)

    def get_all_trades(self) -> list[ZoneReversalTrade]:
        result = list(self.trades)
        if self.active_trade and self.active_trade not in result:
            result.append(self.active_trade)
        return result


class UnifiedLiquiditySniper:
    """Unified Institutional Liquidity & Fibonacci Sniper System.
    
    Combines:
    1. Arm 1: Phase 14 M/W Sweeps at Extremes (PDH / PDL / OI Walls with MSS & Fib Extensions).
    2. Arm 2: Trendline & Golden Pocket Retracements (Intraday Continuations without breaking previous liquidity).
    3. Arm 3: Clean 2-Candle Liquidity Zone Reversal (PDH/PDL/OI + Trendline rejection) — Production Engine.
    """
    def __init__(
        self,
        store=None,
        today_iso: str | None = None,
        pdh: float | None = None,
        pdl: float | None = None,
        isolated: bool = False,
        vah: float | None = None,
        val: float | None = None,
        poc: float | None = None,
        notifier: Any = None,
    ):
        self.today_iso = today_iso or ist_now().date().isoformat()
        self.store = store
        self.isolated = isolated
        self.arm1 = ShadowMWEngine(store=store, today_iso=self.today_iso, isolated=isolated, notifier=notifier)
        if pdh is not None and pdl is not None and pdh > 0 and pdl > 0:
            fake_market = type("M", (), {"pdh": pdh, "pdl": pdl, "chain": None})()
            self.arm1.initialize_levels(fake_market, today_iso=self.today_iso)
        self.arm2 = LiquidityTrendlineFibEngine(pdh=pdh, pdl=pdl, store=store, vah=vah, val=val, poc=poc, notifier=notifier)
        # Arm 3: Production 2-Candle Liquidity Zone Reversal Engine
        self.arm3 = LiquidityZoneReversalEngine(pdh=pdh, pdl=pdl, notifier=notifier)

    def initialize_levels(self, pdh: float, pdl: float, oi_call_level: float | None = None, oi_put_level: float | None = None) -> None:
        """Dynamically update/initialize key levels across all arms."""
        if pdh and pdl and pdh > 0 and pdl > 0:
            fake_market = type("M", (), {"pdh": pdh, "pdl": pdl, "chain": None})()
            self.arm1.initialize_levels(fake_market, today_iso=self.today_iso)
            self.arm2.pdh = pdh
            self.arm2.pdl = pdl
            self.arm3.initialize_levels(pdh=pdh, pdl=pdl, oi_call_level=oi_call_level, oi_put_level=oi_put_level)

    def on_candle(self, c: Candle, day_state: Any = None, chain: Any = None, allow_alerts: bool = True) -> None:
        if chain is not None:
            self.arm1.chain_snapshot = chain
            self.arm2.chain_snapshot = chain
            self.arm3.chain_snapshot = chain

        orig_notifier1 = self.arm1.notifier
        orig_notifier2 = self.arm2.notifier
        orig_notifier3 = self.arm3.notifier
        if not allow_alerts:
            self.arm1.notifier = None
            self.arm2.notifier = None
            self.arm3.notifier = None

        try:
            # Enforce single active trade across ALL three arms
            arm1_has_open = any(t.status in ("OPEN", "PARTIAL_TGT1") for t in self.arm1.trades)
            arm2_has_open = (self.arm2.active_trade is not None)
            arm3_has_open = (self.arm3.active_trade is not None)

            any_open = arm1_has_open or arm2_has_open or arm3_has_open

            self.arm1.blocked = arm2_has_open or arm3_has_open
            self.arm2.blocked = arm1_has_open or arm3_has_open
            self.arm3.blocked = arm1_has_open or arm2_has_open

            # Process all three arms
            self.arm1.on_candle(c, spot=c.close, chain=chain, day_state=day_state)

            # Re-check after arm1
            arm1_has_open_now = any(t.status in ("OPEN", "PARTIAL_TGT1") for t in self.arm1.trades)
            self.arm2.blocked = arm1_has_open_now or arm3_has_open
            self.arm3.blocked = arm1_has_open_now or arm2_has_open

            self.arm2.on_candle(c, day_state=day_state, chain_snapshot=chain)

            # Re-check after arm2
            arm2_has_open_now = (self.arm2.active_trade is not None)
            self.arm3.blocked = arm1_has_open_now or arm2_has_open_now

            self.arm3.on_candle(c, chain=chain, day_state=day_state)

            # Persist final blocked states so callers can inspect them
            arm3_has_open_now = (self.arm3.active_trade is not None)
            self.arm1.blocked = arm2_has_open_now or arm3_has_open_now
            self.arm2.blocked = arm1_has_open_now or arm3_has_open_now
        finally:
            if not allow_alerts:
                self.arm1.notifier = orig_notifier1
                self.arm2.notifier = orig_notifier2
                self.arm3.notifier = orig_notifier3

    def square_off(self, exit_price: float, exit_time: datetime) -> None:
        self.arm1.square_off(exit_price=exit_price, exit_time=exit_time)
        self.arm2.square_off(exit_price=exit_price, exit_time=exit_time)
        self.arm3.square_off(exit_price=exit_price, exit_time=exit_time)

    def get_summary(self) -> dict[str, Any]:
        s1 = self.arm1.get_summary(self.today_iso)
        t1 = [t for t in self.arm1.trades if t.status == "CLOSED"]
        t2 = [t for t in self.arm2.trades if t.status == "CLOSED"]
        t3 = [t for t in self.arm3.trades if t.status == "CLOSED"]
        all_closed = t1 + t2 + t3
        wins = [t for t in all_closed if t.realized_r > 0]
        losses = [t for t in all_closed if t.realized_r < 0]
        tot_r = sum(t.realized_r for t in all_closed)
        wr = (len(wins) / len(all_closed) * 100.0) if all_closed else 0.0
        exp = (tot_r / len(all_closed)) if all_closed else 0.0

        return {
            "combined_trades": len(all_closed),
            "combined_wins": len(wins),
            "combined_losses": len(losses),
            "combined_win_rate": round(wr, 1),
            "combined_total_r": round(tot_r, 2),
            "combined_expectancy": round(exp, 2),
            "arm1_summary": s1,
            "arm2_trades_count": len(t2),
            "arm2_return_r": round(sum(t.realized_r for t in t2), 2),
            "arm3_trades_count": len(t3),
            "arm3_return_r": round(sum(t.realized_r for t in t3), 2),
        }

    def get_all_trades(self) -> list[Any]:
        """Returns all trades (both closed and currently active) across all three arms."""
        all_tr = list(self.arm1.trades) + list(self.arm2.trades) + list(self.arm3.trades)
        if getattr(self.arm2, "active_trade", None) and self.arm2.active_trade not in all_tr:
            all_tr.append(self.arm2.active_trade)
        if getattr(self.arm3, "active_trade", None) and self.arm3.active_trade not in all_tr:
            all_tr.append(self.arm3.active_trade)
        return all_tr

    def get_all_trades_dict(self) -> list[dict[str, Any]]:
        """Returns JSON-serializable dictionaries of all trades."""
        res = []
        for t in self.get_all_trades():
            if hasattr(t, "to_dict"):
                res.append(t.to_dict())
            else:
                try:
                    from dataclasses import asdict
                    res.append(asdict(t))
                except Exception:
                    res.append(dict(t.__dict__))
        return res


def compute_fixed_range_volume_profile(
    candles: list[Candle],
    value_area_pct: float = 0.70,
    bin_size: float = 2.0,
) -> dict[str, float]:
    """Compute Fixed Range Volume Profile (FRVP) across session candles (09:15 to 15:30).
    
    Identifies:
    - VAH (Value Area High): Price boundary encompassing 70% of total volume above POC
    - VAL (Value Area Low): Price boundary encompassing 70% of total volume below POC
    - POC (Point of Control): Price bin where the highest trading volume took place
    """
    if not candles:
        return {"vah": 0.0, "val": 0.0, "poc": 0.0, "total_volume": 0.0, "spread": 0.0}

    min_p = min(c.low for c in candles)
    max_p = max(c.high for c in candles)
    if min_p >= max_p:
        return {"vah": min_p, "val": min_p, "poc": min_p, "total_volume": 0.0, "spread": 0.0}

    import numpy as np

    bins = np.arange(np.floor(min_p), np.ceil(max_p) + bin_size, bin_size)
    if len(bins) < 2:
        return {"vah": max_p, "val": min_p, "poc": (max_p + min_p) / 2.0, "total_volume": 0.0, "spread": max_p - min_p}

    vol_profile = np.zeros(len(bins) - 1)
    for c in candles:
        c_vol = float(c.volume) if (c.volume is not None and c.volume > 0) else 100.0
        i_s = max(0, int((c.low - bins[0]) // bin_size))
        i_e = min(len(vol_profile) - 1, int((c.high - bins[0]) // bin_size))
        n_bins = max(1, i_e - i_s + 1)
        vol_profile[i_s : i_e + 1] += c_vol / n_bins

    poc_idx = int(np.argmax(vol_profile))
    total_vol = float(np.sum(vol_profile))
    target_vol = value_area_pct * total_vol
    cur_vol = float(vol_profile[poc_idx])

    included = {poc_idx}
    up_idx = poc_idx + 1
    down_idx = poc_idx - 1

    while cur_vol < target_vol and (up_idx < len(vol_profile) or down_idx >= 0):
        vol_up = vol_profile[up_idx] if up_idx < len(vol_profile) else -1.0
        vol_down = vol_profile[down_idx] if down_idx >= 0 else -1.0
        if vol_up >= vol_down:
            cur_vol += max(0.0, vol_up)
            included.add(up_idx)
            up_idx += 1
        else:
            cur_vol += max(0.0, vol_down)
            included.add(down_idx)
            down_idx -= 1

    val_level = float(bins[min(included)])
    vah_level = float(bins[max(included)] + bin_size)
    poc_level = float(bins[poc_idx] + bin_size / 2.0)

    return {
        "vah": round(vah_level, 1),
        "val": round(val_level, 1),
        "poc": round(poc_level, 1),
        "total_volume": round(total_vol, 0),
        "spread": round(vah_level - val_level, 1),
    }


class FRVPSweepEngine:
    """Fixed Range Volume Profile (FRVP) Liquidity Sweep Engine.
    
    1. Projects yesterday's VAH, VAL, and POC across today's session.
    2. VAL Liquidity Sweep: Price sweeps below VAL, closes back inside -> Target VAH.
    3. VAH Liquidity Sweep: Price sweeps above VAH, closes back inside -> Target VAL.
    4. 2-Stage Management: At POC (halfway), locks 50% profit and moves SL to Entry (BE_SL).
    """
    def __init__(self, vah: float, val: float, poc: float, use_trend_filter: bool = True):
        self.vah = vah
        self.val = val
        self.poc = poc
        self.use_trend_filter = use_trend_filter
        self.trades: list[LiquidityTrendlineTrade] = []
        self.active_trade: LiquidityTrendlineTrade | None = None

    def on_candle(self, c: Candle, trend: str = "RANGE") -> None:
        if self.vah <= self.val:
            return

        # 1. Manage active position
        if self.active_trade and self.active_trade.status in ("OPEN", "PARTIAL_TGT1"):
            t = self.active_trade
            if t.direction == "LONG":
                # Check Stop Loss
                if c.low <= t.current_sl:
                    loss_pts = t.entry_price - t.current_sl
                    loss_r = -(loss_pts / t.risk_pts)
                    if t.half_booked:
                        t.realized_r = round(0.50 * t.tgt1_r + 0.50 * loss_r, 2)
                        t.exit_reason = "BE_SL"
                    else:
                        t.realized_r = round(loss_r, 2)
                        t.exit_reason = "SL_HIT"
                    t.status = "CLOSED"
                    t.exit_price = t.current_sl
                    t.exit_time = c.start.isoformat()
                    self.trades.append(t)
                    self.active_trade = None
                    return

                # Target 1: POC (halfway)
                if not t.half_booked and c.high >= t.tgt1_price:
                    t.half_booked = True
                    t.status = "PARTIAL_TGT1"
                    t.tgt1_r = max(1.0, (t.tgt1_price - t.entry_price) / t.risk_pts)
                    t.current_sl = t.entry_price  # Move SL to Breakeven BE_SL

                # Target 2: VAH (opposite Value Area boundary)
                if c.high >= t.tgt2_price:
                    runner_r = (t.tgt2_price - t.entry_price) / t.risk_pts
                    if t.half_booked:
                        t.realized_r = round(0.50 * t.tgt1_r + 0.50 * runner_r, 2)
                    else:
                        t.realized_r = round(runner_r, 2)
                    t.status = "CLOSED"
                    t.exit_price = t.tgt2_price
                    t.exit_time = c.start.isoformat()
                    t.exit_reason = "TGT_VAH"
                    self.trades.append(t)
                    self.active_trade = None
                    return
            else:  # SHORT
                # Check Stop Loss
                if c.high >= t.current_sl:
                    loss_pts = t.current_sl - t.entry_price
                    loss_r = -(loss_pts / t.risk_pts)
                    if t.half_booked:
                        t.realized_r = round(0.50 * t.tgt1_r + 0.50 * loss_r, 2)
                        t.exit_reason = "BE_SL"
                    else:
                        t.realized_r = round(loss_r, 2)
                        t.exit_reason = "SL_HIT"
                    t.status = "CLOSED"
                    t.exit_price = t.current_sl
                    t.exit_time = c.start.isoformat()
                    self.trades.append(t)
                    self.active_trade = None
                    return

                # Target 1: POC (halfway)
                if not t.half_booked and c.low <= t.tgt1_price:
                    t.half_booked = True
                    t.status = "PARTIAL_TGT1"
                    t.tgt1_r = max(1.0, (t.entry_price - t.tgt1_price) / t.risk_pts)
                    t.current_sl = t.entry_price  # Move SL to Breakeven BE_SL

                # Target 2: VAL (opposite Value Area boundary)
                if c.low <= t.tgt2_price:
                    runner_r = (t.entry_price - t.tgt2_price) / t.risk_pts
                    if t.half_booked:
                        t.realized_r = round(0.50 * t.tgt1_r + 0.50 * runner_r, 2)
                    else:
                        t.realized_r = round(runner_r, 2)
                    t.status = "CLOSED"
                    t.exit_price = t.tgt2_price
                    t.exit_time = c.start.isoformat()
                    t.exit_reason = "TGT_VAL"
                    self.trades.append(t)
                    self.active_trade = None
                    return

        # 2. Check for new sweep triggers (if not currently in a trade)
        if not self.active_trade:
            # Long trigger: swept VAL (c.low < val) and closed back inside (c.close > val)
            if c.low < self.val and c.close > self.val and c.close > c.open:
                if self.use_trend_filter and trend == "DOWNTREND":
                    return  # Filter out counter-trend Longs during strong downtrend
                sl_px = max(c.low - 3.0, c.close - 20.0)
                risk_pts = max(6.0, c.close - sl_px)
                self.active_trade = LiquidityTrendlineTrade(
                    trade_id=f"FRVP-L-{len(self.trades)+1:03d}",
                    direction="LONG",
                    entry_price=c.close,
                    initial_sl=sl_px,
                    current_sl=sl_px,
                    risk_pts=risk_pts,
                    tgt1_price=self.poc,
                    tgt2_price=self.vah,
                    entry_time=c.start.isoformat(),
                    setup_name="VAL Liquidity Sweep",
                    notes=f"Swept VAL ({self.val:.1f}) -> Target POC ({self.poc:.1f}) & VAH ({self.vah:.1f})",
                )
                return

            # Short trigger: swept VAH (c.high > vah) and closed back inside (c.close < vah)
            if c.high > self.vah and c.close < self.vah and c.close < c.open:
                if self.use_trend_filter and trend == "UPTREND":
                    return  # Filter out counter-trend Shorts during strong uptrend
                sl_px = min(c.high + 3.0, c.close + 20.0)
                risk_pts = max(6.0, sl_px - c.close)
                self.active_trade = LiquidityTrendlineTrade(
                    trade_id=f"FRVP-S-{len(self.trades)+1:03d}",
                    direction="SHORT",
                    entry_price=c.close,
                    initial_sl=sl_px,
                    current_sl=sl_px,
                    risk_pts=risk_pts,
                    tgt1_price=self.poc,
                    tgt2_price=self.val,
                    entry_time=c.start.isoformat(),
                    setup_name="VAH Liquidity Sweep",
                    notes=f"Swept VAH ({self.vah:.1f}) -> Target POC ({self.poc:.1f}) & VAL ({self.val:.1f})",
                )
                return

    def square_off(self, exit_price: float, exit_time: datetime) -> None:
        t = self.active_trade
        if not t or t.status == "CLOSED":
            return
        px = exit_price or t.entry_price
        pts = (px - t.entry_price) if t.direction == "LONG" else (t.entry_price - px)
        runner_r = pts / t.risk_pts
        if t.half_booked:
            t.realized_r = round(0.50 * t.tgt1_r + 0.50 * runner_r, 2)
        else:
            t.realized_r = round(runner_r, 2)
        t.status = "CLOSED"
        t.exit_price = px
        t.exit_time = exit_time.isoformat()
        t.exit_reason = "SQUARE_OFF_1510"
        self.trades.append(t)
        self.active_trade = None


