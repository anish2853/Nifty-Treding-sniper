"""Daily-worker orchestration (Phases A/B/B+ on the MarketDataStore) - the phone
is the only UI.

SINGLE SOURCE OF TRUTH (hotfix): MarketDataStore fetches ONCE per 60 s cycle
(5-min candles, option chain = nearest + next expiry, India VIX), validates every
fetch (symbol NIFTY, strikes x50, underlyingValue within +/-3% of stored PDC) and
stores values + timestamps + source. PDC/PDH/PDL come from the pre-market
yfinance fetch, stored once, reused all day. EVERY consumer - regime, liquidity
map, entry, exit, sanity gates, trade cards, EOD - reads ONLY from the store;
every message footer shows "spot X | PDC Y | store TS" so mismatches are visible
instantly. If PDC is unavailable after 3 startup retries -> NO-TRADE DAY (a
silent bot beats a wrong bot).

Hard day lifecycle (IST): 08:45 pre-market report -> 09:15 regime -> 60 s cycles
(🎯 breakout + sweep-fade evaluations on candle CLOSE, 🚨 exits, 1R booking) ->
15:10 ⏹ square-off -> 15:40 📕 EOD -> 15:45 🔴 shutdown. Watchdog: crash -> log +
⚠️ + 60 s + in-place restart (max 5/day). All Telegram traffic flows through ONE
choke point that refuses to transmit for any non-live data source, so test or
fixture data can never reach a real chat again.

Modes: live | now | backtest (Phase C) | chain-test | score. ZERO order code.
"""
import argparse
import json
import logging
import sys
import time
from datetime import datetime

from alerts import messages
from alerts.telegram_bot import TelegramSender
from data_sources.market_data import MarketDataStore
from data_sources.nse_chain import _contract_info_url, _v3_url
from data_sources.nse_session import NSESession
from engines.coil_snipe import CoilSnipeEngine
from engines.entry import EntryEngine
from engines.exit import ExitEngine
from engines.candles import orb_levels
from engines.day_state import DayStateEngine
from engines.level_runtime import LevelRuntime
from engines.liquidity import room_to_run
from engines.mtf_live import (MTF_FAMILIES, coil_viz_payload, compute_mtf_tgt1,
                              event_family, is_mtf_family, journal_viz,
                              mtf_entry_allowed, ready_confirmed_zone,
                              zone_viz_payload)
from engines.replay_mtf import run_replay_mtf
from engines.oi_engine import recent_oi_net, spot_adjacent
from engines.pullback import PullbackEngine
from engines.paper import PaperTrade
from engines.regime import Regime, RegimeClassifier, RegimeState
from engines.sweep import SweepEngine
from journal import stats
from journal.store import JournalStore
import premarket_report
import settings
from utils import fmt_num, ist_now, load_date_set, safe_print, setup_logging

log = logging.getLogger("main")

LOOP_SLEEP_SEC = 2      # loop granularity
CYCLE_INTERVAL_SEC = settings.CANDLE_POLL_INTERVAL_SEC   # the ONE 60 s cycle


def wait_until(target) -> None:
    now = ist_now()
    wake = datetime.combine(now.date(), target, tzinfo=settings.IST)
    if wake > now:
        minutes = (wake - now).total_seconds() / 60
        log.info("Sleeping %.0f min until %02d:%02d IST...", minutes,
                 target.hour, target.minute)
        time.sleep((wake - now).total_seconds())


def round_to_strike(spot: float) -> int:
    step = settings.STRIKE_ROUND_TO
    return int(round(spot / step) * step)


def option_ltp_for(snapshot, strike: int, direction: str, expiry: str | None = None):
    """Option LTP at one strike from a chain snapshot (trade expiry by default)."""
    if snapshot is None:
        return None
    want = expiry or snapshot.nearest_expiry
    for row in snapshot.rows:
        if row.strike == strike and row.expiry == want:
            return row.ce_ltp if direction == "LONG" else row.pe_ltp
    return None


def ltp_is_plausible(ltp, spot) -> bool:
    """Frozen sanity band: option LTP between Rs 0.5 and 10% of spot."""
    return ltp is not None and \
        settings.OPTION_LTP_MIN <= ltp <= spot * settings.OPTION_LTP_MAX_PCT_OF_SPOT


class TradeBook:
    """Open paper trades, journal-backed recovery, and daily entry-budget queries."""

    def __init__(self, store: JournalStore, today_iso: str):
        self.store = store
        self.today_iso = today_iso
        self.trades: list = []

    def load(self) -> None:
        closed = {r[0] for r in self.store.conn.execute(
            "SELECT trade_id FROM journal WHERE type = 'EXIT' AND trade_id IS NOT NULL "
            "AND substr(ts, 1, 10) = ?", (self.today_iso,)).fetchall()}
        armed = {}
        for trade_id, reasons_json in self.store.conn.execute(
                "SELECT trade_id, reasons_json FROM journal WHERE type = 'SKIP' AND "
                "trade_id IS NOT NULL AND substr(ts, 1, 10) = ? ORDER BY id",
                (self.today_iso,)).fetchall():
            try:
                data = json.loads(reasons_json or "{}")
            except ValueError:
                continue
            if data.get("event") == "1R":
                armed[trade_id] = data.get("booked_prem")
        rows = self.store.conn.execute(
            "SELECT trade_id, reasons_json FROM journal WHERE type = 'SIGNAL' AND "
            "trade_id IS NOT NULL AND substr(ts, 1, 10) = ? ORDER BY id",
            (self.today_iso,)).fetchall()
        for trade_id, reasons_json in rows:
            if trade_id in closed:
                continue
            try:
                data = json.loads(reasons_json or "{}")
                trade = PaperTrade.from_reasons(data)
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("Trade recovery failed for %s: %s", trade_id, exc)
                continue
            if trade.trade_id in armed:
                trade.runner_armed = True
                trade.booked_prem = armed.get(trade.trade_id)
            self.trades.append(trade)
        if self.trades:
            log.info("Recovered %d open paper trade(s) from journal", len(self.trades))

    def signals_today(self) -> int:
        return self.store.conn.execute(
            "SELECT COUNT(*) FROM journal WHERE type = 'SIGNAL' AND "
            "substr(ts, 1, 10) = ?", (self.today_iso,)).fetchone()[0]

    def direction_count(self, direction: str) -> int:
        return self.store.conn.execute(
            "SELECT COUNT(*) FROM journal WHERE type = 'SIGNAL' AND direction = ? AND "
            "substr(ts, 1, 10) = ?", (direction, self.today_iso)).fetchone()[0]

    def caps_ok(self, direction: str, day_state=None, reentry=False):
        total = self.budget_used()
        limit = self.budget_max(day_state)
        count = self.direction_count(direction)
        if count >= settings.MAX_TRADES_PER_DIRECTION and not reentry:
            return False, (f"{direction} cap reached "
                           f"({count}/{settings.MAX_TRADES_PER_DIRECTION})")
        if total >= limit and not reentry:
            return False, f"budget exhausted ({total}/{limit} on a {day_state or 'unknown'} day)"
        return True, ""

    def budget_used(self) -> int:
        """SIGNAL rows today EXCLUDING wall-unwind re-entries (they never count
        toward the budget - Trend-Day v2.1)."""
        return self.store.conn.execute(
            "SELECT COUNT(*) FROM journal WHERE type = 'SIGNAL' AND "
            "substr(ts, 1, 10) = ? AND (notes IS NULL OR "
            "notes NOT LIKE '%re-entry grant%')", (self.today_iso,)).fetchone()[0]

    def budget_max(self, day_state: str | None) -> int:
        return settings.BUDGET_TREND if day_state in ("TREND-UP", "TREND-DOWN") \
            else settings.BUDGET_RANGE

    def reentry_used(self, direction: str) -> bool:
        return bool(self.store.conn.execute(
            "SELECT COUNT(*) FROM journal WHERE type = 'SIGNAL' AND direction = ? "
            "AND substr(ts, 1, 10) = ? AND notes LIKE '%WALL-UNWINDING re-entry grant used%'",
            (direction, self.today_iso)).fetchone()[0])

    def reentry_granted(self, direction: str) -> bool:
        return bool(self.store.conn.execute(
            "SELECT COUNT(*) FROM journal WHERE type = 'SKIP' AND direction = ? "
            "AND substr(ts, 1, 10) = ? AND notes LIKE '%one re-entry armed%'",
            (direction, self.today_iso)).fetchone()[0])

    def last_signal_ts(self) -> str | None:
        return self.store.conn.execute(
            "SELECT MAX(ts) FROM journal WHERE type = 'SIGNAL' AND "
            "substr(ts, 1, 10) = ?", (self.today_iso,)).fetchone()[0]

    def day_total_r(self) -> float:
        return self.store.conn.execute(
            "SELECT COALESCE(SUM(result_r), 0) FROM journal WHERE type = 'EXIT' "
            "AND result_r IS NOT NULL AND substr(ts, 1, 10) = ?",
            (self.today_iso,)).fetchone()[0]

    def day_pnl_rupees(self) -> float:
        return self.store.conn.execute(
            "SELECT COALESCE(SUM(pnl_rupees), 0) FROM journal WHERE type = 'EXIT' "
            "AND pnl_rupees IS NOT NULL AND substr(ts, 1, 10) = ?",
            (self.today_iso,)).fetchone()[0]

    def add(self, trade: PaperTrade) -> None:
        self.trades.append(trade)

    def remove(self, trade: PaperTrade) -> None:
        if trade in self.trades:
            self.trades.remove(trade)


