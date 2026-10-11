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
    entry_prem: float = 0.0     # Option Buy Price / LTP
    sl_prem: float = 0.0        # Option Stop Loss Premium
    tgt1_prem: float = 0.0      # Option Target 1 Premium
    tgt2_prem: float = 0.0      # Option Target 2 Premium
    exit_prem: float | None = None

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
            self.trades = [ShadowMWTrade(**t) for t in data.get("trades", [])]
            self.candidates = [MWPatternCandidate(**c) for c in data.get("candidates", [])]
            self.levels = [MWLevel(**lvl) for lvl in data.get("levels", [])]
            if self.levels:
                self.levels_initialized = True
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
            entry_prem=opt_leg["entry_prem"],
            sl_prem=opt_leg["sl_prem"],
            tgt1_prem=opt_leg["tgt1_prem"],
            tgt2_prem=opt_leg["tgt2_prem"],
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
    tgt1_price: float           # Previous High / Low
    tgt2_price: float           # -0.618 Fib Extension
    entry_time: str
    fib_50: float = 0.0
    fib_618: float = 0.0
    fib_705: float = 0.0
    status: str = "OPEN"        # "OPEN", "PARTIAL_TGT1", "CLOSED"
    exit_price: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    realized_r: float = 0.0
    half_booked: bool = False
    setup_name: str = ""
    notes: str = ""
    # Option Chain Execution Fields
    strike: int = 0
    option_type: str = ""       # "CE" or "PE"
    option_symbol: str = ""     # e.g. "NIFTY 22550 CE"
    entry_prem: float = 0.0     # Option Buy Price / LTP
    sl_prem: float = 0.0        # Option Stop Loss Premium
    tgt1_prem: float = 0.0      # Option Target 1 Premium
    tgt2_prem: float = 0.0      # Option Target 2 Premium
    exit_prem: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_option_leg(
    spot: float,
    direction: str,
    risk_pts: float,
    tgt1_price: float,
    tgt2_price: float,
    chain_snapshot: Any = None,
) -> dict[str, Any]:
    """Calculates ATM option strike, option type, buying premium, and target premiums."""
    step = getattr(settings, "STRIKE_ROUND_TO", 50)
    strike = int(round(spot / step) * step)
    opt_type = "CE" if direction == "LONG" else "PE"
    symbol = f"NIFTY {strike} {opt_type}"

    ltp = None
    if chain_snapshot is not None:
        try:
            from main import option_ltp_for
            ltp = option_ltp_for(chain_snapshot, strike, direction)
        except Exception:
            pass

    beta = getattr(settings, "PREMIUM_BETA", 0.50)
    if ltp is None or ltp <= 0:
        # Standard realistic weekly ATM premium baseline (~0.65% of spot)
        base_atm = max(80.0, round((spot * 0.0065) / 5.0) * 5.0)
        ltp = base_atm

    prem_risk = round(risk_pts * beta, 1)
    sl_prem = max(5.0, round(ltp - prem_risk, 1))
    tgt1_gain = round(abs(tgt1_price - spot) * beta, 1)
    tgt2_gain = round(abs(tgt2_price - spot) * beta, 1)
    tgt1_prem = round(ltp + tgt1_gain, 1)
    tgt2_prem = round(ltp + tgt2_gain, 1)

    return {
        "strike": strike,
        "option_type": opt_type,
        "option_symbol": symbol,
        "entry_prem": ltp,
        "sl_prem": sl_prem,
        "tgt1_prem": tgt1_prem,
        "tgt2_prem": tgt2_prem,
    }


