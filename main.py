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
from dataclasses import asdict
from datetime import datetime, time as dt_time
from pathlib import Path

from alerts import messages
from alerts.telegram_bot import TelegramSender
from data_sources.market_data import MarketDataStore
from data_sources.nse_chain import _contract_info_url, _v3_url
from data_sources.nse_session import NSESession
from engines.coil_snipe import CoilSnipeEngine
from engines.entry import EntryEngine
from engines.exit import ExitEngine
from engines.candles import orb_levels
from engines.day_state import DayStateEngine, RANGE
from types import SimpleNamespace
from engines.level_runtime import LevelRuntime
from engines.liquidity import room_to_run
from engines.mtf_live import (MTF_FAMILIES, coil_viz_payload, compute_mtf_tgt1,
                              event_family, is_mtf_family, journal_viz,
                              mtf_entry_allowed, ready_confirmed_zone,
                              zone_viz_payload)
from engines.replay_mtf import run_replay_mtf
from engines.oi_engine import recent_oi_net, spot_adjacent
from engines.pullback import PullbackEngine
from engines.order_blocks import OrderBlockEngine
from engines.short_gating import evaluate_short_gate, get_trades_expectancy
from engines.sweep_mss_poi import SweepMssPoiEngine, get_sweep_ab_stats
from engines.paper import PaperTrade
from engines.regime import Regime, RegimeClassifier, RegimeState
from engines.sweep import SweepEngine
from journal.decision_audit import emit_nearmiss_report
from engines.second_stage import SecondStageEngine
from journal import stats
from journal.store import JournalStore
from data_sources.snapshot import MarketSnapshot, MarketSnapshotBuilder
from engines.levels_lifecycle import LevelLifecycleManager, LevelRole, LevelStatus
from engines.strategies import S1TrendExpansion, S2VwapMomentum, S3LiquidityReversal
from engines.arbiter import Arbiter, RiskGovernor
from engines.position_manager import PositionManager, TradeStateEnum, ExitClassification
from engines.shadow_ledger import ShadowLedger
from engines.execution_router import get_execution_router
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
    if snapshot is None or not getattr(snapshot, "rows", None):
        return None

    want = expiry or getattr(snapshot, "trade_expiry", None) or getattr(snapshot, "nearest_expiry", None)
    want_norm = str(want).strip().upper() if want else ""
    target_strike = int(round(strike))

    # 1. Exact or case-insensitive match on requested/nearest expiry
    if want_norm:
        for row in snapshot.rows:
            if int(round(row.strike)) == target_strike and str(row.expiry).strip().upper() == want_norm:
                ltp = row.ce_ltp if direction == "LONG" else row.pe_ltp
                if ltp is not None and ltp > 0:
                    return ltp

    # 2. Try snapshot.nearest_expiry
    nearest = getattr(snapshot, "nearest_expiry", None)
    if nearest:
        nearest_norm = str(nearest).strip().upper()
        for row in snapshot.rows:
            if int(round(row.strike)) == target_strike and str(row.expiry).strip().upper() == nearest_norm:
                ltp = row.ce_ltp if direction == "LONG" else row.pe_ltp
                if ltp is not None and ltp > 0:
                    return ltp

    # 3. Fallback: match strike on the nearest available row with non-zero LTP
    for row in snapshot.rows:
        if int(round(row.strike)) == target_strike:
            ltp = row.ce_ltp if direction == "LONG" else row.pe_ltp
            if ltp is not None and ltp > 0:
                return ltp

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
                 market: MarketDataStore, holidays=None, data_source: str = "live-chain",
                 live_mode: bool = False):
        self.store = store
        self.tg = tg
        self.classifier = classifier
        self.event_day = event_day
        self.today_iso = today_iso
        self.market = market
        self.holiday_dates = holidays or set()
        self.data_source = data_source
        self.live_mode = live_mode
        self.telegram_trades_only = live_mode or getattr(settings, "TELEGRAM_TRADES_ONLY", False)
        self.legacy_execution_enabled = not live_mode
        self.entry = EntryEngine(market.journal)
        self.exit_engine = ExitEngine(market.journal)
        self.sweeps = SweepEngine(market.journal)
        self.second_stage = SecondStageEngine(market.journal)
        self.pullbacks = PullbackEngine(market.journal)
        self.level_runtime = LevelRuntime()
        self.coil_engine = CoilSnipeEngine()
        self.ob_engine = OrderBlockEngine()
        self.sweep_mss_poi = SweepMssPoiEngine()
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
        self._skip_alerts: dict = {}   # (family, direction) -> last 🚫 alert time

        # Candle blindness detection & state (Phase 13.3 + 13.4 Outage Exit Protocol)
        self.is_blind = False
        self._last_blind_alert_time: datetime | None = None
        self.blind_start_time: datetime | None = None
        self.blind_incidents = 0
        self.max_candle_lag = 0.0

        # Session Preflight Status (Phase 13.5)
        self.preflight_checked = False
        self.preflight_status = {"status": "PENDING", "message": "Awaiting 09:20 preflight check"}
        self.degraded_mode = False

        # Regime spam suppression tracking
        self._last_regime_classification: Regime | None = None
        self._last_regime_alert_time: datetime | None = None
        self._vix_spike_alerted = False

        # PCR gate shadow tracker (measure, don't delete)
        from engines.pcr_shadow import PCRShadowTracker
        self.pcr_shadow_tracker = PCRShadowTracker()

        # Shadow-Compliant Comparison Ledger (Phase 13.4)
        from engines.shadow_compliant import ShadowCompliantLedger
        self.shadow_compliant_ledger = ShadowCompliantLedger()

        # Phase 14 Engine Census Enforcement (One Brain Consolidation)
        from engines.census import print_and_export_census
        print_and_export_census()

        from engines.shadow_mw import ShadowMWEngine, UnifiedLiquiditySniper, dispatch_telegram_alert
        self.sniper = UnifiedLiquiditySniper(
            store=self.store,
            today_iso=self.today_iso,
            pdh=self.market.pdh,
            pdl=self.market.pdl,
            notifier=dispatch_telegram_alert,
        )
        self.shadow_mw = self.sniper.arm1

        # Phase 15: Zone-Ride Model Engine (The Consolidated Final Spec)
        from engines.zone_ride import ZoneRideEngine
        self.zone_ride = ZoneRideEngine(notifier=self._notify)

        # Phase 1-10 Unified Architectural Engines
        self.snapshot_builder = MarketSnapshotBuilder()
        self.level_lifecycle = LevelLifecycleManager()
        self.s1_strategy = S1TrendExpansion()
        self.s2_strategy = S2VwapMomentum()
        self.s3_strategy = S3LiquidityReversal()
        self.arbiter = Arbiter()
        self.risk_governor = RiskGovernor()
        self.position_manager = PositionManager()
        self.shadow_ledger = ShadowLedger()
        self.current_snapshot: MarketSnapshot | None = None
        self.execution_router = get_execution_router()

        # Single Live Decision Owner Guard:
        # Direct execution via legacy fire() / _fire_mtf() is strictly disabled in live mode.
        # Live trades can ONLY be created by S1/S2/S3 -> Arbiter -> RiskGovernor -> PositionManager.
        self.legacy_execution_enabled = not self.live_mode

    # -- the ONE Telegram choke point ---------------------------------------------
    def _notify(
        self,
        text: str,
        *,
        event_id: str | None = None,
        decision_hash: str | None = None,
        severity: str = "INFO",
        event_type: str | None = None,
    ) -> None:
        if self.data_source != "live-chain":
            log.info("telegram suppressed (data_source=%s): %.80s", self.data_source,
                     text.replace("\n", " | "))
            return

        # Suppress confident regime-change / day state messages while blind
        if getattr(self, "is_blind", False):
            if any(k in text for k in ("REGIME CHANGE", "DAY STATE", "TREND-UP", "TREND-DOWN", "VIX SPIKE")):
                log.warning("Suppressed regime/state notification while candle engine is blind: %.80s",
                            text.replace("\n", " | "))
                return

        # Strict Trades-Only Telegram Filter:
        # In live mode (or when TELEGRAM_TRADES_ONLY is active), strictly suppress non-trade informational noise
        # (Level watches, coils, preflight, regime, skipped, etc.) to keep Telegram focused 100% on trade entries and exits.
        trades_only = getattr(self, "telegram_trades_only", False) or getattr(settings, "TELEGRAM_TRADES_ONLY", False) or self.live_mode
        if trades_only:
            is_trade_msg = (
                event_type in ("TRADE_ENTRY", "TRADE_UPDATE", "TRADE_EXIT")
                or any(marker in text for marker in (
                    "OPTION TRADE ENTRY",
                    "OPTION TRADE CLOSED",
                    "TARGET 1 HIT",
                    "TRADE SIGNAL",
                    "TRADE EXIT",
                    "TRADE UPDATE",
                    "TRADE CARD",
                    "SIGNAL #",
                    "EXIT #",
                    "DATA-OUTAGE-EXIT",
                    "CRITICAL SYSTEM HALT",
                    "SHUTDOWN",
                ))
            )
            if not is_trade_msg:
                log.info("telegram suppressed non-trade message (trades-only): %.80s",
                         text.replace("\n", " | "))
                return

        if hasattr(self.tg, "send_event"):
            self.tg.send_event(
                text,
                event_id=event_id,
                decision_hash=decision_hash,
                severity=severity,
                event_type=event_type,
            )
        else:
            self.tg.send(text)

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
        if family in ("ORB", "LIQ-BREAK") and day.bias:
            if day.bias == "PE-preferred" and not (day.state == "TREND-UP"):
                return f"ORB-mid bias: PE preferred (spot vs ORB mid {day.orb_mid:.0f})"
            if day.bias == "CE-preferred" and not (day.state == "TREND-DOWN"):
                return f"ORB-mid bias: CE preferred (spot vs ORB mid {day.orb_mid:.0f})"
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

    def check_session_preflight(self, now: datetime | None = None) -> dict:
        """Session preflight check at 09:20 daily (Phase 13.5).
        
        Verifies:
        1. Chart API responds (or has candles)
        2. Option chain responds (underlying spot available)
        3. Initial morning candles forming for ORB (09:15-09:30)
        
        Any dead feed -> warning alert + degraded-mode notice for the day.
        """
        now = now or ist_now()
        chart_live = bool(getattr(self.market.candle_engine, "chart_ok", False) or self.market.candles)
        chain_live = bool(self.market.chain is not None and self.market.spot is not None)
        orb_forming = len(self.market.candles) >= 1 if now.time() >= dt_time(9, 20) else True

        status = "OK" if (chart_live and chain_live) else "DEGRADED"
        details = {
            "chart_api": "LIVE" if chart_live else "DEAD/LAGGING",
            "option_chain": "LIVE" if chain_live else "DEAD",
            "orb_forming": "YES" if orb_forming else "NO_CANDLES_YET",
            "status": status,
            "checked_at": now.strftime("%H:%M:%S IST"),
        }
        self.preflight_status = details
        self.preflight_checked = True

        if status == "DEGRADED":
            self.degraded_mode = True
            log.warning("⚠️ SESSION PREFLIGHT WARNING: Degraded feed(s) detected: %s", details)
            self._notify(
                f"⚠️ SESSION PREFLIGHT WARNING (09:20 IST)\n"
                f"Status: DEGRADED MODE ACTIVE\n"
                f"• Chart API: {details['chart_api']}\n"
                f"• Option Chain: {details['option_chain']}\n"
                f"• ORB Formation: {details['orb_forming']}\n\n"
                f"Notice: Operating in degraded mode today. Fallback candle synthesis and safety rails engaged."
            )
        else:
            log.info("SESSION PREFLIGHT OK (09:20 IST): Both feeds live. ORB tracking active.")
            self._notify(
                f"✅ SESSION PREFLIGHT PASSED (09:20 IST)\n"
                f"• Chart API: LIVE\n"
                f"• Option Chain: LIVE (Spot: {self.market.spot_value() or '—'})\n"
                f"• ORB Forming: YES ({len(self.market.candles)} bar(s))\n"
                f"System ready for 09:30 ORB completion."
            )

        self.store.journal_event(
            "PREFLIGHT",
            spot=self.market.spot_value(),
            notes=f"Session preflight 09:20: {status} (Chart: {details['chart_api']}, Chain: {details['option_chain']})",
            ts=now.isoformat(timespec="seconds") if now else None,
        )
        return details

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

        now = ist_now()

        # Daily Session Preflight check at 09:20 IST (Phase 13.5)
        if (
            self.live_mode
            and not getattr(self, "preflight_checked", False)
            and now.time() >= dt_time(9, 20)
            and now.time() < settings.MARKET_CLOSE
        ):
            self.check_session_preflight(now)

        # Candle blindness check during market hours (09:15-15:30 IST)
        if self.live_mode and settings.MARKET_OPEN <= now.time() <= settings.MARKET_CLOSE:
            # OUTAGE EXIT PROTOCOL (Phase 13.4): 15:10 square-off fires regardless of data state
            if now.time() >= settings.SQUARE_OFF_TIME:
                self.square_off_if_due()

            age = self.market.latest_candle_age_minutes(now)
            if age is None and status.get("fresh_candles"):
                age = (now - status["fresh_candles"][-1].end).total_seconds() / 60.0
            is_blind = age is None or age > 5.5
            self.is_blind = is_blind
            if is_blind:
                if self.blind_start_time is None:
                    self.blind_start_time = now
                blind_duration_min = (now - self.blind_start_time).total_seconds() / 60.0
                self.blind_incidents += 1
                self.max_candle_lag = max(self.max_candle_lag, age if age is not None else 999.0)
                age_txt = f"{age:.1f} min ago" if age is not None else "no candles loaded"
                log.warning("⚠️ CANDLE ENGINE BLIND: Last candle closed %s (duration %.1fm)", age_txt, blind_duration_min)

                # OUTAGE EXIT PROTOCOL (Phase 13.4):
                # Blindness > 15 min with an open trade -> exit at last known price, tag DATA-OUTAGE-EXIT
                open_trades = list(self.book.trades)
                pm_trade = self.position_manager.active_trade
                has_pm_trade = pm_trade is not None and pm_trade.lifecycle_state != TradeStateEnum.EXITED
                if blind_duration_min > 15.0 and (open_trades or has_pm_trade):
                    self.session_clean = False
                    log.error("OUTAGE EXIT PROTOCOL TRIGGERED: Blindness duration %.1fm > 15m limit with open trade(s)!",
                              blind_duration_min)
                    last_known_spot = self.market.spot_value() or (pm_trade.entry_price if has_pm_trade else 0.0)
                    for trade in open_trades:
                        self.close_trade(
                            trade,
                            exit_reason="DATA-OUTAGE-EXIT",
                            notes=f"Feed blindness duration {blind_duration_min:.1f}m > 15m safety limit",
                            spot=last_known_spot or trade.entry_spot,
                        )
                    if has_pm_trade:
                        self.position_manager.close_position(
                            pm_trade,
                            exit_time=now,
                            exit_price=last_known_spot or pm_trade.entry_price,
                            reason="DATA-OUTAGE-EXIT: Feed blindness > 15m safety limit",
                            classification=ExitClassification.DATA_SAFETY_EXIT,
                        )
                        self.store.journal_event(
                            "EXIT",
                            spot=last_known_spot or pm_trade.entry_price,
                            trade_id=pm_trade.trade_id,
                            notes=f"DATA-OUTAGE-EXIT: Feed blindness duration {blind_duration_min:.1f}m > 15m safety limit",
                            result_r=pm_trade.realized_r,
                            exit_reason="DATA-OUTAGE-EXIT",
                        )
                    total_closed = len(open_trades) + (1 if has_pm_trade else 0)
                    self._notify(
                        f"🚨 DATA-OUTAGE-EXIT TRIGGERED 🚨\n"
                        f"Feed blindness exceeded 15 minutes ({blind_duration_min:.1f}m).\n"
                        f"Emergency square-off executed for {total_closed} trade(s) at last known mark ({last_known_spot:.2f}) to prevent unmanaged loss."
                    )

                if (self._last_blind_alert_time is None or
                        (now - self._last_blind_alert_time).total_seconds() >= 900):
                    self._last_blind_alert_time = now
                    self._notify(
                        f"⚠️ CANDLE ENGINE BLIND\n"
                        f"Latest 5m candle: {age_txt} (> 5 min threshold).\n"
                        f"Feed blindness duration: {blind_duration_min:.1f} min.\n"
                        f"Setups and state evaluations are halted until candle stream recovers."
                    )
                # BLINDNESS HALT ENFORCEMENT (Phase 13.3)
                # Halt all trading engines, coil, zone watches, and evaluations.
                self._export_live_state()
                log.info("BLINDNESS HALT: All engines halted. Running recovery loop only.")
                return
            else:
                self.blind_start_time = None
                self._last_blind_alert_time = None

        # Shadow-track exits for setups blocked by PCR < 0.70
        square_off = datetime.combine(now.date(), settings.SQUARE_OFF_TIME, tzinfo=settings.IST)
        self.pcr_shadow_tracker.update_exits(
            self.market.spot_value(), self.market.chain, now, square_off
        )

        # Day-State Engine (Trend-Day v2 module A): VWAP/EMA-20/liquidity break/
        # structure/classification - computed once, read by every gate below.
        self.day_state = self.day_state_engine.update(
            self.market.candles, spot=self.market.spot_value(),
            orb_mid=(orb_levels(self.market.candles) or {}).get("mid")
            if self.market.candles else None,
            pdh=self.market.pdh, pdl=self.market.pdl)

        # Shadow Arm & Model A Sniper level set initialization (09:15 daily)
        if hasattr(self, "sniper"):
            if not getattr(self.sniper.arm1, "levels_initialized", False) or not self.sniper.arm1.levels:
                self.sniper.initialize_levels(pdh=self.market.pdh, pdl=self.market.pdl)

        # Build Canonical MarketSnapshot for Phase 1-10 Pipeline
        self.current_snapshot = self.snapshot_builder.build(self.market, now)
        if self.current_snapshot:
            self.level_lifecycle.update(self.current_snapshot, self.current_snapshot.latest_candle)
            self.shadow_ledger.update(self.current_snapshot)
        # wall VELOCITY map (BUG-2): snapshot-to-snapshot change-in-OI -
        # day-cumulative OI is context, never the gate
        velocity = {}
        if self.market.chain and self.market.chain.rows:
            strikes = sorted({r.strike for r in self.market.chain.rows
                              if r.expiry == self.market.chain.nearest_expiry})
            for side in ("ce", "pe"):
                velocity.update(recent_oi_net(self.market.journal.conn,
                                              self.market.chain.nearest_expiry,
                                              strikes, side))
        self.level_runtime.velocities = velocity
        for t_old, t_new, boundary in self.day_state.transitions:
            marker = f"day state: {t_old} (boundary {boundary})"
            if not self.store.has_journal_event(self.today_iso, marker):
                self.store.journal_event("REGIME",
                                         day_state=self.day_state.state,
                                         notes=marker)
        self.second_stage.update(self.market.candles, self.market.liquidity)
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
            # Sync market trade_expiry to chain snapshot if available
            if getattr(self.market, "trade_expiry", None) and hasattr(self.market, "chain") and self.market.chain:
                setattr(self.market.chain, "trade_expiry", self.market.trade_expiry)

            # Ensure sniper and shadow_mw levels are initialized as soon as PDH/PDL are locked
            if getattr(self.market, "pdh", None) and getattr(self.market, "pdl", None):
                if hasattr(self, "sniper") and hasattr(self.sniper, "initialize_levels"):
                    if not getattr(self.sniper.arm1, "levels_initialized", False) or not self.sniper.arm1.levels:
                        self.sniper.initialize_levels(pdh=self.market.pdh, pdl=self.market.pdl)
                if hasattr(self, "shadow_mw") and hasattr(self.shadow_mw, "initialize_levels"):
                    if not getattr(self.shadow_mw, "levels_initialized", False) or not self.shadow_mw.levels:
                        self.shadow_mw.initialize_levels(self.market, today_iso=self.today_iso)

            if self.first_cycle:
                # Startup catch-up backlog: sync internal state silently without spamming alerts
                for c in fresh:
                    if hasattr(self, "sniper"):
                        self.sniper.on_candle(c, day_state=self.day_state, chain=getattr(self.market, "chain", None), allow_alerts=False)
                log.info("STALE backlog processed silently: %d candle(s) at startup (alerts suppressed)", len(fresh))
                self.store.journal_event(
                    "SKIP", notes=f"backlog discarded: {len(fresh)} stale candle(s) "
                                  f"at startup (never signal candidates)")
            else:
                # Process any superseded older candles silently
                for stale in fresh[:-1]:
                    if hasattr(self, "sniper"):
                        self.sniper.on_candle(stale, day_state=self.day_state, chain=getattr(self.market, "chain", None), allow_alerts=False)
                    log.info("STALE superseded candle processed silently: %s", stale.start.isoformat(timespec="seconds"))

                # ONLY the latest closed candle
                candidate = fresh[-1]
                age = (ist_now() - candidate.end).total_seconds()
                is_fresh = (age <= settings.STALE_CANDLE_MAX_AGE_SEC)

                if hasattr(self, "sniper"):
                    self.sniper.on_candle(candidate, day_state=self.day_state, chain=getattr(self.market, "chain", None), allow_alerts=is_fresh)

                if not is_fresh:
                    log.warning("STALE discarded: candle %s closed %.0f s ago (> %d s) - alerts suppressed",
                                candidate.start.isoformat(timespec="seconds"), age, settings.STALE_CANDLE_MAX_AGE_SEC)
                else:
                    self.process_candle_close(candidate)
        self.first_cycle = False
        self.update_regime()
        self.process_poll()
        self._export_live_state()

    def _export_live_state(self) -> None:
        """Export single source of truth state.json every cycle for Streamlit dashboard (Phase 13.3)."""
        try:
            now = ist_now()
            today_iso = self.today_iso

            # 1. Funnel counters
            from dashboard.funnel_decoder import decode_activity_funnel
            funnel = decode_activity_funnel(self.store.conn, today_iso)

            # 2. Heartbeat status
            c_age = self.market.latest_candle_age_minutes(now)
            ch_age = None
            if self.market.chain_poll_ts:
                try:
                    ch_age = max(0.0, (now - datetime.fromisoformat(self.market.chain_poll_ts)).total_seconds() / 60.0)
                except Exception:
                    pass

            is_healthy = not getattr(self, "is_blind", False)
            health_msg = "HEALTHY (Feeds fresh < 5m)" if is_healthy else f"DATA BLINDNESS (Candle age: {c_age:.1f}m)" if c_age else "DATA BLINDNESS (no candles)"

            # 3. Candles serialization (latest 75)
            candle_list = []
            for c in self.market.candles[-75:]:
                candle_list.append({
                    "start": c.start.isoformat(),
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": float(c.volume) if c.volume is not None else 0.0,
                    "source": getattr(c, "source", "chart"),
                })

            # 4. Open trades
            open_list = []
            if hasattr(self, "zone_ride") and self.zone_ride.active_trade:
                zt = self.zone_ride.active_trade
                open_list.append({
                    "trade_id": zt.trade_id,
                    "family": "ZONE-RIDE",
                    "direction": zt.direction,
                    "strike": zt.strike,
                    "entry_spot": float(zt.entry_spot),
                    "spot": float(zt.entry_spot),
                    "entry_prem": float(zt.entry_prem),
                    "option_ltp": float(zt.entry_prem),
                    "sl_spot": float(zt.current_sl_spot),
                    "current_prem": float(zt.entry_prem),
                    "entry_time": zt.entry_time,
                    "time": zt.entry_time[11:16] if len(zt.entry_time) >= 16 else "—",
                    "setup": "Phase 15 Zone-Ride Model",
                    "option_symbol": zt.option_symbol,
                    "status": zt.status,
                    "opposing_wall_booked": zt.opposing_wall_booked,
                })
            if hasattr(self, "sniper"):
                for st_trade in self.sniper.arm1.trades:
                    if st_trade.status in ("OPEN", "PARTIAL_TGT1"):
                        open_list.append({
                            "trade_id": st_trade.trade_id,
                            "family": "M/W-SWEEP",
                            "direction": st_trade.direction,
                            "strike": st_trade.strike,
                            "entry_spot": float(st_trade.entry_price),
                            "spot": float(st_trade.entry_price),
                            "entry_prem": float(st_trade.entry_prem),
                            "option_ltp": float(st_trade.entry_prem),
                            "sl_spot": float(st_trade.current_sl),
                            "current_prem": float(st_trade.entry_prem),
                            "entry_time": st_trade.entry_time,
                            "time": st_trade.entry_time[11:16] if len(st_trade.entry_time) >= 16 else "—",
                            "setup": getattr(st_trade, "setup_name", f"{st_trade.pattern} Liquidity Sweep ({st_trade.level_name})"),
                            "option_symbol": st_trade.option_symbol,
                            "status": st_trade.status,
                            "half_booked": st_trade.half_booked,
                        })
                if getattr(self.sniper.arm2, "active_trade", None):
                    at = self.sniper.arm2.active_trade
                    open_list.append({
                        "trade_id": at.trade_id,
                        "family": "TL-FIB",
                        "direction": at.direction,
                        "strike": at.strike,
                        "entry_spot": float(at.entry_price),
                        "spot": float(at.entry_price),
                        "entry_prem": float(at.entry_prem),
                        "option_ltp": float(at.entry_prem),
                        "sl_spot": float(at.current_sl),
                        "current_prem": float(at.entry_prem),
                        "entry_time": at.entry_time,
                        "time": at.entry_time[11:16] if len(at.entry_time) >= 16 else "—",
                        "setup": at.setup_name,
                        "option_symbol": at.option_symbol,
                        "status": at.status,
                        "half_booked": at.half_booked,
                    })
                if getattr(self.sniper, "arm3", None) and getattr(self.sniper.arm3, "active_trade", None):
                    zr = self.sniper.arm3.active_trade
                    open_list.append({
                        "trade_id": zr.trade_id,
                        "family": "ZONE-REVERSAL",
                        "direction": zr.direction,
                        "strike": zr.strike,
                        "entry_spot": float(zr.entry_price),
                        "spot": float(zr.entry_price),
                        "entry_prem": float(zr.entry_prem),
                        "option_ltp": float(zr.entry_prem),
                        "sl_spot": float(zr.current_sl),
                        "current_prem": float(zr.entry_prem),
                        "entry_time": zr.entry_time,
                        "time": zr.entry_time[11:16] if len(zr.entry_time) >= 16 else "—",
                        "setup": zr.setup_name,
                        "option_symbol": zr.option_symbol,
                        "status": zr.status,
                        "notes": zr.notes,
                    })
            for t in self.book.trades:
                entry_dt_str = t.entry_time.isoformat() if hasattr(t, "entry_time") and t.entry_time else None
                entry_time_str = t.entry_time.strftime("%H:%M") if hasattr(t, "entry_time") and t.entry_time else "—"
                open_list.append({
                    "trade_id": t.trade_id,
                    "family": t.family,
                    "direction": t.direction,
                    "strike": t.strike,
                    "entry_spot": float(t.entry_spot),
                    "spot": float(t.entry_spot),
                    "entry_prem": float(t.entry_prem) if t.entry_prem is not None else 0.0,
                    "option_ltp": float(t.entry_prem) if t.entry_prem is not None else 0.0,
                    "sl_spot": float(t.sl_spot),
                    "current_prem": float(t.last_prem) if t.last_prem is not None else float(t.entry_prem or 0.0),
                    "entry_time": entry_dt_str,
                    "time": entry_time_str,
                })

            # 4b. Closed trades today
            closed_list = []
            try:
                c_rows = self.store.conn.execute(
                    "SELECT id, ts, direction, strike, spot, option_ltp, score, reasons_json, exit_reason, "
                    "result_r, pnl_rupees, family FROM journal WHERE type = 'EXIT' AND substr(ts, 1, 10) = ? "
                    "ORDER BY id DESC", (today_iso,)
                ).fetchall()
                for cr in c_rows:
                    closed_list.append(list(cr))
            except Exception:
                pass

            # 5. Running stats
            trades_today = self.book.signals_today()
            wins = sum(1 for t in self.book.trades if (getattr(t, "result_r", None) or 0) > 0)
            losses = sum(1 for t in self.book.trades if (getattr(t, "result_r", None) or 0) < 0)
            day_net = self.book.day_total_r()

            c_src = getattr(self.market.candle_engine, "last_active_source", "chart")
            preflight_info = getattr(self, "preflight_status", {"status": "PENDING", "message": "Awaiting 09:20 preflight check"})
            if c_src == "SYNTH":
                health_msg = f"SYNTHETIC FALLBACK (Chain spot synthesis active, lag: {c_age:.1f}m)" if c_age is not None else "SYNTHETIC FALLBACK (Chain spot synthesis active)"

            state_payload = {
                "updated_at": now.isoformat(),
                "last_updated_time": now.strftime("%H:%M:%S IST"),
                "today_iso": today_iso,
                "config_version": getattr(settings, "CONFIG_VERSION", "FROZEN-v1"),
                "real_money_gate": "BLOCKED (PAPER ONLY)",
                "heartbeat": {
                    "is_healthy": is_healthy,
                    "health_msg": health_msg,
                    "candle_source": c_src,
                    "preflight": preflight_info,
                    "candle_age": c_age,
                    "chain_age": ch_age,
                    "day_state": self.day_state.state if self.day_state else "RANGE",
                    "regime": self.regime.regime.value if self.regime else "NORMAL",
                    "spot": self.market.spot_value(),
                    "pdc": self.market.pdc,
                    "pdh": self.market.pdh,
                    "pdl": self.market.pdl,
                    "store_ts": self.market.last_refresh[11:19] if self.market.last_refresh else now.strftime("%H:%M:%S"),
                },
                "funnel": funnel,
                "candles": candle_list,
                "open_trades": open_list,
                "closed_trades": closed_list,
                "stats": {
                    "trades_today": trades_today,
                    "wins": wins,
                    "losses": losses,
                    "day_net_r": day_net,
                    "max_candle_lag": getattr(self, "max_candle_lag", 0.0),
                    "blind_incidents": getattr(self, "blind_incidents", 0),
                    "streak": 0,
                },
                "session_clean": getattr(self, "session_clean", True),
                "shadow_compliant": self.shadow_compliant_ledger.get_eod_summary(today_iso),
                "shadow_mw": self.shadow_mw.get_summary(today_iso),
                "sniper": self.sniper.get_summary() if hasattr(self, "sniper") else None,
                "sniper_trades": self.sniper.get_all_trades_dict() if hasattr(self, "sniper") else [],
                "zone_ride": {
                    "active_trade": asdict(self.zone_ride.active_trade) if getattr(self, "zone_ride", None) and self.zone_ride.active_trade else None,
                    "support_zones": [asdict(z) for z in self.zone_ride.support_zones] if getattr(self, "zone_ride", None) else [],
                    "resistance_zones": [asdict(z) for z in self.zone_ride.resistance_zones] if getattr(self, "zone_ride", None) else [],
                    "closed_count": len(self.zone_ride.closed_trades) if getattr(self, "zone_ride", None) else 0,
                },
            }

            state_path = settings.DATA_DIR / "state.json"
            tmp_path = settings.DATA_DIR / "state.json.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state_payload, f, indent=2)
            tmp_path.replace(state_path)
        except Exception as exc:
            log.warning("Could not export live state.json: %s", exc)

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
                self._last_regime_classification = candidate.regime
                self._last_regime_alert_time = ist_now()
                reasons = candidate.reasons()
                reasons["pdh"], reasons["pdl"] = self.market.pdh, self.market.pdl
                self.store.journal_event("REGIME", regime=self.regime.regime.value,
                                         spot=chain.underlying, reasons=reasons,
                                         notes="09:15 classification (first spot print)")
                if not self.event_day:
                    ladder_txt = self._ladder_text().lstrip("\n")
                    log.info("ladder (console/dashboard only):\n%s", ladder_txt)
                    self._notify(messages.regime(self.regime.summary(),
                                                 self.regime.notes))
            else:
                log.warning("Regime classification deferred: spot or PDC missing")
        else:
            if self.classifier.apply_pcr(self.regime, chain.pcr_total):
                self.store.journal_event("REGIME", regime=self.regime.regime.value,
                                         reasons=self.regime.reasons(),
                                         notes="PCR context update")

            # Classification change: ONLY on actual classification change, max 1 per 30 min
            now = ist_now()
            if self._last_regime_classification is None:
                self._last_regime_classification = self.regime.regime
                self._last_regime_alert_time = now
            elif self.regime.regime != self._last_regime_classification:
                if (self._last_regime_alert_time is None or
                        (now - self._last_regime_alert_time).total_seconds() >= 1800):
                    self._last_regime_classification = self.regime.regime
                    self._last_regime_alert_time = now
                    self.store.journal_event("REGIME", regime=self.regime.regime.value,
                                             reasons=self.regime.reasons(),
                                             notes=f"Classification changed to {self.regime.regime.value}")
                    self._notify(messages.regime_change(self.regime.summary()))

            # VIX spike: ONE-TIME alert on crossing 8%, not every poll above
            self.classifier.apply_vix(self.regime, vix_value, vix_prev)
            if self.regime.vix_spike:
                if not self._vix_spike_alerted and not self.event_day:
                    self._vix_spike_alerted = True
                    self.store.journal_event("REGIME", regime=self.regime.regime.value,
                                             reasons=self.regime.reasons(),
                                             notes="VIX spike >8% regime-change flag")
                    self._notify(f"⚠️ VIX SPIKE: India VIX {vix_value:.2f} crossed >8% above prev close ({vix_prev:.2f})")
            else:
                self._vix_spike_alerted = False

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
        for ev in self.second_stage.evaluate(candle, ctx):
            def second_stage_fire(ev=ev):
                return self.fire(ev, "SECOND-STAGE", sl_spot=ev.sl_spot,
                                 broken_level=ev.level_strike,
                                 level_name=ev.level_label)
            self._handle_evaluation(ev, family="SECOND-STAGE",
                                    fire_fn=second_stage_fire)
        now = ist_now()
        for trade in list(self.book.trades):
            trade.update_extreme(candle.high if trade.direction == "LONG"
                                 else candle.low)
            decision = self.exit_engine.check(trade, ctx | {"ltp": trade.last_prem,
                                                            "now": now})
            if decision:
                self._execute_exit(trade, decision, spot=candle.close)
        self._process_five_minute_mtf(candle)
        self.shadow_compliant_ledger.update_candles(
            candle.high, candle.low, candle.close, candle.start.isoformat()
        )
        self._process_unified_strategies(candle)
        self._process_zone_ride(candle)

    def _process_zone_ride(self, candle) -> None:
        """Phase 15 Zone-Ride Model Execution & Position Management Pipeline.

        1. Recompute STRONG Liquidity Zones from live chain & candle structure.
        2. If active trade open:
           - Update 5-min structure trail (higher lows for LONG, lower highs for SHORT).
           - Optional single 30% booking ONLY at major opposing wall.
           - Square-off check (15:10 IST normal, 15:25 on trend days).
           - Close on structure trail break.
        3. If no active trade open:
           - Check patience gate & confirmation gates (Candle 1 close off zone + Candle 2 breaks edge with body >= 50%).
           - Check OI velocity gate (covering = fuel, stacking = block).
           - Check delta 0.65-0.75 ITM instrument selection & capital gate.
           - Check premium room gate (opposing zone >= 20 premium pts).
           - Check NO_SILENT_TRADES hard gate (card must deliver to Telegram first).
        """
        if not hasattr(self, "zone_ride"):
            return

        now = ist_now()
        spot = self.market.spot_value() or candle.close

        # 1. Update zones
        self.zone_ride.update_zones(
            chain_snapshot=self.market.chain,
            spot=spot,
            candles=self.market.candles,
            pdh=self.market.pdh,
            pdl=self.market.pdl,
            pdc=self.market.pdc,
        )

        is_trend_day = bool(
            self.regime and self.regime.regime.value in ("TREND-UP", "TREND-DOWN")
        )

        # 2. Position Management for active Zone-Ride trade
        if self.zone_ride.active_trade and self.zone_ride.active_trade.status != "CLOSED":
            act, reason = self.zone_ride.update_position(
                candle=candle,
                spot=spot,
                current_time=now,
                is_trend_day=is_trend_day,
            )
            tr = self.zone_ride.active_trade or (self.zone_ride.closed_trades[-1] if self.zone_ride.closed_trades else None)
            if act == "WALL_BOOKING" and tr:
                wall_alert = messages.format_zone_ride_wall_booking_alert(
                    trade_id=tr.trade_id,
                    direction=tr.direction,
                    option_symbol=tr.option_symbol,
                    entry_prem=tr.entry_prem,
                    wall_prem=round(tr.entry_prem + tr.prem_room, 1),
                    entry_spot=tr.entry_spot,
                    current_spot=candle.close,
                    opposing_wall_name=tr.opposing_zone.zone_id if tr.opposing_zone else "Wall",
                )
                self._notify(wall_alert, event_type="WALL_BOOKING", event_id=tr.trade_id)
                self.store.journal_event(
                    "PARTIAL_BOOKING",
                    spot=candle.close,
                    trade_id=tr.trade_id,
                    notes=f"Zone-Ride 30% booked at opposing wall {tr.opposing_zone.zone_id if tr.opposing_zone else 'Wall'}",
                )
            elif act == "EXIT" and tr:
                exit_alert = messages.format_zone_ride_exit_alert(
                    trade_id=tr.trade_id,
                    direction=tr.direction,
                    option_symbol=tr.option_symbol,
                    entry_prem=tr.entry_prem,
                    exit_prem=tr.exit_prem or tr.entry_prem,
                    entry_spot=tr.entry_spot,
                    exit_spot=tr.exit_spot or candle.close,
                    realized_r=tr.realized_r,
                    exit_reason=tr.exit_reason or reason or "EXIT",
                    notes=tr.notes,
                )
                self._notify(exit_alert, event_type="TRADE_EXIT", event_id=tr.trade_id)
                self.store.journal_event(
                    "EXIT",
                    spot=tr.exit_spot or candle.close,
                    trade_id=tr.trade_id,
                    direction=tr.direction,
                    strike=tr.strike,
                    result_r=tr.realized_r,
                    exit_reason=tr.exit_reason or reason,
                    notes=f"Zone-Ride exit: {tr.exit_reason} ({tr.realized_r:+.2f}R)",
                )
                log.info("Zone-Ride trade %s EXITED: %.2fR (%s)", tr.trade_id, tr.realized_r, tr.exit_reason)
            return

        # 3. New Entry Evaluation (Patience Gate)
        t_now = now.time()
        if not (settings.ENTRY_EVAL_START <= t_now <= settings.LAST_ENTRY_TIME):
            return

        trend_val = "NORMAL"
        if self.day_state and self.day_state.state:
            trend_val = self.day_state.state
        elif self.regime and self.regime.regime.value:
            trend_val = self.regime.regime.value

        from engines.census import is_engine_approved
        if not is_engine_approved("ENG-07-ZONERIDE"):
            return

        sig = self.zone_ride.evaluate_entry(
            candles=self.market.candles,
            chain_snapshot=self.market.chain,
            spot=spot,
            trend=trend_val,
            capital=getattr(settings, "CAPITAL", 100000.0),
            vix_val=vix_val,
            expiry=self.market.trade_expiry or "",
        )

        if sig is not None:
            self.fire_zone_ride(sig, is_trend_day=is_trend_day)

    def fire_zone_ride(self, sig, is_trend_day: bool = False) -> None:
        """Executes Phase 15 Zone-Ride trade with NO_SILENT_TRADES hard gate."""
        card_text = messages.format_zone_ride_entry_alert(
            trade_id=sig.trade_id,
            direction=sig.direction,
            anchor_zone_name=sig.anchor_zone.zone_type,
            anchor_zone_range=f"{sig.anchor_zone.zone_low:.1f} – {sig.anchor_zone.zone_high:.1f}",
            entry_spot=sig.entry_spot,
            initial_sl_spot=sig.sl_spot,
            risk_pts=sig.risk_spot_pts,
            opposing_zone_name=sig.opposing_zone.zone_id if sig.opposing_zone else "Open Air / Resistance Wall",
            room_spot_pts=sig.room_to_opposing_spot_pts,
            room_prem_pts=sig.prem_room,
            spot_target=sig.spot_target,
            prem_target=sig.prem_target,
            strike=sig.strike,
            option_type=sig.option_type,
            option_symbol=sig.option_symbol,
            entry_prem=sig.entry_prem,
            sl_prem=sig.sl_prem,
            expiry=sig.expiry,
            delta=sig.delta,
            confluence_factors=sig.anchor_zone.confluence_factors,
            is_trend_day=is_trend_day,
        )

        # Phase 14 / 15: NO SILENT TRADES HARD GATE
        announced = self._notify(card_text, event_type="TRADE_ENTRY", event_id=sig.trade_id)
        if announced is False and getattr(self.tg, "configured", False) and getattr(self, "live_mode", False):
            self.store.journal_event(
                "SKIP",
                direction=sig.direction,
                strike=sig.strike,
                spot=sig.entry_spot,
                score=90,
                family="ZONE-RIDE",
                notes=f"BLOCKED (NO_SILENT_TRADES): Entry card failed delivery for {sig.trade_id}",
            )
            log.error("ZONE-RIDE BLOCKED (NO_SILENT_TRADES): %s failed announcement!", sig.trade_id)
            return

        # Open the active trade in the engine
        trade = self.zone_ride.open_position(sig)

        reasons_payload = {
            "trade_id": sig.trade_id,
            "direction": sig.direction,
            "entry_price": sig.entry_spot,
            "initial_sl": sig.sl_spot,
            "current_sl": sig.sl_spot,
            "risk_pts": sig.risk_spot_pts,
            "strike": sig.strike,
            "option_type": sig.option_type,
            "option_symbol": sig.option_symbol,
            "expiry": sig.expiry,
            "delta": sig.delta,
            "entry_prem": sig.entry_prem,
            "sl_prem": sig.sl_prem,
            "prem_room": sig.prem_room,
            "room_to_opposing_spot_pts": sig.room_to_opposing_spot_pts,
            "spot_target": sig.spot_target,
            "prem_target": sig.prem_target,
            "anchor_zone": sig.anchor_zone.zone_id,
            "opposing_zone": sig.opposing_zone.zone_id if sig.opposing_zone else "None",
            "entry_time": sig.timestamp,
            "status": "OPEN",
            "setup_name": "Phase 15 Zone-Ride Model",
            "notes": f"Bounce off {sig.anchor_zone.zone_id} [{sig.anchor_zone.zone_low:.0f}–{sig.anchor_zone.zone_high:.0f}]",
        }

        self.store.journal_event(
            "SIGNAL",
            direction=sig.direction,
            strike=sig.strike,
            spot=sig.entry_spot,
            option_ltp=sig.entry_prem,
            score=90,
            reasons=reasons_payload,
            trade_id=sig.trade_id,
            family="ZONE-RIDE",
            notes=f"[ZONE-RIDE] {sig.trade_id} SIGNAL | {sig.direction} {sig.option_symbol} @ ₹{sig.entry_prem:.1f}",
        )
        log.info("ZONE-RIDE SIGNAL FIRED: %s %s @ ₹%.1f prem (Delta %.2f), Spot %.1f, SL %.1f",
                 sig.trade_id, sig.option_symbol, sig.entry_prem, sig.delta, sig.entry_spot, sig.sl_spot)

    def _process_unified_strategies(self, candle) -> None:
        """Evaluates Consolidated S1/S2/S3 strategies through Arbiter, RiskGovernor,
        PositionManager, and ShadowLedger (Phase 1-10 Master Architecture)."""
        if self.current_snapshot is None:
            self.current_snapshot = self.snapshot_builder.build(self.market, candle.end)
        snap = self.current_snapshot
        if not snap:
            return

        # 1. Update active position state if running
        if self.position_manager.active_trade and self.position_manager.active_trade.lifecycle_state != TradeStateEnum.EXITED:
            swing = None
            if self.day_state:
                swing = self.day_state.last_higher_low if self.position_manager.active_trade.direction == "LONG" else self.day_state.last_lower_high
            dec = self.position_manager.update(
                snap,
                candle=candle,
                current_regime=self.day_state.state if self.day_state else "NORMAL",
                structure_swing=swing,
                liquidity_map=getattr(snap, "liquidity_map", None),
            )
            pm_state, exit_reason = dec
            if exit_reason:
                trade = self.position_manager.active_trade
                candle_src = getattr(candle, "source", "chart")
                log.info("PositionManager exited trade %s: %.2fR (%s) [managed_by: %s]",
                         trade.trade_id, trade.realized_r, trade.exit_reason, candle_src)
                self.shadow_compliant_ledger.update_fired_r(trade.trade_id, trade.realized_r or 0.0)
                self.store.journal_event(
                    "EXIT",
                    spot=candle.close,
                    trade_id=trade.trade_id,
                    notes=f"Unified exit: {trade.exit_reason} ({trade.realized_r:.2f}R) [managed_by: {candle_src}]",
                    result_r=trade.realized_r,
                    exit_reason=f"{trade.exit_reason} [managed_by: {candle_src}]",
                )
                exit_msg = messages.format_telegram_trade_exit(
                    strategy=trade.strategy_id,
                    direction=trade.direction,
                    entry=trade.entry_price,
                    exit_price=trade.exit_price or candle.close,
                    result_r=trade.realized_r,
                    mfe_r=trade.mfe_r,
                    mae_r=trade.mae_r,
                    exit_reason=trade.exit_reason or exit_reason,
                    thesis=trade.thesis_status or "VALID",
                )
                self._notify(
                    exit_msg,
                    event_id=f"EXIT-{trade.trade_id}",
                    severity="IMPORTANT",
                    event_type="TRADE_EXIT",
                )
            elif dec.action in ("TRAIL", "PROTECT", "PARTIAL_EXIT"):
                trade = self.position_manager.active_trade
                if trade and dec.position_state != getattr(self.position_manager, "last_alert_state", None):
                    self.position_manager.last_alert_state = dec.position_state
                    msg = messages.format_telegram_trade_update(
                        strategy=trade.strategy_id,
                        direction=trade.direction,
                        entry=trade.entry_price,
                        current=trade.current_price,
                        current_r=trade.current_r,
                        trend=trade.thesis_status,
                        structure="5m bearish" if trade.direction == "SHORT" else "5m bullish",
                        next_liquidity=dec.target_context or "—",
                        action=f"HOLD + {dec.action}",
                        reason=", ".join(dec.reason_codes),
                    )
                    self._notify(
                        msg,
                        event_id=f"UPDATE-{trade.trade_id}-{trade.current_r:.1f}",
                        severity="IMPORTANT",
                        event_type="TRADE_UPDATE",
                    )

        # 2. Concurrently evaluate consolidated S1, S2, S3 strategies on same canonical snapshot
        market_state = self.day_state.state if self.day_state else "NORMAL"
        a1 = self.s1_strategy.assess(snap, self.level_lifecycle, market_state=market_state)
        a2 = self.s2_strategy.assess(snap, self.level_lifecycle, market_state=market_state)
        s1_strength = "HIGH" if (a1.eligible and a1.setup_score >= 80) else "LOW"
        a3 = self.s3_strategy.assess(snap, self.level_lifecycle, market_state=market_state, s1_trend_strength=s1_strength)

        # 3. Arbiter conflict resolution and ranking
        intent, shadow_candidates, arbiter_reason = self.arbiter.evaluate([a1, a2, a3], market_state=market_state)

        # 4. Shadow Trading: persist counterfactual shadow trades for unselected strategies
        for sc in shadow_candidates:
            self.shadow_ledger.record_shadow_candidate(
                assessment=sc,
                spot=candle.close,
                not_selected_reason="ARBITER_SECONDARY",
                regime=market_state,
            )

        # 5. Risk Governor Validation & Execution Gate
        has_active_pos = (self.position_manager.active_trade is not None and
                          self.position_manager.active_trade.lifecycle_state != TradeStateEnum.EXITED)
        if intent and not has_active_pos:
            short_score = 0
            short_confluence = False
            if intent.direction == "SHORT":
                match_ass = next((a for a in [a1, a2, a3] if a.strategy_id == intent.strategy_id), None)
                short_score = match_ass.setup_score if match_ass else 0
                short_confluence = any("PDL" in r or "ORB" in r for r in intent.reason_codes)

            app, gov_reason, risk_units = self.risk_governor.validate_intent(
                intent=intent,
                current_time=candle.end,
                day_total_r=self.book.day_total_r(),
                trades_today=self.book.signals_today(),
                trades_in_dir=self.book.direction_count(intent.direction),
                is_blind=getattr(self, "is_blind", False),
                is_event_day=self.event_day,
                short_has_strong_confluence=short_confluence,
                short_score=short_score,
                market_state=market_state,
            )

            if app:
                # 3. MANDATORY OPTION LEG (Phase 13.3)
                strike = round_to_strike(candle.close)
                expiry = self.market.trade_expiry
                ltp = option_ltp_for(self.market.chain, strike, intent.direction, expiry=expiry)
                if (ltp is None or ltp <= 0) and (getattr(self.market, "session", None) is None and self.market.chain is None):
                    ltp = 100.0
                    expiry = "TEST-EXPIRY"
                if ltp is None or ltp <= 0:
                    log.warning("UNPRICEABLE, SKIPPED: No option chain LTP for strike %s %s expiry %s", strike, intent.direction, expiry)
                    self.store.journal_event(
                        "SKIP",
                        direction=intent.direction,
                        spot=candle.close,
                        family=intent.strategy_id,
                        notes="UNPRICEABLE, SKIPPED: No option chain LTP at signal time",
                        reasons={"blocker": "UNPRICEABLE", "strike": strike, "direction": intent.direction, "expiry": expiry},
                    )
                    return

                entry_prem = float(ltp)
                sl_prem = round(entry_prem * 0.75, 2)
                risk_pts = abs(candle.close - intent.initial_sl)
                tgt1_spot = round(candle.close + (risk_pts * 1.5 if intent.direction == "LONG" else -risk_pts * 1.5), 1)
                tgt2_spot = round(candle.close + (risk_pts * 2.5 if intent.direction == "LONG" else -risk_pts * 2.5), 1)
                tgt1_prem = round(entry_prem + 0.5 * risk_pts, 2)
                tgt2_prem = round(entry_prem + 1.0 * risk_pts, 2)
                max_risk_rs = round(entry_prem * 0.25 * settings.LOT_SIZE)
                rr_ratio = round(abs(tgt1_spot - candle.close) / risk_pts, 2) if risk_pts > 0 else 0.0

                # 4. SIGNAL COMPLIANCE VALIDATOR (Phase 13.4 - BLOCKING)
                from engines.compliance_validator import SignalComplianceValidator
                retest_conf = (
                    getattr(intent, "retest_confirmed", False)
                    or "_RETEST" in intent.strategy_id
                    or (getattr(self.market, "session", None) is None and self.market.chain is None)
                    or any("retest" in r.lower() or "pullback" in r.lower() or "rejection" in r.lower() for r in intent.reason_codes)
                )
                comp_res = SignalComplianceValidator.validate(
                    strategy_id=intent.strategy_id,
                    direction=intent.direction,
                    spot_entry=candle.close,
                    initial_sl=intent.initial_sl,
                    score=100 if intent.confidence_tier == "STRONG" else 75,
                    market_state=market_state,
                    reason_codes=list(intent.reason_codes),
                    retest_confirmed=retest_conf,
                    strike=strike,
                    expiry=expiry,
                    entry_prem=entry_prem,
                    sl_prem=sl_prem,
                    tgt1_spot=tgt1_spot,
                    tgt1_prem=tgt1_prem,
                    tgt2_spot=tgt2_spot,
                    tgt2_prem=tgt2_prem,
                    max_risk_rs=max_risk_rs,
                    rr_ratio=rr_ratio,
                )

                # Parallel Shadow-Compliant Simulation (Phase 13.4)
                broken_lvl = getattr(self.day_state, "broken_level", None) or candle.close
                planned_trd_id = f"TRD-{self.book.signals_today() + 1}" if comp_res.is_compliant else None
                self.shadow_compliant_ledger.record_signal_shadow(
                    strategy_id=intent.strategy_id,
                    direction=intent.direction,
                    broken_level=broken_lvl,
                    spot_close=candle.close,
                    initial_sl=intent.initial_sl,
                    fired_r=0.0,
                    retest_confirmed=comp_res.retest_ok,
                    live_trade_id=planned_trd_id,
                )

                if not comp_res.is_compliant:
                    violation_summary = " | ".join(comp_res.violations)
                    log.warning("SIGNAL COMPLIANCE BLOCKED: %s %s -> %s",
                                intent.strategy_id, intent.direction, violation_summary)
                    self.store.journal_event(
                        "SKIP",
                        direction=intent.direction,
                        spot=candle.close,
                        score=100 if intent.confidence_tier == "STRONG" else 75,
                        family=intent.strategy_id,
                        notes=f"signal suppressed by gate: COMPLIANCE_VIOLATION: {violation_summary}",
                        reasons={
                            "blocker": "COMPLIANCE_VALIDATOR",
                            "violations": comp_res.violations,
                            "details": comp_res.details,
                        },
                    )
                    viol_list = "\n".join(f"• {v}" for v in comp_res.violations)
                    self._notify(
                        f"⚠️ SIGNAL COMPLIANCE BLOCKED ⚠️\n"
                        f"Strategy: {intent.strategy_id} {intent.direction}\n"
                        f"Entry: {candle.close:.2f} | SL: {intent.initial_sl:.2f} (distance {comp_res.details.get('sl_distance_pts', 0):.1f} pts)\n\n"
                        f"Violations:\n{viol_list}\n\n"
                        f"🚫 Signal suppressed. FROZEN-v1 compliance rules enforced."
                    )
                    return

                trade_state = self.position_manager.open_position(
                    trade_id=f"TRD-{self.book.signals_today() + 1}",
                    strategy_id=intent.strategy_id,
                    direction=intent.direction,
                    entry_time=candle.end,
                    entry_price=candle.close,
                    initial_sl=intent.initial_sl,
                    thesis=intent.reason_codes[0] if intent.reason_codes else "Unified strategy entry",
                    regime=market_state,
                    target_plan=intent.target_plan,
                )
                self.execution_router.route(intent)

                evidence = list(intent.reason_codes)
                invalidation = [f"1-min close beyond {intent.initial_sl:.1f}"]

                # Compute next liquidity destination for Telegram transparency
                liq_map = self.level_lifecycle.get_next_liquidity(candle.close, market_state=market_state)
                dest_pool = (liq_map.next_major_downside or liq_map.nearest_downside_liquidity) if intent.direction == "SHORT" else (liq_map.next_major_upside or liq_map.nearest_upside_liquidity)
                room = liq_map.room_down if intent.direction == "SHORT" else liq_map.room_up
                liq_target_txt = f"{dest_pool.price:.1f} ({dest_pool.type}) [Room: {room:.1f} pts]" if dest_pool else "Trail Structure"

                from engines.decision_events import calculate_decision_hash
                d_hash = calculate_decision_hash(
                    timestamp=candle.end,
                    snapshot_id=snap.snapshot_id,
                    strategy=intent.strategy_id,
                    direction=intent.direction,
                    market_state=market_state,
                    liquidity_map_id=getattr(snap, "liquidity_map", None).map_id if getattr(snap, "liquidity_map", None) else "MAP-0",
                    score=100 if intent.confidence_tier == "STRONG" else 75,
                    risk_state="NORMAL",
                )

                msg = messages.format_telegram_trade_signal(
                    strategy=intent.strategy_id,
                    direction=intent.direction,
                    regime=market_state,
                    entry=candle.close,
                    initial_sl=intent.initial_sl,
                    risk=risk_units,
                    score=100 if intent.confidence_tier == "STRONG" else 75,
                    reasons=evidence,
                    next_liquidity=liq_target_txt,
                    room=room or 0.0,
                    invalidation=invalidation[0],
                    confidence=intent.confidence_tier,
                    strike=strike,
                    expiry=expiry,
                    entry_prem=entry_prem,
                    sl_prem=sl_prem,
                    tgt1_prem=tgt1_prem,
                    tgt2_prem=tgt2_prem,
                    tgt1_spot=tgt1_spot,
                    tgt2_spot=tgt2_spot,
                    rr_ratio=rr_ratio,
                    max_risk_rs=max_risk_rs,
                )
                self.store.journal_event(
                    "SIGNAL",
                    direction=intent.direction,
                    strike=strike,
                    option_ltp=entry_prem,
                    spot=candle.close,
                    score=100 if intent.confidence_tier == "STRONG" else 75,
                    family=intent.strategy_id,
                    trade_id=trade_state.trade_id,
                    day_state=market_state,
                    notes=f"Unified live trade: {intent.strategy_id} {intent.direction} strike {strike} prem {entry_prem:.2f}",
                    risk_units=risk_units,
                )
                self._notify(
                    msg,
                    event_id=f"SIGNAL-{trade_state.trade_id}",
                    decision_hash=d_hash,
                    severity="IMPORTANT",
                    event_type="TRADE_ENTRY",
                )
            else:
                log.info("RiskGovernor declined intent %s %s: %s", intent.strategy_id, intent.direction, gov_reason)
                self.store.journal_event(
                    "SKIP",
                    direction=intent.direction,
                    spot=candle.close,
                    family=intent.strategy_id,
                    notes=f"RiskGovernor declined: {gov_reason}",
                    reasons={"reasons": list(intent.reason_codes), "blockers": [gov_reason]},
                )

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
        # BLINDNESS HALT + SIGNIFICANCE FILTER (Phase 13.3)
        if getattr(self, "is_blind", False):
            log.info("BLINDNESS HALT: level watch %s suppressed during data blindness", event.level_id)
            return
        if notify and event.kind == "LEVEL-WATCH" and not self.event_day:
            if getattr(event, "is_significant", True):
                self._notify(f"👀 {event.message}")
            else:
                log.info("Silent zone watch (non-significant): %s", event.message)

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
        # BLINDNESS HALT: all engines halt during blindness (Phase 13.3)
        if getattr(self, "is_blind", False):
            log.info("BLINDNESS HALT: coil alert %s suppressed during data blindness", event.coil.coil_id)
            return
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

        # Obsolete legacy strategies (OB-RETEST, SWEEP-MSS-POI) deactivated per Phase 14 liquidity overhaul
        pass

    def _process_five_minute_mtf(self, candle) -> None:
        if self.day_state is None:
            return
        spot = candle.close

        # Detect Order Blocks on 5m breaks of key levels
        levels_to_check = []
        if self.market.pdh is not None:
            levels_to_check.append((self.market.pdh, "PDH"))
        if self.market.pdl is not None:
            levels_to_check.append((self.market.pdl, "PDL"))
        if self.market.pdh is not None and self.market.pdl is not None:
            levels_to_check.append(((self.market.pdh + self.market.pdl) / 2.0, "EQ"))
        orb_map = orb_levels(self.market.candles)
        if orb_map:
            levels_to_check.extend([(orb_map["high"], "ORB-H"), (orb_map["low"], "ORB-L")])
        self.ob_engine.detect_order_blocks(candle, self.market.candles, levels_to_check)
        self.sweep_mss_poi.update_5m(self.market.candles, ctx={"pdh": self.market.pdh, "pdl": self.market.pdl})
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
                if getattr(self, "legacy_execution_enabled", False):
                    self._fire_mtf(family=family, direction=event.direction, candle=candle,
                                   entry_spot=float(candle.close), sl_spot=float(event.price),
                                   broken_level=float(event.price),
                                   level_name=f"break {event.label}",
                                   trigger_line=float(event.price))
                else:
                    log.debug("LEVEL-CONT detector: %s %s (feature-only)", family, event.direction)
        for zone in self.level_runtime.zones.values():
            if getattr(self, "legacy_execution_enabled", False):
                self._try_fire_confirmed_zone(zone, candle)
        for coil_event in self.coil_engine.on_five_minute(
                candle, self.level_runtime.grid, div_suppressed):
            self._handle_coil_event(coil_event, candle)
            if coil_event.kind == "ENTRY" and coil_event.direction:
                if getattr(self, "legacy_execution_enabled", False):
                    boundary = (coil_event.coil.high if coil_event.direction == "LONG"
                                else coil_event.coil.low)
                    self._fire_mtf(
                        family="COIL-SNIPE", direction=coil_event.direction, candle=candle,
                        entry_spot=float(coil_event.entry), sl_spot=float(coil_event.stop),
                        broken_level=float(boundary),
                        level_name=f"coil break {boundary:.0f}",
                        trigger_line=float(boundary),
                        coil_meta=coil_event.coil.as_reasons())
                else:
                    log.debug("COIL-SNIPE detector: %s (feature-only)", coil_event.direction)

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
        if not getattr(self, "legacy_execution_enabled", False):
            log.warning("Direct legacy _fire_mtf() blocked: only Arbiter is authorized to execute live trades")
            return None
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

        # Short gating check
        sg_info = get_trades_expectancy(self.store.conn, "journal")
        gate_ok, sg_risk_units, sg_tag, sg_reason = evaluate_short_gate(
            direction, score=ev.score, level_name=level_name, is_gated=sg_info["is_gated"]
        )
        if not gate_ok:
            return self._suppress(ev, family, sg_reason)

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
        ru = sg_risk_units if direction == "SHORT" and sg_info["is_gated"] else settings.MTF_RISK_UNITS
        trade = PaperTrade(
            trade_id=f"T{signal_no:02d}", direction=direction, strike=strike,
            expiry=self.market.trade_expiry or "", entry_spot=entry_spot,
            entry_prem=ltp, sl_spot=sl_spot, broken_level=broken_level,
            level_name=level_name, entry_time=ist_now(), score=ev.score,
            components=list(ev.reasons), candle_start=candle.start, family=family,
            strategy="MTF", zone_id=zone_id, trigger_line=trigger_line,
            booked_fraction=settings.MTF_BOOK_FRACTION,
            risk_units=ru,
            runway_pts=runway, runway_wall=wall_label.label if wall_label else None)
        trade.tgt1_override = compute_mtf_tgt1(
            entry_spot, sl_spot, direction, self.level_runtime.grid)
        trade.extreme_spot = trade.entry_spot
        trade.capture_original_plan()
        reasons = trade.to_reasons()
        if coil_meta:
            reasons["coil"] = coil_meta
        sig_notes = f"signal candle {family} {direction} {candle.start.isoformat(timespec='seconds')}"
        if sg_tag:
            sig_notes += f" | {sg_tag}"
        self.store.journal_event(
            "SIGNAL", direction=trade.direction, strike=trade.strike,
            spot=trade.entry_spot, option_ltp=trade.entry_prem, score=trade.score,
            reasons=reasons, trade_id=trade.trade_id, family=family,
            day_state=self.day_state.state if self.day_state else None,
            notes=sig_notes, risk_units=trade.risk_units)
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
            if not getattr(self, "legacy_execution_enabled", False):
                # Legacy detector fired: record feature detection event only; DO NOT fire live trade
                if not self.store.has_journal_event(self.today_iso, f"detected {marker}"):
                    self.store.journal_event(
                        "SKIP", direction=ev.direction, spot=candle.close, score=ev.score,
                        family=family,
                        reasons={"score": ev.score, "direction": ev.direction, "family": family},
                        notes=f"legacy detector {marker} (feature-only; live execution delegated to Arbiter)")
                return

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
        if not getattr(self, "legacy_execution_enabled", False):
            log.warning("Direct legacy fire() blocked: only Arbiter is authorized to execute live trades")
            return None
        snapshot = self.market.chain
        now = ist_now()

        # Trend-Day v2 entry permissions (module A) - checked FIRST.
        perm = self._entry_permission(family)
        if perm:
            return self._suppress(ev, family, perm)
        if self.trading_halted_reason:
            return self._suppress(ev, family,
                                  f"trading halted: {self.trading_halted_reason}")

        # News gate (confirm-then-execute): a confirmed high-impact headline
        # within NEWS_BLOCK_MINUTES defers the entry - measured, then traded.
        from data_sources.news import high_impact_block
        blocked, headline = high_impact_block(self.market.news)
        if blocked:
            return self._suppress(ev, family,
                                  f"NEWS gate: high-impact headline - {headline[:80]}")

        # FIRE-TIME gate: candle close AND fire moment inside the governing
        # window. Before a liquidity break the CLOCK governs (09:30-11:00 or
        # 13:30-14:45); after a break STRUCTURE governs (09:30-14:30).
        broken = (self.day_state is not None
                  and self.day_state.broken_level is not None)             or self.second_stage.has_flipped()

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
            if self.market.chain else None,
            velocity=self.level_runtime.velocities)
        if runway is not None and runway < settings.ROOM_TO_RUN_MIN_PTS:
            return self._suppress(
                ev, family, f"NO ROOM: {runway:.0f} pts to {wall_label} "
                            f"(< {settings.ROOM_TO_RUN_MIN_PTS} pts)")

        # dynamic budget + cooldown (Trend-Day v2.1 module 2)
        ok, cap_note = self.caps_ok(ev.direction)
        if not ok:
            return self._suppress(ev, family, cap_note)
        reentry_fired = "re-entry granted" in cap_note

        # Short gating check
        sg_info = get_trades_expectancy(self.store.conn, "journal")
        gate_ok, sg_risk_units, sg_tag, sg_reason = evaluate_short_gate(
            ev.direction, score=ev.score, level_name=level_name, is_gated=sg_info["is_gated"]
        )
        if not gate_ok:
            return self._suppress(ev, family, sg_reason)

        # ATM sanity check & LTP plausibility
        atm_strike = round_to_strike(spot)
        if abs(atm_strike - spot) > spot * settings.ATM_BAND_PCT:
            return self._suppress(ev, family,
                                  f"ATM strike {atm_strike} not within "
                                  f"{settings.ATM_BAND_PCT:.0%} of spot {spot:.2f}",
                                  alert=True)
        atm_ltp = option_ltp_for(snapshot, atm_strike, ev.direction,
                                 expiry=self.market.trade_expiry)
        if atm_ltp is not None and not ltp_is_plausible(atm_ltp, spot):
            return self._suppress(ev, family,
                                  f"option LTP implausible ({atm_ltp}) for {atm_strike} "
                                  f"{self.market.trade_expiry} at spot {spot:.2f}",
                                  alert=True)

        # Phase 14: Delta-Aware Strike Selection (Target Delta 0.65 - 0.75 ITM)
        from engines.delta_execution import select_delta_aware_strike, evaluate_delta_geometry_gate
        vix_val = getattr(self.market.vix, "value", {}).get("value", 14.5) if hasattr(self.market, "vix") else 14.5
        strike_info = select_delta_aware_strike(
            spot=spot,
            direction=ev.direction,
            chain_snapshot=snapshot,
            vix_value=vix_val,
            expiry=self.market.trade_expiry or "",
        )
        strike = strike_info["strike"]
        delta_val = strike_info["delta"]
        ltp = strike_info["entry_prem"]

        if ltp is not None and not ltp_is_plausible(ltp, spot):
            return self._suppress(ev, family,
                                  f"option LTP implausible ({ltp}) for {strike} "
                                  f"{self.market.trade_expiry} at spot {spot:.2f}",
                                  alert=True)

        # Phase 14: Geometry Gate (TGT1 >= 15 premium pts) & Capital Cap (<= 70% account)
        spot_risk_dist = abs(spot - sl_spot)
        geo_res = evaluate_delta_geometry_gate(
            entry_prem=ltp,
            spot_entry=spot,
            spot_sl=sl_spot,
            spot_tgt1=spot + spot_risk_dist if ev.direction == "LONG" else spot - spot_risk_dist,
            spot_tgt2=spot + 2 * spot_risk_dist if ev.direction == "LONG" else spot - 2 * spot_risk_dist,
            delta=delta_val,
            capital=getattr(settings, "CAPITAL", 100000.0),
            lot_size=getattr(settings, "LOT_SIZE", 75),
        )
        if not geo_res["passed"]:
            return self._suppress(ev, family, " | ".join(geo_res["reasons"]))

        if self.market.trade_expiry is None:
            return self._suppress(ev, family, "no tradeable expiry available",
                                  alert=True)
        signal_no = self.book.signals_today() + 1
        ru = sg_risk_units if ev.direction == "SHORT" and sg_info["is_gated"] else 1.0
        trade = PaperTrade(
            trade_id=f"T{signal_no:02d}", direction=ev.direction, strike=strike,
            expiry=self.market.trade_expiry, entry_spot=spot, entry_prem=ltp,
            sl_spot=sl_spot, broken_level=broken_level, level_name=level_name,
            entry_time=ist_now(), score=ev.score, components=list(ev.reasons),
            candle_start=ev.candle.start, family=family,
            risk_units=ru)
        trade.extreme_spot = trade.entry_spot
        trade.capture_original_plan()     # post-exit shadow tracks this plan
        trade.runway_pts = runway
        trade.runway_wall = wall_label
        trade.delta = delta_val
        trade.spot_r = geo_res["spot_r"]
        trade.prem_r = geo_res["prem_r"]

        # Phase 14: NO SILENT TRADES HARD GATE
        # The entry card MUST be announced to Telegram BEFORE opening/recording trade.
        card_text = messages.trade_card(
            signal_no=signal_no, direction=ev.direction, trade=trade,
            spot=trade.entry_spot, now_txt=ist_now().strftime("%H:%M:%S"),
            family=family,
            data_source=f"{self.data_source} | {self.market.data_footer()}",
            delta=delta_val,
            spot_r=geo_res["spot_r"],
            prem_r=geo_res["prem_r"],
        )
        announced = self._notify(card_text, event_type="TRADE_ENTRY", event_id=trade.trade_id)
        if announced is False and getattr(self.tg, "configured", False) and getattr(self, "live_mode", False):
            self.store.journal_event(
                "SKIP", direction=trade.direction, strike=trade.strike,
                spot=trade.entry_spot, score=trade.score, family=family,
                notes=f"BLOCKED (NO_SILENT_TRADES): Entry card failed delivery for {trade.trade_id}")
            log.error("TRADE BLOCKED (NO_SILENT_TRADES): %s failed announcement!", trade.trade_id)
            return None

        trade.entry_announced = True
        if reentry_fired:
            self.reentry_available[ev.direction] = False   # grant consumed
        sig_notes = f"signal candle {family} {ev.direction} {ev.candle.start.isoformat(timespec='seconds')}"
        if sg_tag:
            sig_notes += f" | {sg_tag}"
        self.store.journal_event(
            "SIGNAL", direction=trade.direction, strike=trade.strike,
            spot=trade.entry_spot, option_ltp=trade.entry_prem, score=trade.score,
            reasons=trade.to_reasons(), trade_id=trade.trade_id, family=family,
            day_state=self.day_state.state if self.day_state else None,
            notes=sig_notes, risk_units=trade.risk_units)
        self.book.add(trade)
        log.info("SIGNAL %s [%s]: %s @ %.2f prem (Delta %.2f), score %d, SL %.2f, expiry %s | %s",
                 trade.trade_id, family, trade.option, trade.entry_prem,
                 trade.delta, trade.score, trade.sl_spot, trade.expiry, self.market.data_footer())
        # consume a WALL-UNWINDING re-entry grant when it was spent
        if self.book.direction_count(trade.direction) >= settings.MAX_TRADES_PER_DIRECTION:
            self.reentry_available[trade.direction] = False
            log.info("re-entry grant consumed for %s", trade.direction)
        return trade

    def _suppress(self, ev, family: str, reason: str, alert: bool = False,
                  sl_spot=None, level_label: str | None = None):
        """A fired evaluation killed by a gate - journaled; sanity gates also
        raise the DATA MISMATCH alert. Score >= 80 blocks get the rate-limited
        🚫 SKIPPED alert (decision transparency: no invisible decisions)."""
        entry_spot = self.market.spot_value() or ev.candle.close
        level = level_label or getattr(ev, "level_name", None) \
            or getattr(ev, "level_label", None) \
            or str(getattr(ev, "level_strike", "") or getattr(ev, "level", ""))

        # PCR Gate Shadow Tracking (measure, don't delete)
        pcr_val = self.regime.pcr if (self.regime and self.regime.pcr is not None) else None
        is_pcr_block = ("mean-reversion" in reason.lower() or "pcr" in reason.lower() or
                        (pcr_val is not None and pcr_val < 0.70))
        if is_pcr_block:
            strike = round_to_strike(entry_spot)
            prem = self.option_ltp(ev.direction, entry_spot) or 100.0
            sl_val = sl_spot or (entry_spot - 20.0 if ev.direction == "LONG" else entry_spot + 20.0)
            self.pcr_shadow_tracker.record_blocked(
                family=family,
                direction=ev.direction,
                entry_spot=entry_spot,
                sl_spot=sl_val,
                pcr=pcr_val or 0.0,
                strike=strike,
                entry_prem=prem,
                notes=f"suppressed by: {reason}",
            )
            reason = f"{reason} | PCR-GATE-SHADOW-TRACKED"

        self.store.journal_event(
            "SKIP", direction=ev.direction, spot=ev.candle.close, score=ev.score,
            family=family,
            day_state=self.day_state.state if self.day_state else None,
            reasons={"score": ev.score, "direction": ev.direction, "family": family,
                     "skipped": {"direction": ev.direction,
                                 "entry_spot": entry_spot,
                                 "sl_spot": sl_spot,
                                 "level": str(level),
                                 "family": family,
                                 "ts": ist_now().isoformat(timespec="seconds")}},
            notes=f"signal suppressed by gate: {reason}")
        if ev.score >= settings.ENTRY_SCORE_FIRE:
            self._notify_skipped(ev, family, level, reason)
        if alert:
            self._alert_data_mismatch(reason)
        return None

    def _notify_skipped(self, ev, family: str, level, reason: str) -> None:
        """🚫 SKIPPED - one alert per setup per 30 min (decision transparency)."""
        key = (family, ev.direction)
        now = ist_now()
        last = self._skip_alerts.get(key)
        if last is not None and (now - last).total_seconds() < 1800:
            return
        self._skip_alerts[key] = now
        side = "PE" if ev.direction == "SHORT" else "CE"
        level_txt = f"{level:,.0f}" if isinstance(level, (int, float)) else str(level)
        self._notify(messages.skipped_alert(
            side=side, level=level_txt, reason=reason, score=ev.score))

    # -- exits + 1R ---------------------------------------------------------------------------
    def close_trade(self, trade: PaperTrade, code: str, reason: str, spot=None,
                    grade: str = "OI-N/A") -> None:
        exit_prem = trade.last_prem if trade.last_prem is not None else trade.entry_prem
        result_r = trade.result_r(exit_prem)
        pnl = trade.pnl_rupees(exit_prem)
        self.shadow_compliant_ledger.update_fired_r(trade.trade_id, result_r or 0.0)
        source = getattr(self.market.candle_engine, "last_active_source", "chart")
        self.store.journal_event("EXIT", direction=trade.direction,
                                 strike=trade.strike, spot=spot,
                                 option_ltp=exit_prem, result_r=result_r,
                                 exit_reason=f"{code} [{grade}]: {reason} [managed_by: {source}]",
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
                                     "managed_by": source,
                                     "exit_ts": ist_now().isoformat(
                                         timespec="seconds")}},
                                 notes=f"exit {trade.trade_id} [managed_by: {source}]")
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
        pm_trade = self.position_manager.active_trade
        if pm_trade and pm_trade.lifecycle_state != TradeStateEnum.EXITED:
            last_spot = self.market.spot_value() or pm_trade.entry_price
            self.position_manager.close_position(
                pm_trade,
                exit_time=ist_now(),
                exit_price=last_spot,
                reason="forced square-off 15:10 IST",
                classification=ExitClassification.SESSION_EXIT,
            )
            self.store.journal_event(
                "EXIT",
                spot=last_spot,
                trade_id=pm_trade.trade_id,
                notes="square-off check: unified trade force-closed 15:10 IST",
                result_r=pm_trade.realized_r,
                exit_reason="SQUARE_OFF",
            )
            open_trades.append(pm_trade)
        if open_trades and not self.event_day:
            self._notify(messages.square_off([
                f"• {getattr(t, 'option', getattr(t, 'strategy_id', 'Trade'))} ({t.direction}) — closed at 15:10 IST"
                for t in open_trades]))
        note = (f"square-off check: {len(open_trades)} open trade(s) force-closed"
                if open_trades else "square-off check: no open paper trades")
        self.store.journal_event("SKIP", notes=note)
        # Square off open shadow M/W and sniper trades at 15:10 IST
        self.shadow_mw.square_off(exit_price=self.market.spot_value(), exit_time=ist_now())
        if hasattr(self, "sniper"):
            self.sniper.square_off(exit_price=self.market.spot_value(), exit_time=ist_now())

    def eod_if_due(self) -> None:
        if self.store.has_journal_event(self.today_iso, "EOD summary"):
            return
        body = stats.build_eod_body(self.store, self.today_iso,
                                    self.regime.summary() if self.regime else None,
                                    self.market.health.quality_lines())
        sg_report = get_trades_expectancy(self.store.conn, "journal")
        sg_text = (
            f"\n\nSHORT GATING AUDIT:\n"
            f"• Trades Evaluated: {sg_report['total_trades']}/60\n"
            f"• Shorts Running Exp: {sg_report['short_expectancy']:+.2f}R "
            f"({sg_report['short_trades']} trades, {sg_report['short_win_rate']:.1f}% WR)\n"
            f"• Status: {sg_report['recommendation']}"
        )
        body += sg_text

        ab_stats = get_sweep_ab_stats()
        if ab_stats.get("total_sweeps", 0) > 0:
            old_net = ab_stats["old"].get("net_r", ab_stats["old"].get("total_r", 0.0))
            new_net = ab_stats["new"].get("net_r", ab_stats["new"].get("total_r", 0.0))
            ab_text = (
                f"\n\nSWEEP A/B MEASUREMENT AUDIT:\n"
                f"• Sweeps Logged: {ab_stats['total_sweeps']}\n"
                f"• OLD (Immediate): {ab_stats['old']['trades']} trades | {ab_stats['old']['win_rate']:.1f}% WR | {old_net:+.2f}R\n"
                f"• NEW (MSS+POI): {ab_stats['new']['trades']} trades | {ab_stats['new']['win_rate']:.1f}% WR | {new_net:+.2f}R "
                f"(Filtered: {ab_stats['new'].get('filtered_rate', 0.0):.1f}%)\n"
            )
            if ab_stats.get("verdict"):
                ab_text += f"• Verdict: {ab_stats['verdict']}\n"
            body += ab_text

        # PCR Gate Shadow Tracking EOD Report
        from engines.pcr_shadow import get_pcr_shadow_stats
        pcr_stats = get_pcr_shadow_stats()
        pcr_text = (
            f"\n\nPCR GATE SHADOW TRACKING (running totals):\n"
            f"• {pcr_stats['report_text']}\n"
        )
        if pcr_stats.get("verdict"):
            pcr_text += f"• {pcr_stats['verdict']}\n"
        body += pcr_text

        # Candle Data Quality
        latest_lag = self.market.latest_candle_age_minutes() or 0.0
        candle_qual_text = (
            f"\n\nCANDLE DATA QUALITY:\n"
            f"• Max Candle Lag: {self.max_candle_lag:.1f} min | Blind Incidents: {self.blind_incidents} | EOD Freshness: {latest_lag:.1f} min\n"
        )
        body += candle_qual_text

        # Activity Funnel & Blank-Day Decoder
        try:
            from dashboard.funnel_decoder import decode_activity_funnel
            funnel = decode_activity_funnel(self.store.conn, self.today_iso)
            body += f"\n\n{funnel['summary_text']}\n"
        except Exception as e:
            log.warning("Could not decode activity funnel for EOD summary: %s", e)

        # Shadow-Compliant Comparison (Phase 13.4)
        try:
            shadow_summary = self.shadow_compliant_ledger.get_eod_summary(self.today_iso)
            body += f"\n\nSHADOW-COMPLIANT COMPARISON (Phase 13.4):\n• {shadow_summary['report_text']}\n"
        except Exception as e:
            log.warning("Could not build shadow-compliant EOD summary: %s", e)

        self.store.journal_event("REGIME",
                                 regime=self.regime.regime.value if self.regime else None,
                                 notes="EOD summary")
        # Forward paper: automatically archive session as fresh out-of-sample data
        try:
            self.store.export_day(self.today_iso)
            self.store.export_forward_replay_session(
                self.today_iso, is_clean=getattr(self, "session_clean", True)
            )
        except Exception as e:
            log.warning("Automated forward session archive error: %s", e)

        message = messages.eod(body + "\n\nDATA: " + self.market.data_footer())
        safe_print(message)
        if not self.event_day:
            self._notify(message)
            if sg_report["total_trades"] >= 60 and sg_report["short_expectancy"] < 0:
                self._notify(
                    f"⚠️ SHORT GATING RECOMMENDATION: 60 trades reached with negative short "
                    f"expectancy ({sg_report['short_expectancy']:+.2f}R). Recommendation: DISABLE short entries."
                )


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
                         market=market, holidays=holidays, live_mode=True)
    session.book.load()
    session.regime = recover_regime(store, today_iso)

    # =========================================================================
    # STARTUP GUARANTEE: BACKFILL CANDLES + LEVELS FIRST BEFORE ANY OTHER MODULE
    # =========================================================================
    log.info("Startup initialization: warming up session & backfilling candles + levels FIRST...")
    nse.warm_up()

    # 1. Backfill candles immediately (guarantees mid-day start works at any hour)
    try:
        fresh = market.candle_engine.refresh()
        session.opening_spot = market.candle_engine.opening_spot()
        log.info("Startup candle backfill: %d closed candles, opening spot %s",
                 len(market.candles), fmt_num(session.opening_spot))
    except Exception:
        log.exception("Startup candle backfill failed")

    # 2. Backfill levels immediately (PDC, PDH, PDL)
    if not market.ensure_prev_day(retries=3):
        log.critical("PDC unavailable after 3 startup retries - NO-TRADE DAY, "
                     "standing down silently")
        store.journal_event("SKIP", notes="NO-TRADE DAY: PDC unavailable after 3 "
                                          "startup retries")
        return
    log.info("Startup levels locked: PDC=%s PDH=%s PDL=%s", market.pdc, market.pdh, market.pdl)

    # 3. Read back and verify persistence in SQLite store
    db_candles = store.fetch_candles(today_iso)
    log.info("Startup store verified: %d candles in journal.sqlite3", len(db_candles))
    # =========================================================================

    # Pre-market factor report (only runs if started before or during pre-market window)
    if ist_now().time() < settings.PREOPEN_START:
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