class DaySession:
    """One trading day's wiring. Every data read goes through MarketDataStore;
    every Telegram send goes through _notify (live data source only)."""

    def __init__(self, store: JournalStore, tg: TelegramSender,
                 classifier: RegimeClassifier, event_day: bool, today_iso: str,
                 market: MarketDataStore, holidays=None, data_source: str = "live-chain"):
        self.store = store
        self.tg = tg
        self.classifier = classifier
        self.event_day = event_day
        self.today_iso = today_iso
        self.market = market
        self.holiday_dates = holidays or set()
        self.data_source = data_source
        self.entry = EntryEngine(market.journal)
        self.exit_engine = ExitEngine(market.journal)
        self.sweeps = SweepEngine(market.journal)
        self.pullbacks = PullbackEngine(market.journal)
        self.level_runtime = LevelRuntime()
        self.coil_engine = CoilSnipeEngine()
        self.day_state_engine = DayStateEngine()
        self._fired_mtf_keys: set[str] = set()
        self.day_state = None            # DayState | None (Trend-Day v2)
        self.trading_halted_reason = None
        self.book = TradeBook(store, today_iso)
        self.reentry_used = {direction: self.book.reentry_used(direction)
                     for direction in ("LONG", "SHORT")}
        self.regime: RegimeState | None = None
        self.us_direction = 0
        self.opening_spot = None
        self.first_cycle = True
        self.reentry_available = {
            direction: (not self.reentry_used[direction]
                        and self.book.reentry_granted(direction))
            for direction in ("LONG", "SHORT")}
        self.HARD_EXIT_CODES = {"VIX_SPIKE", "TIME_STOP", "TGT_2R", "SQUARE_OFF",
                                "OPPOSITE_SIGNAL", "STRUCTURE_BREAK"}

    # -- the ONE Telegram choke point ---------------------------------------------
    def _notify(self, text: str) -> None:
        if self.data_source == "live-chain":
            self.tg.send(text)
        else:
            log.info("telegram suppressed (data_source=%s): %.80s", self.data_source,
                     text.replace("\n", " | "))

    # -- regime gates ----------------------------------------------------------------
    def regime_allows(self, direction: str, level_name: str | None):
        state = self.regime
        if state is None:
            return False, "regime not yet classified"
        if state.regime == Regime.NO_TRADE_DAY:
            return False, "event day"
        if state.low_confidence:
            return False, "LOW-CONFIDENCE suppresses signals"
        if state.mean_reversion:
            return False, "mean-reversion regime suppresses breakout entries"
        if state.vix_spike:
            return False, "VIX spike regime-change flag"
        if (state.regime == Regime.GAP_UP_EXTENSION and direction == "LONG"
                and level_name == "ORB high"):
            return False, "gap-up-extension: no fresh ORB longs"
        if state.regime == Regime.GAP_DOWN and direction == "LONG":
            return False, "gap-down: no counter-trend longs"
        return True, ""

    def regime_allows_sweep(self, direction: str):
        state = self.regime
        if state is None:
            return False, "regime not yet classified"
        if state.regime == Regime.NO_TRADE_DAY:
            return False, "event day"
        if state.low_confidence:
            return False, "LOW-CONFIDENCE suppresses signals"
        if state.vix_spike:
            return False, "VIX spike regime-change flag"
        if state.regime == Regime.GAP_DOWN and direction == "LONG":
            return False, "gap-down: no counter-trend longs"
        return True, ""

    def _entry_permission(self, family: str):
        """Trend-Day v2 module A permissions. None = allowed."""
        if ist_now().time() >= settings.LAST_ENTRY_TIME:
            return "no new entries after 14:30"
        day = self.day_state
        if day is None:
            return None
        if is_mtf_family(family):
            return mtf_entry_allowed(self, family)
        if day.state == "RANGE" and family in ("ORB", "LIQ-BREAK"):
            return "range day - breakout entries off (sweep-fade only)"
        if day.broken_level is not None:
            # post-break: STRUCTURE governs, not the clock (09:30-14:30)
            if not (settings.ENTRY_EVAL_START <= ist_now().time()
                    <= settings.LAST_ENTRY_TIME):
                return "outside structure window 09:30-14:30"
            if not day.structure_intact:
                return "structure broken - state re-evaluating"
        return None

    def caps_ok(self, direction: str):
        """Dynamic budget (Trend-Day v2.1): TREND 4 / RANGE 2 entries; wall-unwind
        re-entries never count toward the budget; cooldown 20 min (trend) /
        30 min (range) between entries."""
        state = self.day_state.state if self.day_state else None
        direction_count = self.book.direction_count(direction)
        reentry = self.reentry_available.get(direction, False) and \
            direction_count >= settings.MAX_TRADES_PER_DIRECTION
        ok, note = self.book.caps_ok(direction, day_state=state, reentry=reentry)
        if not ok:
            return False, note
        last_ts = self.book.last_signal_ts()
        if last_ts:
            cooldown = settings.COOLDOWN_TREND_SEC \
                if state in ("TREND-UP", "TREND-DOWN") else settings.COOLDOWN_RANGE_SEC
            since = (ist_now() - datetime.fromisoformat(last_ts)).total_seconds()
            if since < cooldown:
                return False, (f"cooldown {since:.0f}s < {cooldown}s on a "
                               f"{state or 'unknown'} day")
        return True, ("re-entry granted (WALL-UNWINDING)" if reentry else "ok")

    def _ladder_text(self) -> str:
        if not self.market.liquidity:
            return ""
        return "\n" + "\n".join(self.market.liquidity.ladder_lines())

    # -- hard data sanity gates (unchanged behaviour, store-fed) --------------------
    def _data_gate(self, spot) -> tuple:
        if self.data_source != "live-chain":
            return False, (f"data source is {self.data_source} - live signals "
                           f"disabled outside --mode live/now")
        if self.market.pdc is None:
            return False, "PDC unavailable - spot cannot be sanity-checked"
        deviation = abs(spot / self.market.pdc - 1)
        if deviation > settings.SPOT_PDC_MAX_DEVIATION:
            return False, (f"spot {spot:.2f} deviates {deviation:+.2%} from PDC "
                           f"{self.market.pdc:.2f} "
                           f"(> {settings.SPOT_PDC_MAX_DEVIATION:.0%})")
        return True, ""

    def _alert_data_mismatch(self, reason: str) -> None:
        log.warning("DATA MISMATCH: %s - signal evaluation suppressed", reason)
        marker = f"data mismatch: {reason[:60]}"
        if not self.store.has_journal_event(self.today_iso, marker):
            self.store.journal_event("SKIP", notes=marker)
            self._notify(messages.data_mismatch(reason))

    # -- the ONE 60 s cycle ----------------------------------------------------------
    def run_cycle(self) -> None:
        """store.refresh() ONCE -> entry evaluations on the LATEST fresh close ->
        regime updates -> poll-based exits and 1R booking. Nothing fetches
        elsewhere; nothing stale ever reaches the engines (freshness gate)."""
        status = self.market.refresh()
        for alarm in status.get("alarms", []):
            detail = None
            if alarm == "option_chain" and self.market.chain_tried:
                detail = ", ".join(f"{label}={code if code is not None else 'ERR'}"
                                   for label, code in self.market.chain_tried)
            self._notify(messages.feed_alert(
                alarm, settings.FEED_FAILURE_ALERT_THRESHOLD, detail=detail))
        log.info("cycle: %s", self.market.data_footer())

        # Day-State Engine (Trend-Day v2 module A): VWAP/EMA-20/liquidity break/
        # structure/classification - computed once, read by every gate below.
        self.day_state = self.day_state_engine.update(
            self.market.candles, self.market.pdh, self.market.pdl,
            self.market.spot_value())
        # ratchet structure trails for open runners
        if self.day_state:
            for trade in self.book.trades:
                if trade.trail_stop is not None:
                    if trade.direction == "LONG" \
                            and self.day_state.last_higher_low is not None:
                        trade.trail_stop = max(trade.trail_stop,
                                               self.day_state.last_higher_low)
                    elif trade.direction == "SHORT" \
                            and self.day_state.last_lower_high is not None:
                        trade.trail_stop = min(trade.trail_stop,
                                               self.day_state.last_lower_high)
            log.info("day state: %s", self.day_state.summary())

        if self.day_state is not None:
            for event in self.level_runtime.refresh_grid(self.market, self.day_state):
                self._handle_level_event(event)

        minute_candidates = list(getattr(self.market.candle_engine, "fresh_minutes", []) or [])
        if self.first_cycle and minute_candidates:
            minute_candidates = minute_candidates[-1:]
        for minute in minute_candidates:
            self._process_minute_close(minute)

        fresh = status.get("fresh_candles") or []
        if fresh:
            if self.first_cycle:
                # Backlog is NEVER a signal candidate (hotfix: Signal #2 fired 85
                # min after its candle from a processed backlog).
                log.info("STALE backlog discarded: %d candle(s) at startup - never "
                         "signal candidates", len(fresh))
                self.store.journal_event(
                    "SKIP", notes=f"backlog discarded: {len(fresh)} stale candle(s) "
                                  f"at startup (never signal candidates)")
            else:
                for stale in fresh[:-1]:
                    log.info("STALE discarded (superseded by a newer candle): %s",
                             stale.start.isoformat(timespec="seconds"))
                candidate = fresh[-1]                # ONLY the latest closed candle
                age = (ist_now() - candidate.end).total_seconds()
                if age > settings.STALE_CANDLE_MAX_AGE_SEC:
                    log.warning("STALE discarded: candle %s closed %.0f s ago "
                                "(> %d s) - never fired",
                                candidate.start.isoformat(timespec="seconds"),
                                age, settings.STALE_CANDLE_MAX_AGE_SEC)
                else:
                    self.process_candle_close(candidate)
        self.first_cycle = False
        self.update_regime()
        self.process_poll()

    def _entry_ctx(self, candle, spot=None) -> dict:
        return {
            "candle": candle,
            "snapshot": self.market.chain,
            "spot": spot if spot is not None else (candle.close if candle else None),
            "vix_spike": bool(self.regime.vix_spike) if self.regime else False,
            "event_day": self.event_day,
            "regime_allows": self.regime_allows,
            "regime_allows_sweep": self.regime_allows_sweep,
            "caps_ok": self.caps_ok,
            "option_ltp": self.option_ltp,
            "candle_engine": self.market.candle_engine,
            "orb": orb_levels(self.market.candles),
            "pdh": self.market.pdh,
            "pdl": self.market.pdl,
            "liquidity": self.market.liquidity,
            "day_state": self.day_state.as_ctx() if self.day_state else None,
            "vwap": self.day_state.vwap if self.day_state else None,
            "vwap_proxy": self.day_state.vwap_proxy if self.day_state else False,
            "now": ist_now(),
        }

    def option_ltp(self, direction: str, spot):
        return option_ltp_for(self.market.chain, round_to_strike(spot), direction,
                              expiry=self.market.trade_expiry)

    def trade_ltp(self, trade: PaperTrade):
        return option_ltp_for(self.market.chain, trade.strike, trade.direction,
                              expiry=self.trade_expiry_of(trade))

    def trade_expiry_of(self, trade: PaperTrade):
        return trade.expiry or self.market.trade_expiry

    # -- regime --------------------------------------------------------------------------
    def update_regime(self) -> None:
        chain = self.market.chain
        if chain is None:
            return
        vix_value = self.market.vix.value.get("value") if self.market.vix else None
        vix_prev = self.market.vix.value.get("prev_close") if self.market.vix else None
        if self.regime is None:
            # STORED opening gap: mid-day starts classify from the backfilled
            # day-open print, never from the current spot.
            spot_for_gap = self.opening_spot or chain.underlying
            candidate = self.classifier.classify_at_open(
                spot_for_gap, self.market.pdc, self.us_direction, self.event_day)
            if candidate.gap_pct is not None or candidate.regime == Regime.NO_TRADE_DAY:
                if self.opening_spot is not None:
                    candidate.notes.append(
                        "gap uses the day's opening print (backfilled), not current spot")
                self.classifier.apply_pcr(candidate, chain.pcr_total)
                self.classifier.apply_vix(candidate, vix_value, vix_prev)
                self.regime = candidate
                reasons = candidate.reasons()
                reasons["pdh"], reasons["pdl"] = self.market.pdh, self.market.pdl
                self.store.journal_event("REGIME", regime=self.regime.regime.value,
                                         spot=chain.underlying, reasons=reasons,
                                         notes="09:15 classification (first spot print)")
                if not self.event_day:
                    self._notify(messages.regime(self.regime.summary(),
                                                 self.regime.notes)
                                 + self._ladder_text())
            else:
                log.warning("Regime classification deferred: spot or PDC missing")
        else:
            if self.classifier.apply_pcr(self.regime, chain.pcr_total):
                self.store.journal_event("REGIME", regime=self.regime.regime.value,
                                         reasons=self.regime.reasons(),
                                         notes="PCR context update")
            if (self.classifier.apply_vix(self.regime, vix_value, vix_prev)
                    and self.regime.vix_spike and not self.event_day):
                self.store.journal_event("REGIME", regime=self.regime.regime.value,
                                         reasons=self.regime.reasons(),
                                         notes="VIX spike >8% regime-change flag")
                self._notify(messages.regime_change(self.regime.summary())
                             + self._ladder_text())

    # -- entries ---------------------------------------------------------------------------
    def process_candle_close(self, candle) -> None:
        gate_ok, gate_reason = self._data_gate(candle.close)
        if not gate_ok:
            self._alert_data_mismatch(gate_reason)
            return
        # Spot-consistency (freshness gate): the setup is stale when the store's
        # live spot has drifted more than 0.25% from the candle close.
        store_spot = self.market.spot_value()
        if store_spot is not None:
            drift = abs(store_spot - candle.close) / store_spot
            if drift > settings.SPOT_STALENESS_BAND_PCT:
                log.warning("STALE discarded: store spot %.2f vs candle close %.2f "
                            "(%.2f%% drift > %.2f%%)", store_spot, candle.close,
                            drift * 100, settings.SPOT_STALENESS_BAND_PCT * 100)
                return
        prev = self.market.candle_engine.prev_candle(candle)
        ctx = self._entry_ctx(candle)
        for ev in self.entry.evaluate(candle, prev, ctx):
            # setup families: ORB break vs liquidity (PDH/PDL) break
            fam = "ORB" if (ev.level_name or "").startswith("ORB") else "LIQ-BREAK"

            def breakout_fire(ev=ev, fam=fam):
                orb = orb_levels(self.market.candles)
                if orb is None:
                    self.store.journal_event(
                        "SKIP", direction=ev.direction, spot=candle.close,
                        score=ev.score, family=fam,
                        notes="signal blocked: ORB unavailable for SL midpoint")
                    return None
                return self.fire(ev, fam, sl_spot=orb["mid"],
                                 broken_level=ev.level, level_name=ev.level_name)
            self._handle_evaluation(ev, family=fam, fire_fn=breakout_fire)
        for ev in self.sweeps.evaluate(candle, ctx):
            def sweep_fire(ev=ev):
                return self.fire(ev, "SWEEP", sl_spot=ev.sl_spot,
                                 broken_level=ev.level_strike,
                                 level_name=f"swept {ev.level_label}")
            self._handle_evaluation(ev, family="SWEEP", fire_fn=sweep_fire)
        for ev in self.pullbacks.evaluate(
                candle, self.market.candle_engine.prev_candle(candle), ctx):
            def pullback_fire(ev=ev):
                return self.fire(ev, "PULLBACK", sl_spot=ev.sl_spot,
                                 broken_level=ev.level,
                                 level_name=f"pullback {ev.level_label}")
            self._handle_evaluation(ev, family="PULLBACK", fire_fn=pullback_fire)
        now = ist_now()
        for trade in list(self.book.trades):
            trade.update_extreme(candle.high if trade.direction == "LONG"
                                 else candle.low)
            decision = self.exit_engine.check(trade, ctx | {"ltp": trade.last_prem,
                                                            "now": now})
            if decision:
                self._execute_exit(trade, decision, spot=candle.close)
        self._process_five_minute_mtf(candle)

    def _prev_minute(self, minute):
        candles = self.market.candle_engine.minute_candles
        try:
            index = candles.index(minute)
        except ValueError:
            return None
        return candles[index - 1] if index > 0 else None

    def _handle_level_event(self, event, candle=None, notify: bool = True) -> None:
        if event.zone is not None:
            self.level_runtime.zones[event.zone.zone_id] = event.zone
            journal_viz(self.store, self.today_iso, "zone", zone_viz_payload(event.zone),
                        note=event.kind)
        marker = f"{event.kind} {event.level_id} {event.message[:40]}"
        if self.store.has_journal_event(self.today_iso, marker):
            return
        self.store.journal_event(
            "SKIP", direction=event.direction, spot=self.market.spot_value(),
            family=event_family(event) or "LEVEL",
            notes=marker, reasons={"level_event": event.kind, "message": event.message})
        if notify and event.kind == "LEVEL-WATCH" and not self.event_day:
            self._notify(f"👀 {event.message}")

    def _handle_coil_event(self, event, candle=None) -> None:
        journal_viz(self.store, self.today_iso, "coil", coil_viz_payload(event.coil),
                    note=event.kind)
        marker = f"{event.kind} {event.coil.coil_id} {event.message[:48]}"
        if self.store.has_journal_event(self.today_iso, marker):
            return
        self.store.journal_event(
            "SKIP", direction=event.direction, spot=self.market.spot_value(),
            family="COIL-SNIPE", notes=marker,
            reasons={"coil_event": event.kind, "message": event.message,
                     "coil": event.coil.as_reasons()})
        if event.kind == "COIL" and not self.event_day:
            self._notify(f"🧲 {event.message}")

    def _process_minute_close(self, minute) -> None:
        if self.day_state is None:
            return
        previous = self._prev_minute(minute)
        for event in self.level_runtime.minute_close(minute, previous, self.day_state):
            self._handle_level_event(event)
        for event in self.coil_engine.on_minute(minute):
            if event.fade_zone is not None:
                self.level_runtime.zones[event.fade_zone.zone_id] = event.fade_zone
            self._handle_coil_event(event, minute)
        for trade in list(self.book.trades):
            if not trade.oi_flip_pending or trade.trigger_line is None:
                continue
            if trade.direction == "LONG" and minute.close < trade.trigger_line:
                self.close_trade(trade, "MTF_TRIGGER_BREAK",
                                 "OI flip + 1m close back below trigger line",
                                 spot=minute.close)
            elif trade.direction == "SHORT" and minute.close > trade.trigger_line:
                self.close_trade(trade, "MTF_TRIGGER_BREAK",
                                 "OI flip + 1m close back below trigger line",
                                 spot=minute.close)

    def _process_five_minute_mtf(self, candle) -> None:
        if self.day_state is None:
            return
        spot = candle.close
        for event in self.coil_engine.update_coil(
                self.market.candles, spot, self.day_state.state,
                self.day_state.vwap, self.level_runtime.grid):
            self._handle_coil_event(event, candle)
        events = self.level_runtime.five_minute_close(candle, self.market.candles)
        div_suppressed = any(event.kind == "DIVERGENCE-TRAP-SKIPPED" for event in events)
        for event in events:
            self._handle_level_event(event, candle=candle)
            family = event_family(event)
            if family == "LEVEL-CONT" and event.direction:
                self._fire_mtf(family=family, direction=event.direction, candle=candle,
                               entry_spot=float(candle.close), sl_spot=float(event.price),
                               broken_level=float(event.price),
                               level_name=f"break {event.label}",
                               trigger_line=float(event.price))
        for zone in self.level_runtime.zones.values():
            self._try_fire_confirmed_zone(zone, candle)
        for coil_event in self.coil_engine.on_five_minute(
                candle, self.level_runtime.grid, div_suppressed):
            self._handle_coil_event(coil_event, candle)
            if coil_event.kind == "ENTRY" and coil_event.direction:
                boundary = (coil_event.coil.high if coil_event.direction == "LONG"
                            else coil_event.coil.low)
                self._fire_mtf(
                    family="COIL-SNIPE", direction=coil_event.direction, candle=candle,
                    entry_spot=float(coil_event.entry), sl_spot=float(coil_event.stop),
                    broken_level=float(boundary),
                    level_name=f"coil break {boundary:.0f}",
                    trigger_line=float(boundary),
                    coil_meta=coil_event.coil.as_reasons())

    def _try_fire_confirmed_zone(self, zone, candle) -> None:
        now = ist_now()
        if not ready_confirmed_zone(zone, now):
            if zone.status in ("EXPIRED", "STALE"):
                journal_viz(self.store, self.today_iso, "zone", zone_viz_payload(zone))
            return
        key = f"{zone.zone_id}-{zone.confirm_time.isoformat()}"
        if key in self._fired_mtf_keys:
            return
        family = "LEVEL-FADE" if zone.zone_id.startswith("FADE-") else "MTF-SCALP"
        runway, wall = self.level_runtime.runway(zone.entry_spot, zone.direction)
        if runway is not None and runway < settings.ROOM_TO_RUN_MIN_PTS:
            self.store.journal_event(
                "SKIP", direction=zone.direction, spot=zone.entry_spot, family=family,
                notes=f"MTF runway {runway:.0f} pts to {wall.label if wall else '?'} "
                      f"(< {settings.ROOM_TO_RUN_MIN_PTS})")
            return
        if self._fire_mtf(
                family=family, direction=zone.direction, candle=candle,
                entry_spot=float(zone.entry_spot), sl_spot=float(zone.stop),
                broken_level=float(zone.broken_level),
                level_name=f"MTF zone {zone.zone_id}",
                trigger_line=float(zone.trigger), zone_id=zone.zone_id):
            self._fired_mtf_keys.add(key)
            zone.status = "FIRED"
            journal_viz(self.store, self.today_iso, "zone", zone_viz_payload(zone))

    def _fire_mtf(self, *, family: str, direction: str, candle, entry_spot: float,
                  sl_spot: float, broken_level: float, level_name: str,
                  trigger_line: float | None = None, zone_id: str | None = None,
                  coil_meta: dict | None = None):
        class _Ev:
            pass

        ev = _Ev()
        ev.direction = direction
        ev.candle = candle
        ev.score = settings.ENTRY_SCORE_FIRE
        ev.reasons = [("MTF confirmed", True, ev.score, level_name)]

        perm = self._entry_permission(family)
        if perm:
            self._suppress(ev, family, perm)
            return None
        if self.trading_halted_reason:
            return self._suppress(ev, family, f"trading halted: {self.trading_halted_reason}")

        ok, cap_note = self.caps_ok(direction)
        if not ok:
            return self._suppress(ev, family, cap_note)

        risk_pts = abs(entry_spot - sl_spot)
        if risk_pts <= 0 or risk_pts > settings.MTF_MAX_RISK_POINTS:
            return self._suppress(
                ev, family, f"geometry: entry-SL {risk_pts:.1f} pts "
                             f"(max {settings.MTF_MAX_RISK_POINTS})")

        runway, wall_label = self.level_runtime.runway(entry_spot, direction)
        if runway is not None and runway < settings.ROOM_TO_RUN_MIN_PTS:
            return self._suppress(
                ev, family, f"NO ROOM: {runway:.0f} pts to "
                            f"{wall_label.label if wall_label else '?'} "
                            f"(< {settings.ROOM_TO_RUN_MIN_PTS} pts)")

        if family == "LEVEL-FADE":
            sweep_ok, sweep_note = self.regime_allows_sweep(direction)
            if not sweep_ok:
                return self._suppress(ev, family, sweep_note)

        snapshot = self.market.chain
        spot = self.market.spot_value() or entry_spot
        strike = round_to_strike(spot)
        ltp = option_ltp_for(snapshot, strike, direction, expiry=self.market.trade_expiry)
        if not ltp_is_plausible(ltp, spot):
            return self._suppress(ev, family, f"option LTP implausible ({ltp})",
                                  alert=True)

        signal_no = self.book.signals_today() + 1
        trade = PaperTrade(
            trade_id=f"T{signal_no:02d}", direction=direction, strike=strike,
            expiry=self.market.trade_expiry or "", entry_spot=entry_spot,
            entry_prem=ltp, sl_spot=sl_spot, broken_level=broken_level,
            level_name=level_name, entry_time=ist_now(), score=ev.score,
            components=list(ev.reasons), candle_start=candle.start, family=family,
            strategy="MTF", zone_id=zone_id, trigger_line=trigger_line,
            booked_fraction=settings.MTF_BOOK_FRACTION,
            risk_units=settings.MTF_RISK_UNITS,
            runway_pts=runway, runway_wall=wall_label.label if wall_label else None)
        trade.tgt1_override = compute_mtf_tgt1(
            entry_spot, sl_spot, direction, self.level_runtime.grid)
        trade.extreme_spot = trade.entry_spot
        trade.capture_original_plan()
        reasons = trade.to_reasons()
        if coil_meta:
            reasons["coil"] = coil_meta
        self.store.journal_event(
            "SIGNAL", direction=trade.direction, strike=trade.strike,
            spot=trade.entry_spot, option_ltp=trade.entry_prem, score=trade.score,
            reasons=reasons, trade_id=trade.trade_id, family=family,
            day_state=self.day_state.state if self.day_state else None,
            notes=f"signal candle {family} {direction} "
                  f"{candle.start.isoformat(timespec='seconds')}")
        self.book.add(trade)
        self._notify(messages.trade_card(
            signal_no=signal_no, direction=direction, trade=trade,
            spot=trade.entry_spot, now_txt=ist_now().strftime("%H:%M:%S"),
            family=family,
            data_source=f"{self.data_source} | {self.market.data_footer()}"))
        log.info("SIGNAL %s [%s]: %s @ %.2f prem, SL %.2f, tgt1 %.2f | %s",
                 trade.trade_id, family, trade.option, trade.entry_prem,
                 trade.sl_spot, trade.tgt1_spot, self.market.data_footer())
        return trade

    def _handle_evaluation(self, ev, family: str, fire_fn) -> None:
        candle = ev.candle
        marker = f"{family} {ev.direction} {candle.start.isoformat(timespec='seconds')}"
        if ev.fired:
            if self.store.has_journal_event(self.today_iso, f"signal candle {marker}"):
                return
            ok, note = self.book.caps_ok(ev.direction)   # shared cap, re-checked
            if not ok:
                self.store.journal_event(
                    "SKIP", direction=ev.direction, spot=candle.close, score=ev.score,
                    family=family,
                    reasons={"score": ev.score, "direction": ev.direction,
                             "family": family},
                    notes=f"signal blocked ({marker}): {note}")
                return
            opposite = "SHORT" if ev.direction == "LONG" else "LONG"
            for trade in [t for t in list(self.book.trades)
                          if t.direction == opposite]:
                self.close_trade(trade, "OPPOSITE_SIGNAL",
                                 f"opposite {family} signal scored {ev.score} "
                                 f"({ev.direction} close {candle.close:.2f})",
                                 spot=candle.close)
            if fire_fn() is None:
                return
        elif ev.near_miss:
            if not self.store.has_journal_event(self.today_iso, f"near-miss {marker}"):
                self.store.journal_event(
                    "SKIP", direction=ev.direction, spot=candle.close, score=ev.score,
                    family=family,
                    reasons={"score": ev.score, "direction": ev.direction,
                             "family": family,
                             "candle_start":
                                 candle.start.isoformat(timespec="seconds"),
                             "components": ev.reasons},
                    notes=f"near-miss {marker} (score {ev.score})")
        elif getattr(ev, "blocked_note", ""):
            self.store.journal_event(
                "SKIP", direction=ev.direction, spot=candle.close, score=ev.score,
                family=family,
                reasons={"score": ev.score, "direction": ev.direction,
                         "family": family},
                notes=f"signal blocked ({marker}): {ev.blocked_note}")

    def fire(self, ev, family: str, sl_spot: float, broken_level, level_name: str):
        snapshot = self.market.chain
        now = ist_now()

        # Trend-Day v2 entry permissions (module A) - checked FIRST.
        perm = self._entry_permission(family)
        if perm:
            return self._suppress(ev, family, perm)
        if self.trading_halted_reason:
            return self._suppress(ev, family,
                                  f"trading halted: {self.trading_halted_reason}")

        # FIRE-TIME gate: candle close AND fire moment inside the governing
        # window. Before a liquidity break the CLOCK governs (09:30-11:00 or
        # 13:30-14:45); after a break STRUCTURE governs (09:30-14:30).
        broken = self.day_state is not None and self.day_state.broken_level is not None

        def in_window(t) -> bool:
            if broken:
                return settings.ENTRY_EVAL_START <= t <= settings.LAST_ENTRY_TIME
            return (settings.ENTRY_EVAL_START <= t <= settings.ENTRY_WINDOW_END
                    or settings.SWEEP_WINDOW_2_START <= t <= settings.SWEEP_WINDOW_2_END)

        if not (in_window(ev.candle.end.time()) and in_window(now.time())):
            return self._suppress(ev, family,
                                  f"fire-time outside entry window (candle close "
                                  f"{ev.candle.end.strftime('%H:%M')}, fire "
                                  f"{now.strftime('%H:%M')})")

        # ONE spot on the card: the store's live spot (the candle is just the
        # trigger) - no mixing of candle spot and live spot anywhere.
        spot = self.market.spot_value() or ev.candle.close

        # ROOM-TO-RUN gate (Trend-Day v2.1): >= 25 pts to the nearest significant
        # wall (or the measured move) or the signal never fires and never counts.
        runway, wall_label = room_to_run(
            self.market.liquidity.levels if self.market.liquidity else [],
            spot, ev.direction, self.market.pdh, self.market.pdl,
            broken_level=self.day_state.broken_level if self.day_state else None,
            max_oi_strike=(getattr(self.market.chain, "max_call_oi_strike", None)
                           if ev.direction == "LONG"
                           else getattr(self.market.chain, "max_put_oi_strike", None))
            if self.market.chain else None)
        if runway is not None and runway < settings.ROOM_TO_RUN_MIN_PTS:
            return self._suppress(
                ev, family, f"NO ROOM: {runway:.0f} pts to {wall_label} "
                            f"(< {settings.ROOM_TO_RUN_MIN_PTS} pts)")

        # dynamic budget + cooldown (Trend-Day v2.1 module 2)
        ok, cap_note = self.caps_ok(ev.direction)
        if not ok:
            return self._suppress(ev, family, cap_note)
        reentry_fired = "re-entry granted" in cap_note

        strike = round_to_strike(spot)
        if abs(strike - spot) > spot * settings.ATM_BAND_PCT:
            return self._suppress(ev, family,
                                  f"ATM strike {strike} not within "
                                  f"{settings.ATM_BAND_PCT:.0%} of spot {spot:.2f}",
                                  alert=True)
        ltp = option_ltp_for(snapshot, strike, ev.direction,
                             expiry=self.market.trade_expiry)
        if not ltp_is_plausible(ltp, spot):
            return self._suppress(ev, family,
                                  f"option LTP implausible ({ltp}) for {strike} "
                                  f"{self.market.trade_expiry} at spot {spot:.2f}",
                                  alert=True)
        if self.market.trade_expiry is None:
            return self._suppress(ev, family, "no tradeable expiry available",
                                  alert=True)
        signal_no = self.book.signals_today() + 1
        trade = PaperTrade(
            trade_id=f"T{signal_no:02d}", direction=ev.direction, strike=strike,
            expiry=self.market.trade_expiry, entry_spot=spot, entry_prem=ltp,
            sl_spot=sl_spot, broken_level=broken_level, level_name=level_name,
            entry_time=ist_now(), score=ev.score, components=list(ev.reasons),
            candle_start=ev.candle.start, family=family)
        trade.extreme_spot = trade.entry_spot
        trade.capture_original_plan()     # post-exit shadow tracks this plan
        trade.runway_pts = runway
        trade.runway_wall = wall_label
        if reentry_fired:
            self.reentry_available[ev.direction] = False   # grant consumed
        self.store.journal_event(
            "SIGNAL", direction=trade.direction, strike=trade.strike,
            spot=trade.entry_spot, option_ltp=trade.entry_prem, score=trade.score,
            reasons=trade.to_reasons(), trade_id=trade.trade_id, family=family,
            day_state=self.day_state.state if self.day_state else None,
            notes=f"signal candle {family} {ev.direction} "
                  f"{ev.candle.start.isoformat(timespec='seconds')}")
        self.book.add(trade)
        self._notify(messages.trade_card(
            signal_no=signal_no, direction=ev.direction, trade=trade,
            spot=trade.entry_spot, now_txt=ist_now().strftime("%H:%M:%S"),
            family=family,
            data_source=f"{self.data_source} | {self.market.data_footer()}"))
        log.info("SIGNAL %s [%s]: %s @ %.2f prem, score %d, SL %.2f, expiry %s | %s",
                 trade.trade_id, family, trade.option, trade.entry_prem,
                 trade.score, trade.sl_spot, trade.expiry, self.market.data_footer())
        # consume a WALL-UNWINDING re-entry grant when it was spent
        if self.book.direction_count(trade.direction) >= settings.MAX_TRADES_PER_DIRECTION:
            self.reentry_available[trade.direction] = False
            log.info("re-entry grant consumed for %s", trade.direction)
        return trade

    def _suppress(self, ev, family: str, reason: str, alert: bool = False):
        """A fired evaluation killed by a gate - journaled; sanity gates also
        raise the DATA MISMATCH alert, day-state/window gates stay silent."""
        self.store.journal_event(
            "SKIP", direction=ev.direction, spot=ev.candle.close, score=ev.score,
            family=family,
            day_state=self.day_state.state if self.day_state else None,
            reasons={"score": ev.score, "direction": ev.direction, "family": family},
            notes=f"signal suppressed by gate: {reason}")
        if alert:
            self._alert_data_mismatch(reason)
        return None

    # -- exits + 1R ---------------------------------------------------------------------------
    def close_trade(self, trade: PaperTrade, code: str, reason: str, spot=None,
                    grade: str = "OI-N/A") -> None:
        exit_prem = trade.last_prem if trade.last_prem is not None else trade.entry_prem
        result_r = trade.result_r(exit_prem)
        pnl = trade.pnl_rupees(exit_prem)
        self.store.journal_event("EXIT", direction=trade.direction,
                                 strike=trade.strike, spot=spot,
                                 option_ltp=exit_prem, result_r=result_r,
                                 exit_reason=f"{code} [{grade}]: {reason}",
                                 trade_id=trade.trade_id, pnl_rupees=pnl,
                                 score=trade.score, family=trade.family,
                                 day_state=self.day_state.state if self.day_state else None,
                                 reasons={"shadow": {
                                     "direction": trade.direction,
                                     "entry_spot": trade.entry_spot,
                                     "entry_prem": trade.entry_prem,
                                     "sl_spot": trade.sl_spot,
                                     "orig_tgt1": trade.orig_tgt1_spot,
                                     "orig_tgt2": trade.orig_tgt2_spot,
                                     "grade": grade, "code": code,
                                     "exit_ts": ist_now().isoformat(
                                         timespec="seconds")}},
                                 notes=f"exit {trade.trade_id}")
        hard_sl_prem = trade.entry_prem if trade.runner_armed \
            else trade.entry_prem * settings.SL_PREMIUM_FRACTION
        saved = max(0.0, (exit_prem - hard_sl_prem) * settings.LOT_SIZE)
        blended = trade.blended_points(exit_prem)
        self._notify(messages.exit_alert(
            option=trade.option, reason=reason, grade=grade,
            pnl_text=f"{blended:+.2f} pts (₹{pnl:+,.0f}) | result {result_r:+.2f}R",
            saved_text=f"₹{saved:,.0f}" if saved > 0 else None))
        log.info("EXIT %s %s [%s/%s]: %.2fR, Rs %.0f", trade.trade_id, trade.option,
                 code, grade, result_r or 0.0, pnl)
        self.book.remove(trade)
        self._check_halt()

    def _check_halt(self) -> None:
        """Frozen risk rails (Trend-Day v2 module E): daily loss cap 5% and the
        NEW profit lock - day reaches +2R total -> bank it, stop trading."""
        if self.trading_halted_reason:
            return
        day_r = self.book.day_total_r()
        day_pnl = self.book.day_pnl_rupees()
        if day_r >= settings.PROFIT_LOCK_R:
            self.trading_halted_reason = (f"PROFIT LOCK: day total {day_r:+.2f}R "
                                          f">= +{settings.PROFIT_LOCK_R:.0f}R - "
                                          f"banked, no further trading")
        elif day_pnl <= -settings.DAILY_LOSS_CAP_PCT * settings.CAPITAL:
            self.trading_halted_reason = (f"DAILY LOSS CAP: day {day_pnl:+,.0f} Rs "
                                          f"<= -{settings.DAILY_LOSS_CAP_PCT:.0%} "
                                          f"of capital")
        if self.trading_halted_reason:
            self.store.journal_event("SKIP", notes=self.trading_halted_reason)
            self._notify(messages.trading_halted(self.trading_halted_reason))
            log.warning(self.trading_halted_reason)

    # -- graded runner protocol (Prompt B+ hotfix #2) -----------------------------
    def _oi_grade(self, trade: PaperTrade, spot) -> str:
        """OI context at exit time: OI-STRONG (fresh writing building against the
        position) / OI-MODERATE (neutral) / WALL-UNWINDING (writers covering - the
        wall in front of the trade is dissolving) / OI-N/A (no chain data)."""
        snapshot = self.market.chain
        if snapshot is None or not snapshot.rows or spot is None:
            return "OI-N/A"
        near = spot_adjacent(snapshot.rows, spot,
                             count=settings.OI_SPOT_ADJACENT_COUNT,
                             side="above" if trade.direction == "LONG" else "below",
                             expiry=snapshot.nearest_expiry)
        net = recent_oi_net(self.market.journal.conn, snapshot.nearest_expiry,
                            [r.strike for r in near],
                            "ce" if trade.direction == "LONG" else "pe")
        values = list(net.values())
        if not values or all(v == 0 for v in values):
            return "OI-MODERATE"
        if any(v > 0 for v in values):
            return "OI-STRONG"
        return "WALL-UNWINDING"

    def _execute_exit(self, trade: PaperTrade, decision, spot) -> None:
        """Graded runner protocol + 'OI whispers, price decides' (Trend-Day v2
        module D). OI flip ALONE: book 50% + structure trail (never premium
        breakeven); full exit only on flip + close back inside / second flip
        within 30 min / structure break / hard SL. Hard triggers always exit."""
        code, reason = decision
        grade = self._oi_grade(trade, spot)

        if code == "OI_FLIP":
            now = ist_now()
            recent = [t for t in trade.flip_times
                      if (now - t).total_seconds() <= 1800]
            if recent:
                self.close_trade(trade, "SECOND_FLIP",
                                 "second OI flip within 30 min (wall thickening)",
                                 spot=spot, grade="OI-STRONG")
                return
            trade.flip_times.append(now)
            trade.runner_armed = True
            trade.booked_prem = trade.last_prem or trade.entry_prem
            if is_mtf_family(trade.family):
                trade.oi_flip_pending = True
                trade.trail_stop = trade.entry_spot
                book_pct = int((trade.booked_fraction or settings.MTF_BOOK_FRACTION) * 100)
                self.store.journal_event(
                    "SKIP", trade_id=trade.trade_id, option_ltp=trade.booked_prem,
                    day_state=self.day_state.state if self.day_state else None,
                    reasons={"event": "oi-flip-mtf", "book_pct": book_pct,
                             "trigger": trade.trigger_line},
                    notes=f"{trade.trade_id}: OI flip - booked {book_pct}%, "
                          f"runner SL at entry; watch 1m vs trigger")
                log.info("%s MTF OI flip: booked %d%%, trigger watch %.2f",
                         trade.trade_id, book_pct, trade.trigger_line or 0)
                blended = trade.blended_points(trade.booked_prem)
                self._notify(messages.exit_alert(
                    option=trade.option, grade="OI-FLIP",
                    reason=f"{reason} — booked {book_pct}%, runner at entry "
                           f"(1m below trigger = full exit)",
                    pnl_text=f"{blended:+.2f} pts "
                             f"(₹{trade.pnl_rupees(trade.booked_prem):+,.0f})"))
                return
            structure_level = (self.day_state.last_higher_low
                               if trade.direction == "LONG"
                               else self.day_state.last_lower_high) \
                if self.day_state else None
            trail = trade.structure_trail(structure_level=structure_level)
            self.store.journal_event(
                "SKIP", trade_id=trade.trade_id, option_ltp=trade.booked_prem,
                day_state=self.day_state.state if self.day_state else None,
                reasons={"event": "oi-flip", "grade": grade, "trail": trail},
                notes=f"{trade.trade_id}: OI flip - booked 50%, structure trail "
                      f"{trail:.2f}")
            log.info("%s OI flip: booked 50%%, trail %.2f", trade.trade_id, trail)
            blended = trade.blended_points(trade.booked_prem)
            self._notify(messages.exit_alert(
                option=trade.option, grade="OI-FLIP",
                reason=f"{reason} — booked 50%, trail to {trail:.2f} (structure)",
                pnl_text=f"{blended:+.2f} pts "
                         f"(₹{trade.pnl_rupees(trade.booked_prem):+,.0f})"))
            return

        if code in self.HARD_EXIT_CODES or grade == "OI-STRONG":
            self.close_trade(trade, code, reason, spot=spot, grade=grade)
        elif grade == "OI-MODERATE":
            if trade.runner_armed:
                self.close_trade(trade, code, reason, spot=spot, grade=grade)
            else:
                trade.runner_armed = True
                trade.booked_prem = trade.last_prem or trade.entry_prem
                blended = trade.blended_points(trade.booked_prem)
                self.store.journal_event(
                    "SKIP", trade_id=trade.trade_id,
                    option_ltp=trade.booked_prem,
                    reasons={"event": "graded-exit", "grade": "OI-MODERATE",
                             "code": code},
                    notes=f"{trade.trade_id}: OI-MODERATE on {code} - booked 50%, "
                          f"runner at breakeven")
                log.info("%s graded OI-MODERATE: runner armed at %.2f",
                         trade.trade_id, trade.booked_prem)
                self._notify(messages.exit_alert(
                    option=trade.option, grade="OI-MODERATE",
                    reason=f"{reason} — booked 50%, runner at breakeven",
                    pnl_text=f"{blended:+.2f} pts "
                             f"(₹{trade.pnl_rupees(trade.booked_prem):+,.0f})"))
        else:  # WALL-UNWINDING
            trade.rearm_targets(spot)
            trade.reentry_granted = True
            self.reentry_available[trade.direction] = True
            self.store.journal_event(
                "SKIP", trade_id=trade.trade_id,
                reasons={"event": "graded-exit", "grade": "WALL-UNWINDING",
                         "code": code},
                notes=f"{trade.trade_id}: WALL-UNWINDING on {code} - trigger "
                      f"suppressed, targets re-armed, one re-entry armed")
            log.info("%s graded WALL-UNWINDING: holding, targets re-armed",
                     trade.trade_id)
            mark = trade.last_prem if trade.last_prem is not None \
                else trade.entry_prem
            self._notify(messages.exit_alert(
                option=trade.option, grade="WALL-UNWINDING",
                reason=f"{reason} — trigger suppressed, targets re-armed, one "
                       f"re-entry armed for {trade.direction}",
                pnl_text=f"mark {mark:+.2f} pts"))

    def process_poll(self) -> None:
        chain = self.market.chain
        if chain is None:
            return
        spot = chain.underlying
        now = ist_now()
        for trade in list(self.book.trades):
            trade.update_extreme(spot)
            ltp = self.trade_ltp(trade)
            if ltp is not None:
                if ltp_is_plausible(ltp, spot):
                    trade.last_prem = ltp
                else:
                    log.warning("implausible LTP %.2f for %s at spot %.2f - mark "
                                "kept at %.2f", ltp, trade.option, spot,
                                trade.last_prem or 0.0)
            if not trade.runner_armed and trade.hit_tgt1(spot) \
                    and trade.last_prem is not None:
                trade.runner_armed = True
                trade.booked_prem = trade.last_prem
                structure_level = (self.day_state.last_higher_low
                                   if trade.direction == "LONG"
                                   else self.day_state.last_lower_high) \
                    if self.day_state else None
                if is_mtf_family(trade.family):
                    trade.trail_stop = trade.entry_spot
                    trail = trade.structure_trail(structure_level=structure_level)
                    book_pct = int((trade.booked_fraction or settings.MTF_BOOK_FRACTION) * 100)
                    event = "MTF-TGT1"
                    note = (f"{event} for {trade.trade_id}: BOOK {book_pct}%, "
                            f"runner at entry, structure trail {trail:.2f}")
                else:
                    trail = trade.structure_trail(structure_level=structure_level)
                    event, note = "1R", (f"1R reached for {trade.trade_id}: BOOK 50%, "
                                         f"structure trail {trail:.2f}")
                self.store.journal_event(
                    "SKIP", trade_id=trade.trade_id, option_ltp=trade.booked_prem,
                    day_state=self.day_state.state if self.day_state else None,
                    reasons={"event": event, "booked_prem": trade.booked_prem,
                             "trail": trail},
                    notes=note)
                log.info("%s at spot %.2f (booked %.2f, trail %.2f)",
                         note, spot, trade.booked_prem, trail)
                if not self.event_day:
                    blended = trade.blended_points(trade.last_prem)
                    self._notify(messages.book_alert(
                        option=trade.option, spot=spot,
                        pnl_text=f"{blended:+.2f} pts "
                                 f"(₹{trade.pnl_rupees(trade.last_prem):+,.0f})"))
            decision = self.exit_engine.check(trade, self._entry_ctx(None, spot=spot)
                                              | {"ltp": trade.last_prem, "now": now})
            if decision:
                self._execute_exit(trade, decision, spot=spot)

    # -- 15:10 / 15:40 ------------------------------------------------------------------------
    def square_off_if_due(self) -> None:
        if self.store.has_journal_event(self.today_iso, "square-off check"):
            return
        open_trades = list(self.book.trades)
        for trade in open_trades:
            self.close_trade(trade, "SQUARE_OFF", "forced square-off 15:10 IST")
        if open_trades and not self.event_day:
            self._notify(messages.square_off([
                f"• {t.option} ({t.direction}) — closed at last mark "
                f"₹{fmt_num(t.last_prem)}" for t in open_trades]))
        note = (f"square-off check: {len(open_trades)} open trade(s) force-closed"
                if open_trades else "square-off check: no open paper trades")
        self.store.journal_event("SKIP", notes=note)

    def eod_if_due(self) -> None:
        if self.store.has_journal_event(self.today_iso, "EOD summary"):
            return
        body = stats.build_eod_body(self.store, self.today_iso,
                                    self.regime.summary() if self.regime else None,
                                    self.market.health.quality_lines())
        self.store.journal_event("REGIME",
                                 regime=self.regime.regime.value if self.regime else None,
                                 notes="EOD summary")
        message = messages.eod(body + "\n\nDATA: " + self.market.data_footer())
        safe_print(message)
        if not self.event_day:
            self._notify(message)