def dispatch_telegram_alert(event_type: str, trade: Any) -> None:
    """Dispatches real-time Telegram alert for Model A sniper trade events."""
    try:
        from alerts.telegram_bot import TelegramSender
        from alerts.messages import format_sniper_entry_alert, format_sniper_tgt1_alert, format_sniper_exit_alert
        sender = TelegramSender()
        if not sender.configured:
            return
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

    def on_candle(self, c: Candle, chain_snapshot: Any = None) -> list[LiquidityTrendlineTrade]:
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

        if self.active_trade is None and len(self.swing_highs) >= 1 and len(self.swing_lows) >= 1:
            self._check_setup(c, c_idx)

        return self.trades

    def _update_trade(self, c: Candle) -> None:
        t = self.active_trade
        if not t:
            return

        if t.direction == "SHORT":
            if c.high >= t.current_sl:
                t.status = "CLOSED"
                t.exit_price = t.current_sl
                t.exit_time = c.start.isoformat()
                t.exit_reason = "BE_SL" if t.half_booked else "SL_HIT"
                t.realized_r = 0.50 if t.half_booked else -1.00
                t.notes = "Partial TGT1 (+1.0R) + Breakeven Stop Locked (+0.50R)" if t.half_booked else "Stop Loss Hit (-1.0R)"
                t.exit_prem = t.entry_prem if t.half_booked else t.sl_prem
                self.trades.append(t)
                self.active_trade = None
                if self.notifier:
                    self.notifier("EXIT", t)
                return

            if not t.half_booked and c.low <= t.tgt1_price:
                t.half_booked = True
                t.current_sl = t.entry_price  # Breakeven stop
                t.status = "PARTIAL_TGT1"
                if self.notifier:
                    self.notifier("TGT1", t)

            if t.half_booked and c.low <= t.tgt2_price:
                t1_r = max(1.0, (t.entry_price - t.tgt1_price) / t.risk_pts)
                t2_r = max(1.5, (t.entry_price - t.tgt2_price) / t.risk_pts)
                t.realized_r = round(0.50 * t1_r + 0.50 * t2_r, 2)
                t.status = "CLOSED"
                t.exit_price = t.tgt2_price
                t.exit_time = c.start.isoformat()
                t.exit_reason = "FIB_EXT_TGT2_HIT"
                t.exit_prem = t.tgt2_prem
                self.trades.append(t)
                self.active_trade = None
                if self.notifier:
                    self.notifier("EXIT", t)
                return

            if t.half_booked and c.high < t.current_sl:
                t.current_sl = min(t.current_sl, c.high + 2.0)

        else:  # LONG
            if c.low <= t.current_sl:
                t.status = "CLOSED"
                t.exit_price = t.current_sl
                t.exit_time = c.start.isoformat()
                t.exit_reason = "BE_SL" if t.half_booked else "SL_HIT"
                t.realized_r = 0.50 if t.half_booked else -1.00
                t.notes = "Partial TGT1 (+1.0R) + Breakeven Stop Locked (+0.50R)" if t.half_booked else "Stop Loss Hit (-1.0R)"
                t.exit_prem = t.entry_prem if t.half_booked else t.sl_prem
                self.trades.append(t)
                self.active_trade = None
                if self.notifier:
                    self.notifier("EXIT", t)
                return

            if not t.half_booked and c.high >= t.tgt1_price:
                t.half_booked = True
                t.current_sl = t.entry_price  # Breakeven stop
                t.status = "PARTIAL_TGT1"
                if self.notifier:
                    self.notifier("TGT1", t)

            if t.half_booked and c.high >= t.tgt2_price:
                t1_r = max(1.0, (t.tgt1_price - t.entry_price) / t.risk_pts)
                t2_r = max(1.5, (t.tgt2_price - t.entry_price) / t.risk_pts)
                t.realized_r = round(0.50 * t1_r + 0.50 * t2_r, 2)
                t.status = "CLOSED"
                t.exit_price = t.tgt2_price
                t.exit_time = c.start.isoformat()
                t.exit_reason = "FIB_EXT_TGT2_HIT"
                t.exit_prem = t.tgt2_prem
                self.trades.append(t)
                self.active_trade = None
                if self.notifier:
                    self.notifier("EXIT", t)
                return

            if t.half_booked and c.low > t.current_sl:
                t.current_sl = max(t.current_sl, c.low - 2.0)

    def get_intraday_trend(self) -> str:
        """Determines active intraday trend: 'DOWNTREND', 'UPTREND', or 'RANGE'.
        
        Evaluates:
        1. Swing pivot structure: lower highs & lower lows vs higher highs & higher lows.
        2. Session open price relation.
        """
        if not self.candles:
            return "RANGE"

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
        # TIME GATE: No new entries after 14:00 IST (avoids late-session chop and EOD theta crush)
        if c.start.hour >= 14:
            return

        last_sh = self.swing_highs[-1]
        last_sl = self.swing_lows[-1]

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

            if H >= 15.0 and c.high < sh_px:
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
                    tgt1 = sl_px
                    tgt2 = min(self.val, sl_px - 0.618 * H) if (self.val > 0 and self.val < sl_px) else (sl_px - 0.618 * H)

                    opt_leg = compute_option_leg(
                        spot=entry,
                        direction="SHORT",
                        risk_pts=risk,
                        tgt1_price=tgt1,
                        tgt2_price=tgt2,
                        chain_snapshot=getattr(self, "chain_snapshot", None),
                    )

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
                        entry_prem=opt_leg["entry_prem"],
                        sl_prem=opt_leg["sl_prem"],
                        tgt1_prem=opt_leg["tgt1_prem"],
                        tgt2_prem=opt_leg["tgt2_prem"],
                    )
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

            if H >= 15.0 and c.low > sl_px:
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
                    tgt1 = sh_px
                    tgt2 = max(self.vah, sh_px + 0.618 * H) if (self.vah > 0 and self.vah > sh_px) else (sh_px + 0.618 * H)

                    opt_leg = compute_option_leg(
                        spot=entry,
                        direction="LONG",
                        risk_pts=risk,
                        tgt1_price=tgt1,
                        tgt2_price=tgt2,
                        chain_snapshot=getattr(self, "chain_snapshot", None),
                    )

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
                        entry_prem=opt_leg["entry_prem"],
                        sl_prem=opt_leg["sl_prem"],
                        tgt1_prem=opt_leg["tgt1_prem"],
                        tgt2_prem=opt_leg["tgt2_prem"],
                    )
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
        runner_r = pts / t.risk_pts
        if t.half_booked:
            t1_r = max(1.0, abs(t.tgt1_price - t.entry_price) / t.risk_pts)
            t.realized_r = round(0.50 * t1_r + 0.50 * runner_r, 2)
        else:
            t.realized_r = round(runner_r, 2)
        t.status = "CLOSED"
        t.exit_price = px
        t.exit_time = exit_time.isoformat()
        t.exit_reason = "SQUARE_OFF_1510"
        beta = getattr(settings, "PREMIUM_BETA", 0.50)
        t.exit_prem = max(0.5, round(t.entry_prem + (pts * beta), 1))
        self.trades.append(t)
        self.active_trade = None
        if self.notifier:
            self.notifier("EXIT", t)
        return [t]