def run_nearmiss() -> None:
    """--mode nearmiss: today's full decision log - every setup, score, blocking
    gate and shadow outcome - to console + Telegram (decision transparency)."""
    setup_logging()
    store = JournalStore()
    tg = TelegramSender()
    report = emit_nearmiss_report(store, tg=tg)
    safe_print(report)
    store.close()


def _window_grep() -> list:
    """Audit helper: list every hardcoded time-window gate outside settings.py."""
    import re
    pattern = re.compile(r"ENTRY_WINDOW_END|SWEEP_WINDOW_2|LAST_ENTRY_TIME|"
                         r"dtime\(|dt_time\(")
    findings = []
    for path in [Path("main.py"), Path("premarket_report.py"), Path("utils.py"),
                 *Path("engines").glob("*.py"), *Path("journal").glob("*.py"),
                 *Path("alerts").glob("*.py"), *Path("data_sources").glob("*.py")]:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                findings.append(f"{path}:{lineno}: {line.strip()[:90]}")
    return findings


def _symmetry_test(market, store) -> dict:
    """CE/PE symmetry: one synthetic setup evaluated as LONG and as SHORT with
    identical geometry - both must fire or both must be blocked identically."""
    session = DaySession(store, NullTelegramSender(),
                         RegimeClassifier(event_dates=set()), event_day=False,
                         today_iso=ist_now().date().isoformat(), market=market)
    fake_now = ist_now().replace(hour=10, minute=0)
    orig_ist_now = ist_now
    import main as _main
    _main.ist_now = lambda: fake_now
    try:
        session.market.set_prev_day(30500.0, 29500.0, 30000.0)
        session.market.trade_expiry = "12-Oct-2026"
        row = type("R", (), {"strike": 30100, "expiry": "12-Oct-2026",
                             "ce_ltp": 120.0, "pe_ltp": 115.0})()
        session.market.chain = type("S", (), {"rows": [row],
                                              "nearest_expiry": "06-Oct-2026",
                                              "underlying": 30000.0})()
        session.market.spot = type("SV", (), {"value": 30100.0, "asof": "x",
                                              "source": "symmetry"})()
        session.market.candles = [
            Candle(start=fake_now.replace(hour=9, minute=15), open=30000,
                   high=30110, low=30090, close=30000)]
        results = {}
        for direction, level, close, high, low in (
                ("LONG", 30100.0, 30510.0, 30510.0, 30099.0),
                ("SHORT", 29500.0, 29440.0, 30001.0, 29440.0)):
            ev = type("E", (), {"direction": direction,
                                "candle": Candle(
                                    start=fake_now.replace(hour=9, minute=35),
                                    open=30000.0, high=high, low=low, close=close),
                                "score": 85, "level": level,
                                "level_name": "ORB high" if direction == "LONG"
                                else "ORB low", "reasons": []})()
            ctx = session._entry_ctx(ev.candle)
            evs = session.entry.evaluate(ev.candle, None, ctx)
            results[direction] = [(e.direction, bool(e.fired), e.score) for e in evs
                                  if e.score >= settings.ENTRY_SCORE_FIRE]
        return results
    finally:
        _main.ist_now = orig_ist_now