def recover_regime(store: JournalStore, today_iso: str) -> RegimeState | None:
    data = store.latest_regime_state(today_iso)
    if not data:
        return None
    try:
        state = RegimeState.from_reasons(data)
        log.info("Regime state recovered from journal: %s", state.summary())
        return state
    except (ValueError, TypeError) as exc:
        log.warning("Could not recover regime state from journal: %s", exc)
        return None


def run_live(store: JournalStore, tg: TelegramSender, nse: NSESession) -> None:
    holidays = load_date_set(settings.HOLIDAYS_FILE)
    events = load_date_set(settings.EVENT_CALENDAR_FILE)
    classifier = RegimeClassifier(holiday_dates=holidays, event_dates=events)
    today = ist_now().date()
    today_iso = today.isoformat()
    event_day = today_iso in events

    if today.weekday() >= 5 or today_iso in holidays:
        log.info("Weekend/holiday (%s) - silent exit(0).", today_iso)
        if today_iso in holidays:
            store.journal_event("SKIP", notes=f"market holiday {today_iso}")
        return
    if ist_now().time() >= settings.MARKET_CLOSE:
        log.info("Started after market close - exporting CSVs and exiting.")
        store.export_day(today_iso)
        return

    market = MarketDataStore(session=nse, journal=store, holidays=holidays)
    session = DaySession(store, tg, classifier, event_day, today_iso,
                         market=market, holidays=holidays)
    session.book.load()
    session.regime = recover_regime(store, today_iso)

    # --- 08:45 pre-market: the sanctioned source of PDC/PDH/PDL (stored once) ---
    wait_until(settings.PREMARKET_FETCH_TIME)
    if not store.has_journal_event(today_iso, "pre-market factor report"):
        try:
            pre = premarket_report.run(store=store, tg=tg, nse=nse)
            market.set_prev_day(pre.get("pdh"), pre.get("pdl"), pre["pdc"])
            session.us_direction = pre["us_direction"]
            for source, ok in pre.get("sources", {}).items():
                market.health.record(source, ok)
        except Exception:
            log.exception("Pre-market report failed; factors may be MISSING at 09:15")

    wait_until(settings.PREOPEN_START)
    nse.warm_up()

    # Candle backfill (mid-day start): full day immediately, opening print stored
    try:
        fresh = market.candle_engine.refresh()
        session.opening_spot = market.candle_engine.opening_spot()
        if fresh:
            log.info("Candle backfill: %d closed candles, opening spot %s",
                     len(fresh), fmt_num(session.opening_spot))
    except Exception:
        log.exception("Candle backfill failed (spot-sampler fallback stays active)")

    # Startup guarantee (spec #5): PDC or NO-TRADE DAY - a silent bot beats a wrong bot
    if not market.ensure_prev_day(retries=3):
        log.critical("PDC unavailable after 3 startup retries - NO-TRADE DAY, "
                     "standing down silently")
        store.journal_event("SKIP", notes="NO-TRADE DAY: PDC unavailable after 3 "
                                          "startup retries")
        return

    last_cycle = None
    while True:
        now = ist_now()
        if now.time() >= settings.MARKET_CLOSE:
            break
        due = last_cycle is None or \
            (time.monotonic() - last_cycle) >= CYCLE_INTERVAL_SEC
        if now.time() >= settings.MARKET_OPEN and due:
            try:
                session.run_cycle()
            except Exception:
                log.exception("Cycle failed (system keeps running)")
            last_cycle = time.monotonic()
        if now.time() >= settings.SQUARE_OFF_TIME:
            session.square_off_if_due()
        time.sleep(LOOP_SLEEP_SEC)

    session.square_off_if_due()
    wait_until(settings.EOD_SUMMARY_TIME)
    session.eod_if_due()
    wait_until(settings.SHUTDOWN_TIME)
    shutdown_clean(store, tg, nse,
                   f"clean exit {today_iso} 15:45 IST; next run next trading day 08:30")