class UnifiedLiquiditySniper:
    """Unified Institutional Liquidity & Fibonacci Sniper System.
    
    Combines:
    1. Arm 1: Phase 14 M/W Sweeps at Extremes (PDH / PDL / OI Walls with MSS & Fib Extensions).
    2. Arm 2: Trendline & Golden Pocket Retracements (Intraday Continuations without breaking previous liquidity).
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
        self.notifier = notifier
        self.arm1 = ShadowMWEngine(store=store, today_iso=self.today_iso, isolated=isolated, notifier=notifier)
        self.arm2 = LiquidityTrendlineFibEngine(pdh=pdh, pdl=pdl, store=store, vah=vah, val=val, poc=poc, notifier=notifier)

    def on_candle(self, c: Candle, day_state: Any = None, chain: Any = None) -> None:
        if chain is not None:
            self.arm1.chain_snapshot = chain
            self.arm2.chain_snapshot = chain
        self.arm1.on_candle(c, spot=c.close, chain=chain, day_state=day_state)
        self.arm2.on_candle(c, chain_snapshot=chain)

    def square_off(self, exit_price: float, exit_time: datetime) -> None:
        self.arm1.square_off(exit_price=exit_price, exit_time=exit_time)
        self.arm2.square_off(exit_price=exit_price, exit_time=exit_time)

    def get_summary(self) -> dict[str, Any]:
        s1 = self.arm1.get_summary(self.today_iso)
        t1 = [t for t in self.arm1.trades if t.status == "CLOSED"]
        t2 = [t for t in self.arm2.trades if t.status == "CLOSED"]
        all_closed = t1 + t2
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
        }


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