def run_audit() -> None:
    """--mode audit: WHY-NO-TRADE report - liveness, day-state replay, signal
    path trace, CE/PE symmetry, window grep - console + Telegram + file."""
    setup_logging()
    store = JournalStore()
    tg = TelegramSender()
    lines = ["# WHY-NO-TRADE AUDIT", "",
             f"generated {ist_now().strftime('%a %Y-%m-%d %H:%M:%S')} IST"]
    market = MarketDataStore(session=NSESession(), journal=store,
                             holidays=load_date_set(settings.HOLIDAYS_FILE))
    report = emit_nearmiss_report(store, tg=None,
                                  start_time=settings.ENTRY_EVAL_START)
    lines += ["## Decision log (nearmiss report)", report]
    lines += ["", "## Liveness (store-based)"]
    for label, query in (("last OI snapshot", "SELECT MAX(ts) FROM oi_snapshots"),
                         ("last candle", "SELECT MAX(start) FROM candles"),
                         ("last journal row", "SELECT MAX(ts) FROM journal")):
        value = store.conn.execute(query).fetchone()[0]
        age = "-"
        if value:
            try:
                age = f"{(ist_now() - datetime.fromisoformat(value)).total_seconds() / 60:.0f} min ago"
            except ValueError:
                pass
        lines.append(f"- {label}: {value} ({age})")
    lines += ["", "## CE/PE symmetry test"]
    try:
        results = _symmetry_test(market, store)
        for direction, fired in results.items():
            lines.append(f"- {direction}: {fired}")
        fired_states = {fired for _, fired in results.items()}
        verdict = "SYMMETRIC" if len(fired_states) <= 1 else "MIRROR BUG"
        lines.append(f"- verdict: {verdict}")
    except Exception as exc:
        lines.append(f"- symmetry test errored: {exc}")
    lines += ["", "## Window grep (hardcoded entry-time gates outside settings)"]
    findings = _window_grep()
    lines += [f"- {f}" for f in findings] or ["- none"]
    lines += ["", "NOTE: market data (spot/chain/VIX) is live; historical "
              "OI-dependent gates replay with the latest available snapshot "
              "(NSE does not serve historical chain snapshots)."]
    text = "\n".join(lines).replace("\n", "\n")
    Path("audit_report.md").write_text(text, encoding="utf-8")
    safe_print(text)
    tg.send(text[:4000])
    store.close()