def shutdown_clean(store: JournalStore, tg: TelegramSender, nse: NSESession,
                   reason: str) -> None:
    store.journal_event("REGIME", notes=f"clean shutdown: {reason}")
    tg.send(messages.shutdown(reason))
    store.flush()
    nse.close()
    store.close()
    sys.exit(0)


def run_live_with_watchdog(store: JournalStore, tg: TelegramSender,
                           nse: NSESession) -> None:
    restarts = 0
    while True:
        try:
            run_live(store, tg, nse)
            return
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            restarts += 1
            log.critical("Day loop crashed (restart %d of %d)", restarts,
                         settings.WATCHDOG_RESTART_LIMIT, exc_info=True)
            short = f"{type(exc).__name__}: {exc}"[:200]
            if restarts > settings.WATCHDOG_RESTART_LIMIT:
                tg.send(messages.shutdown(
                    f"{settings.WATCHDOG_RESTART_LIMIT} restarts exhausted today; "
                    f"last error: {short}. Check the log file - bot is down for today."))
                store.flush()
                nse.close()
                store.close()
                sys.exit(1)
            tg.send(messages.error(short, restarts, settings.WATCHDOG_RESTART_LIMIT))
            time.sleep(settings.WATCHDOG_RESTART_WAIT_SEC)


