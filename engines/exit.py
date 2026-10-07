"""Module 4 - exit engine (Phase B). Pure trigger checks in spec priority order;
the caller owns state, journal writes and alerts.

Triggers here: a) candle CLOSE beyond stop (ORB mid pre-1R / breakeven on runner),
b) premium SL (LTP <= 75% of entry; breakeven premium on runner), c) thesis dead
(close back inside the broken level), d) OI flip (fresh writing beyond the entry
direction), e) VIX spike >8%, f) time stop 75 min without +1R, and the runner's
+2R exit. The +1R "BOOK 50%" event and the 15:10 forced square-off are handled by
the caller.
"""
import logging

import settings
from engines.oi_engine import recent_oi_net, spot_adjacent

log = logging.getLogger(__name__)


class ExitEngine:
    def __init__(self, store):
        self._store = store

    def check(self, trade, ctx: dict):
        """First matching trigger wins. ctx: candle (closed Candle | None),
        snapshot (latest chain | None), spot, ltp, vix_spike, now.
        Returns (code, reason) or None."""
        candle = ctx.get("candle")
        now = ctx.get("now")
        stop_spot = trade.effective_stop_spot

        # a) spot stop on candle close
        if candle is not None:
            beyond = candle.close < stop_spot if trade.direction == "LONG" \
                else candle.close > stop_spot
            if beyond:
                label = "breakeven stop" if trade.runner_armed else "hard SL"
                anchor = "entry spot" if trade.runner_armed else "ORB mid"
                return ("SL_SPOT", f"{label} - 5-min close {candle.close:.2f} "
                                  f"beyond {anchor} {stop_spot:.2f}")

            # c) thesis dead: close back inside the broken level
            inside = candle.close < trade.broken_level if trade.direction == "LONG" \
                else candle.close > trade.broken_level
            if inside:
                return ("THESIS_DEAD", f"candle closed back inside {trade.level_name} "
                                       f"{trade.broken_level:.2f} - thesis dead")

        # structure break (Trend-Day v2): 5-min close beyond the structure trail -
        # only when a structure trail is armed (post OI-flip / +1R)
        if trade.trail_stop is not None and candle is not None:
            broken_trail = candle.close < trade.trail_stop \
                if trade.direction == "LONG" else candle.close > trade.trail_stop
            if broken_trail:
                return ("STRUCTURE_BREAK",
                        f"structure break - 5-min close {candle.close:.2f} beyond "
                        f"trail {trade.trail_stop:.2f}")

        # b) premium stop - NOT used once a structure trail is armed ("giving back
        # open profit to the level is the COST of riding trends")
        ltp = ctx.get("ltp")
        if trade.trail_stop is None and ltp is not None \
                and ltp <= trade.effective_stop_prem:
            label = "breakeven premium" if trade.runner_armed else "premium SL"
            return ("SL_PREM", f"{label} - LTP {ltp:.2f} <= "
                               f"{trade.effective_stop_prem:.2f}")

        # d) OI flip: fresh writing beyond the entry direction ("OI whispers,
        # price decides" - the ACTION for this code lives in main: book 50% +
        # structure trail on the first flip, full exit on a second flip within
        # 30 min; price confirms via close-back-inside / structure break)
        snapshot = ctx.get("snapshot")
        if snapshot is not None and snapshot.underlying and snapshot.rows:
            spot = ctx.get("spot") or snapshot.underlying
            near = spot_adjacent(snapshot.rows, spot,
                                 count=settings.OI_SPOT_ADJACENT_COUNT,
                                 side="above" if trade.direction == "LONG" else "below",
                                 expiry=snapshot.nearest_expiry)
            net = recent_oi_net(self._store.conn, snapshot.nearest_expiry,
                                [r.strike for r in near],
                                "ce" if trade.direction == "LONG" else "pe")
            if any(v > 0 for v in net.values()):
                detail = ", ".join(f"{s}:{v:+.0f}" for s, v in sorted(net.items())
                                   if v > 0)
                return ("OI_FLIP", f"OI flip - fresh writing beyond spot ({detail})")

        # e) VIX spike
        if ctx.get("vix_spike"):
            return ("VIX_SPIKE", "India VIX spiked >8% intraday")

        # f) time stop - applies ONLY in RANGE state; trend runners may hold to
        # 15:10 on the structure trail (Trend-Day v2 module D)
        if not trade.runner_armed and now is not None:
            day_state = ctx.get("day_state") or {}
            state = day_state.get("state")
            elapsed = (now - trade.entry_time).total_seconds() / 60
            if state not in ("TREND-UP", "TREND-DOWN") \
                    and elapsed >= settings.TIME_STOP_MINUTES and not trade.hit_tgt1():
                return ("TIME_STOP", f"time stop - {elapsed:.0f} min without +1R "
                                     f"(TGT 1 {trade.tgt1_spot:.2f})")

        # runner: +2R exit
        if trade.runner_armed and trade.hit_tgt2(ctx.get("spot")):
            return ("TGT_2R", f"target 2 reached (+2R, spot touched "
                              f"{trade.tgt2_spot:.2f}) - exit runner")
        return None