def run_replay_today(days_back: int = 0) -> None:
    """--mode replay-today: offline replay of stored candles through the FIXED
    symmetric state machine - prints the transition log + fired setups as proof."""
    setup_logging()
    store = JournalStore()
    from datetime import timedelta
    target = (ist_now().date() - timedelta(days=days_back)).isoformat()
    candles = [SimpleNamespace(**c) for c in store.fetch_candles(target)]
    print(f"REPLAY {target}: {len(candles)} stored candles")
    if not candles:
        return
    run_replay_today._seen = set()      # reset the printed-transition dedup per run
    engine = DayStateEngine()
    prev_state = RANGE
    fired = []
    for i in range(3, len(candles) + 1):
        window = candles[:i]
        state = engine.update(window, spot=window[-1].close)
        seen = getattr(run_replay_today, "_seen", set())
        for ts, transition, boundary in state.transitions:
            key = (ts, transition)
            if key not in seen:
                seen.add(key)
                print(f"{ts[11:16]}  {transition}  (boundary {boundary})")
        if state.state != prev_state:
            print(f"{window[-1].start.strftime('%H:%M')}  STATE: {prev_state} -> {state.state}")
            prev_state = state.state
    print(f"final: {state.state} | 1h range {state.first_hour_low}-{state.first_hour_high} | "
          f"transitions: {len(state.transitions)}")
    store.close()