def run_now(store: JournalStore, tg: TelegramSender, nse: NSESession) -> None:
    setup_logging()
    events = load_date_set(settings.EVENT_CALENDAR_FILE)
    holidays = load_date_set(settings.HOLIDAYS_FILE)
    classifier = RegimeClassifier(event_dates=events)
    today_iso = ist_now().date().isoformat()
    event_day = today_iso in events
    market = MarketDataStore(session=nse, journal=store, holidays=holidays)
    session = DaySession(store, tg, classifier, event_day, today_iso,
                         market=market, holidays=holidays)

    log.info("--mode now: test run starting")
    if settings.MARKET_OPEN <= ist_now().time() <= settings.MARKET_CLOSE:
        warning = messages.test_mode_warning()
        safe_print(warning)
        session._notify(warning)
    pre = premarket_report.run(store=store, tg=tg, nse=nse)
    market.set_prev_day(pre.get("pdh"), pre.get("pdl"), pre["pdc"])
    session.us_direction = pre["us_direction"]
    for source, ok in pre.get("sources", {}).items():
        market.health.record(source, ok)

    nse.warm_up()
    session.run_cycle()
    try:
        fresh = market.candle_engine.refresh()
        session.opening_spot = market.candle_engine.opening_spot()
        if fresh:
            session.process_candle_close(fresh[-1])
    except Exception:
        log.exception("Candle refresh failed in --mode now")
    session.square_off_if_due()
    session.eod_if_due()
    shutdown_clean(store, tg, nse, "test run complete (--mode now)")


def run_chain_test() -> None:
    setup_logging()
    nse = NSESession()
    print("1) Handshake: GET https://www.nseindia.com/option-chain "
          "(full browser headers, cookies kept in session)")
    print(f"   cookies obtained: {nse.warm_up()}")

    def show(url: str) -> None:
        debug = nse.get_debug(url)
        print("=" * 72)
        print(f"URL requested : {debug['url']}")
        print(f"HTTP status   : {debug['status'] if debug['status'] is not None else 'no response (request error)'}")
        print("Headers sent  :")
        print(debug["request_headers"])
        print(f"Body ({debug['length']} bytes), first 500 characters:")
        print(debug["snippet"])

    print("2) Probing the chain flow, step by step:")
    info = nse.get_json_once(_contract_info_url())
    print(f"   contract-info: HTTP {nse.last_status}")
    expiries = info.get("expiryDates") if isinstance(info, dict) else None
    head = (expiries or [])[:settings.NSE_CHAIN_MAX_EXPIRIES]
    print(f"   expiryDates: {head}"
          f"{' ...' if expiries and len(expiries) > len(head) else ''}")
    if expiries:
        show(_v3_url(expiries[0]))
        probe = nse.get_json_once(_v3_url(expiries[0]))
        value = probe.get("records", {}).get("underlyingValue") \
            if isinstance(probe, dict) else None
        print(f"   underlyingValue from response: {value}  "
              f"(must be ~22,600 for NIFTY - anything else = wrong data)")
    for url in settings.NSE_CHAIN_LEGACY_ENDPOINTS:
        show(url)
    if settings.NSE_CHAIN_ENDPOINT:
        show(settings.NSE_CHAIN_ENDPOINT)
    print("=" * 72)
    print("Working contract as of Oct 2026: /api/option-chain-contract-info ->")
    print("/api/option-chain-v3?...&expiry=<date> (a bare v3 call returns {}).")
    nse.close()