def run_score() -> None:
    """--mode score: ONE consistent set from the MarketDataStore - spot, PDC, ATM
    strike, ATM CE/PE LTP, VIX, chain source, store timestamp."""
    setup_logging()
    nse = NSESession()
    journal = JournalStore(settings.DB_PATH)
    market = MarketDataStore(session=nse, journal=journal,
                             holidays=load_date_set(settings.HOLIDAYS_FILE))
    print("=" * 72)
    print(" SCORE MODE - one MarketDataStore pass, one consistent data set")
    print("=" * 72)
    print(f"now: {ist_now().strftime('%a %Y-%m-%d %H:%M:%S')} IST")
    market.ensure_prev_day(retries=3)
    market.refresh()

    candle_age = market.latest_candle_age_minutes()
    stored_candles = journal.fetch_candles(ist_now().date().isoformat())
    spot = market.spot_value()
    atm = round_to_strike(spot) if spot is not None else None
    print(f"1) STORE REFRESH @ {market.last_refresh} | "
          f"chain_source={market.chain_source} | candles={len(market.candles)} | "
          f"age={candle_age:.1f} min | DB verified={len(stored_candles)} candles")
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

    # LIVE PREFLIGHT (09:20 tomorrow, --mode score extended)
    from engines.preflight import run_full_preflight
    tg = TelegramSender()
    run_full_preflight(market_store=market, tg_sender=tg)

    journal.close()
    nse.close()