def run_score() -> None:
    """--mode score: ONE consistent set from the MarketDataStore - spot, PDC, ATM
    strike, ATM CE/PE LTP, VIX, chain source, store timestamp."""
    setup_logging()
    nse = NSESession()
    market = MarketDataStore(session=nse, journal=None,
                             holidays=load_date_set(settings.HOLIDAYS_FILE))
    print("=" * 72)
    print(" SCORE MODE - one MarketDataStore pass, one consistent data set")
    print("=" * 72)
    print(f"now: {ist_now().strftime('%a %Y-%m-%d %H:%M:%S')} IST")
    market.ensure_prev_day(retries=3)
    market.refresh()

    spot = market.spot_value()
    atm = round_to_strike(spot) if spot is not None else None
    print(f"1) STORE REFRESH @ {market.last_refresh} | "
          f"chain_source={market.chain_source} | candles={len(market.candles)}")
    print(f"2) SPOT {fmt_num(spot)} | PDC {fmt_num(market.pdc)} | "
          f"PDH {fmt_num(market.pdh)} | PDL {fmt_num(market.pdl)}")
    print(f"3) ATM STRIKE {atm} "
          f"(multiple of 50: {atm % settings.STRIKE_ROUND_TO == 0 if atm else '-'}) | "
          f"within 1% of spot: "
          f"{atm is not None and abs(atm - spot) <= spot * settings.ATM_BAND_PCT}")
    if market.chain and atm is not None:
        row = next((r for r in market.chain.rows
                    if r.strike == atm and r.expiry == market.trade_expiry), None)
        if row:
            print(f"4) ATM CE LTP Rs {fmt_num(row.ce_ltp)} | ATM PE LTP "
                  f"Rs {fmt_num(row.pe_ltp)} ({market.trade_expiry}) | "
                  f"plausible: {ltp_is_plausible(row.ce_ltp, spot)} / "
                  f"{ltp_is_plausible(row.pe_ltp, spot)}")
    print(f"5) VIX {fmt_num(market.vix.value.get('value')) if market.vix else 'MISSING'} "
          f"(prev close "
          f"{fmt_num(market.vix.value.get('prev_close')) if market.vix else '-'}) "
          f"source={market.vix.source if market.vix else '-'}")
    print(f"6) CHAIN SOURCE: {market.chain_source}")
    for label, code in market.chain_tried:
        print(f"    {code}  {label}")
    print(f"7) DATA: live-chain | {market.data_footer()}")
    print("=" * 72)
    nse.close()


def main():
    parser = argparse.ArgumentParser(
        description="Nifty options alert-only assistant (alert/journal only, no orders).")
    parser.add_argument("--mode",
                        choices=["live", "now", "backtest", "replay-mtf",
                                 "chain-test", "score"],
                        default="live",
                        help="live = scheduled day (default); now = immediate test "
                             "run; backtest = arrives with Phase C; replay-mtf = "
                             "historical MTF verification; chain-test = per-step "
                             "chain diagnostics; score = one consistent store snapshot")
    parser.add_argument("--days", type=int, default=5,
                        help="replay-mtf: number of recent sessions to replay")
    args = parser.parse_args()
    setup_logging()

    if args.mode == "replay-mtf":
        run_replay_mtf(days=args.days)
        return
    if args.mode == "backtest":
        log.info("--mode backtest requested: the replay backtester is Phase C "
                 "(waiting for 'proceed'). Nothing to run yet.")
        safe_print("--mode backtest: the replay backtester arrives with Phase C. "
                   "Data collection runs via --mode live.")
        return
    if args.mode == "chain-test":
        run_chain_test()
        return
    if args.mode == "score":
        run_score()
        return

    store = JournalStore()
    tg = TelegramSender()
    nse = NSESession()
    if args.mode == "now":
        run_now(store, tg, nse)
    else:
        run_live_with_watchdog(store, tg, nse)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
        sys.exit(130)