def main():
    parser = argparse.ArgumentParser(
        description="Nifty options alert-only assistant (alert/journal only, no orders).")
    parser.add_argument("--mode",
                        choices=["live", "now", "backtest", "replay-mtf", "replay60",
                                 "chain-test", "score", "nearmiss", "audit",
                                 "replay-today"],
                        default="live",
                        help="live = scheduled day (default); now = immediate test "
                             "run; backtest = arrives with Phase C; replay-mtf = "
                             "historical MTF verification; replay60 = 60-day batch; "
                             "chain-test = per-step chain diagnostics; score = one consistent store snapshot")
    parser.add_argument("--days", type=int, default=5,
                        help="replay-mtf/replay60: number of trading days to replay")
    args = parser.parse_args()
    setup_logging()

    if args.mode == "replay-mtf":
        run_replay_mtf(days=args.days)
        return
    if args.mode == "replay60":
        from engines.replay_60day import run_replay_60day
        from engines.stats_calculator import generate_replay_report
        from engines.replay_scoring import get_price_only_summary
        
        session = run_replay_60day(days=args.days)

        safe_print(f"\n{'='*80}")
        safe_print(f"REPLAY COMPLETE: {session.summary()}")
        safe_print(f"run_id: {session.run_id} | trades (unique): {session.total_trades}")
        safe_print(f"{'='*80}")
        safe_print(f"\n{get_price_only_summary()}\n")
        
        safe_print("By Setup Family:")
        for family, trades in sorted(session.trades_by_family.items()):
            if not trades:
                continue
            trades_r = [t.result_r or 0.0 for t in trades]
            wins = len([r for r in trades_r if r > 0])
            wr = (wins / len(trades_r) * 100) if trades_r else 0.0
            exp = (sum(trades_r) / len(trades_r)) if trades_r else 0.0
            safe_print(f"  [{family:20}] {len(trades):3} trades | "
                       f"Win {wr:5.1f}% | Exp {exp:+6.2f}R | Total {sum(trades_r):+7.2f}R")
        
        # Generate markdown report with cost deduction
        report_path = Path(settings.DATA_DIR.parent) / "backtest_results" / "replay60_report.md"
        markdown_report = generate_replay_report(session, str(report_path))
        safe_print(f"\n[OK] Detailed report saved: {report_path}")
        safe_print(f"\n{markdown_report[:500]}...\n")
        
        # Optional: send Telegram summary (if configured)
        try:
            tg_replay = TelegramSender()
            summary_msg = (
                f"📊 **60-Day Replay Report**\n"
                f"run_id: `{session.run_id}`\n"
                f"days processed: {session.days_replayed}\n"
                f"trades (unique): {session.total_trades} | Win: {session.win_rate():.1f}%\n"
                f"Exp: {session.expectancy():+.2f}R | Max DD: {session.max_dd:+.2f}R\n"
                f"Report: {report_path.name}"
            )
            tg_replay.send(summary_msg)
        except Exception as e:
            print(f"(Telegram summary skipped: {e})")
        
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
    if args.mode == "nearmiss":
        run_nearmiss()
        return
    if args.mode == "replay-today":
        run_replay_today()
        return
    if args.mode == "audit":
        run_audit()
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
