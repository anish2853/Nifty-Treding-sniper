"""Historical replay: MTF zone -> confirm -> entry vs plain ORB on the same days."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import settings
from engines.candles import Candle, orb_levels
from engines.coil_snipe import CoilSnipeEngine
from engines.day_state import DayStateEngine
from engines.level_runtime import LevelRuntime
from engines.entry import EntryEngine
from engines.order_blocks import OrderBlockEngine
from engines.short_gating import evaluate_short_gate
from engines.sweep_mss_poi import SweepMssPoiEngine
from engines.mtf_live import compute_mtf_tgt1, event_family, ready_confirmed_zone
from engines.mtf_scalp import EXPIRED, STALE, WAITING
from engines.replay_types import ReplayTrade, DayReplay
from utils import ist_now, safe_print

log = logging.getLogger("replay_mtf")


def _aggregate_five_minute(one_minute: list[Candle]) -> list[Candle]:
    buckets: dict = {}
    for bar in one_minute:
        minute = bar.start.minute
        bucket_start = bar.start.replace(minute=(minute // 5) * 5, second=0, microsecond=0)
        bucket = buckets.get(bucket_start)
        if bucket is None:
            buckets[bucket_start] = [bar.open, bar.high, bar.low, bar.close]
        else:
            bucket[1] = max(bucket[1], bar.high)
            bucket[2] = min(bucket[2], bar.low)
            bucket[3] = bar.close
    return [Candle(start=start, open=v[0], high=v[1], low=v[2], close=v[3],
                   interval_minutes=5)
            for start, v in sorted(buckets.items())]


def _find_gaps(one_minute: list[Candle]) -> list[str]:
    gaps = []
    for previous, current in zip(one_minute, one_minute[1:]):
        if current.start - previous.start > timedelta(minutes=1, seconds=30):
            gaps.append(f"{previous.end.isoformat()} -> {current.start.isoformat()}")
    return gaps


def _fetch_intraday(day: datetime.date) -> tuple[list[Candle], list[Candle], float | None,
                                                  float | None, float | None]:
    from data_sources.replay_data import get_day_candles
    return get_day_candles(day)


def _simulate_exit(trade: ReplayTrade, bar: Candle, square_off: datetime) -> bool:
    """Simulate exit with intrabar SL enforcement (no trade loses > -1.1R).
    
    Rules:
    1. SL is checked INTRABAR: if bar range crosses SL, exit AT SL + 2pts slippage
    2. TGT1 checked on close
    3. Square-off at 15:10 on close
    4. Result_r capped at -1.1R hard limit (no loss exceeds this)
    """
    if trade.exit_time is not None:
        return True
    
    stop = trade.sl
    tgt1 = compute_mtf_tgt1(trade.entry, trade.sl, trade.direction, [])
    SLIPPAGE_PTS = 2
    
    # Intrabar SL enforcement (highest priority)
    if trade.direction == "LONG":
        # SL below entry; check if bar.low touches or goes below SL
        if bar.low <= stop:
            # Intrabar SL hit: exit at SL + 2pts slippage
            trade.exit_spot = stop + SLIPPAGE_PTS
            trade.exit_time = bar.end
            trade.exit_reason = "SL"
        # TGT1: check close against target
        elif bar.close >= tgt1:
            trade.exit_time, trade.exit_spot = bar.end, bar.close
            trade.exit_reason = "TGT1"
        # Square-off: forced exit at 15:10
        elif bar.end >= square_off:
            trade.exit_time, trade.exit_spot = bar.end, bar.close
            trade.exit_reason = "SQUARE_OFF"
    else:  # SHORT
        # SL above entry; check if bar.high touches or goes above SL
        if bar.high >= stop:
            # Intrabar SL hit: exit at SL - 2pts slippage
            trade.exit_spot = stop - SLIPPAGE_PTS
            trade.exit_time = bar.end
            trade.exit_reason = "SL"
        # TGT1: check close against target
        elif bar.close <= tgt1:
            trade.exit_time, trade.exit_spot = bar.end, bar.close
            trade.exit_reason = "TGT1"
        # Square-off: forced exit at 15:10
        elif bar.end >= square_off:
            trade.exit_time, trade.exit_spot = bar.end, bar.close
            trade.exit_reason = "SQUARE_OFF"
    
    # Calculate R value with hard -1.1R cap
    if trade.exit_time is not None:
        risk = abs(trade.entry - trade.sl)
        move = ((trade.exit_spot - trade.entry) if trade.direction == "LONG"
                else (trade.entry - trade.exit_spot))
        trade.result_r = move / risk if risk else None
        
        # Hard cap: no loss exceeds -1.1R (SL enforcement rule)
        if trade.result_r is not None and trade.result_r < -1.1:
            trade.result_r = -1.1
    
    return trade.exit_time is not None


def _replay_day(day: datetime.date, day_data: Any = None) -> DayReplay | None:
    if day_data is not None:
        one_minute = day_data.one_minute
        five_minute = day_data.five_minute
        pdh, pdl, pdc = day_data.pdh, day_data.pdl, day_data.pdc
    else:
        one_minute, five_minute, pdh, pdl, pdc = _fetch_intraday(day)
    if not five_minute:
        return None
    result = DayReplay(date=day.isoformat(), gaps=_find_gaps(one_minute))
    day_state_engine = DayStateEngine()
    runtime = LevelRuntime()
    coil_engine = CoilSnipeEngine()
    ob_engine = OrderBlockEngine()
    sweep_reversal_engine = SweepMssPoiEngine()
    open_trades: list[ReplayTrade] = []
    square_off = datetime.combine(day, settings.SQUARE_OFF_TIME, tzinfo=settings.IST)
    minute_index = {c.start: i for i, c in enumerate(one_minute)}
    
    # DEDUPLICATION & CAP TRACKING
    fired_zone_ids: set[str] = set()
    fired_trade_keys: set[tuple] = set()
    daily_trade_count = 0
    last_entry_time: dict[str, datetime] = {}
    trades_per_direction = {"LONG": 0, "SHORT": 0}

    def _trade_key(family: str, direction: str, entry_time: datetime, entry_price: float):
        minute = entry_time.replace(second=0, microsecond=0).isoformat(timespec="minutes")
        return (family, direction, minute, round(float(entry_price), 2))

    def _register_fire(family: str, direction: str, entry_time: datetime,
                       entry_price: float, zone_id: str | None = None) -> bool:
        nonlocal daily_trade_count
        key = _trade_key(family, direction, entry_time, entry_price)
        if key in fired_trade_keys:
            return False
        fired_trade_keys.add(key)
        if zone_id:
            fired_zone_ids.add(zone_id)
        daily_trade_count += 1
        trades_per_direction[direction] += 1
        last_entry_time[direction] = entry_time
        return True

    class MarketStub:
        def __init__(self):
            self.candles = []
            self.pdh, self.pdl, self.pdc = pdh, pdl, pdc
            self.liquidity = None

        def spot_value(self):
            return self.candles[-1].close if self.candles else None

    market = MarketStub()

    for candle in five_minute:
        market.candles = [c for c in five_minute if c.end <= candle.end]
        day_state = day_state_engine.update(market.candles, spot=candle.close, pdh=pdh, pdl=pdl)
        runtime.refresh_grid(market, day_state)
        coil_engine.update_coil(market.candles, candle.close, day_state.state,
                                day_state.vwap, runtime.grid)

        # Detect Order Blocks on 5m breaks of key levels
        levels_to_check = []
        if pdh is not None:
            levels_to_check.append((pdh, "PDH"))
        if pdl is not None:
            levels_to_check.append((pdl, "PDL"))
        if pdh is not None and pdl is not None:
            levels_to_check.append(((pdh + pdl) / 2.0, "EQ"))
        orb_levels_map = orb_levels(market.candles)
        if orb_levels_map:
            levels_to_check.extend([(orb_levels_map["high"], "ORB-H"), (orb_levels_map["low"], "ORB-L")])
        ob_engine.detect_order_blocks(candle, market.candles, levels_to_check)

        for trade in list(open_trades):
            _simulate_exit(trade, candle, square_off)

        start_min = candle.start
        end_min = candle.end
        session_minutes = [m for m in one_minute
                           if start_min <= m.start < end_min]
        for minute in session_minutes:
            idx = minute_index.get(minute.start)
            previous = one_minute[idx - 1] if idx and idx > 0 else None
            for event in runtime.minute_close(minute, previous, day_state):
                if event.zone is not None:
                    runtime.zones[event.zone.zone_id] = event.zone
                    result.zone_log.append(
                        f"{minute.end:%H:%M} {event.kind} {event.zone.zone_id} "
                        f"trigger {event.zone.trigger:.2f} stop {event.zone.stop:.2f}")
                elif event.kind == "LEVEL-WATCH":
                    result.zone_log.append(f"{minute.end:%H:%M} {event.message}")
            for coil_event in coil_engine.on_minute(minute):
                if coil_event.fade_zone is not None:
                    zone = coil_event.fade_zone
                    runtime.zones[zone.zone_id] = zone

            # Order Block Retest signals on 1m bars
            for ob_sig in ob_engine.on_minute(minute):
                if daily_trade_count >= settings.MAX_TRADES_PER_DAY:
                    continue
                if trades_per_direction[ob_sig.direction] >= settings.MAX_TRADES_PER_DIRECTION:
                    continue
                cooldown_mins = 20 if day_state.state in ("TREND-UP", "TREND-DOWN") else 30
                if ob_sig.direction in last_entry_time:
                    elapsed = (minute.end - last_entry_time[ob_sig.direction]).total_seconds() / 60
                    if elapsed < cooldown_mins:
                        continue

                gate_ok, risk_units, tag, block_reason = evaluate_short_gate(
                    ob_sig.direction, 90, level_name=ob_sig.ob.level_name, is_gated=True
                )
                if not gate_ok:
                    result.zone_log.append(f"{minute.end:%H:%M} skip {ob_sig.ob.ob_id}: {block_reason}")
                    continue

                if not _register_fire("OB-RETEST", ob_sig.direction, minute.end,
                                      ob_sig.entry_spot, ob_sig.ob.ob_id):
                    continue

                notes_txt = f"OB-RETEST {ob_sig.ob.side} level:{ob_sig.ob.level_name}"
                if tag:
                    notes_txt += f" | {tag}"

                open_trades.append(ReplayTrade(
                    day=result.date, family="OB-RETEST", direction=ob_sig.direction,
                    entry_time=minute.end, entry=float(ob_sig.entry_spot),
                    sl=float(ob_sig.sl_spot), trigger=float(ob_sig.ob.broken_level),
                    zone_id=ob_sig.ob.ob_id, notes=notes_txt, risk_units=risk_units
                ))
                result.zone_log.append(
                    f"{minute.end:%H:%M} ENTRY {ob_sig.ob.ob_id} {ob_sig.direction} "
                    f"@ {ob_sig.entry_spot:.2f} SL {ob_sig.sl_spot:.2f} (OB-RETEST)")

            # SWEEP -> MSS -> POI Reversal signals on 1m bars
            for rev_setup in sweep_reversal_engine.update_retest_and_entry(minute, ctx={"score": 90}):
                if daily_trade_count >= settings.MAX_TRADES_PER_DAY:
                    continue
                if trades_per_direction[rev_setup.direction] >= settings.MAX_TRADES_PER_DIRECTION:
                    continue
                cooldown_mins = 20 if day_state.state in ("TREND-UP", "TREND-DOWN") else 30
                if rev_setup.direction in last_entry_time:
                    elapsed = (minute.end - last_entry_time[rev_setup.direction]).total_seconds() / 60
                    if elapsed < cooldown_mins:
                        continue

                gate_ok, risk_units, tag, block_reason = evaluate_short_gate(
                    rev_setup.direction, 90, level_name=rev_setup.pool_name, is_gated=True
                )
                if not gate_ok:
                    result.zone_log.append(f"{minute.end:%H:%M} skip {rev_setup.setup_id}: {block_reason}")
                    continue

                if not _register_fire("SWEEP-MSS-POI", rev_setup.direction, minute.end,
                                      rev_setup.entry_price, rev_setup.setup_id):
                    continue

                poi_lbl = rev_setup.active_poi.label if rev_setup.active_poi else ""
                notes_txt = f"SWEEP-MSS-POI pool:{rev_setup.pool_name} POI:{poi_lbl}"
                if tag:
                    notes_txt += f" | {tag}"

                open_trades.append(ReplayTrade(
                    day=result.date, family="SWEEP-MSS-POI", direction=rev_setup.direction,
                    entry_time=minute.end, entry=float(rev_setup.entry_price),
                    sl=float(rev_setup.sl_price), trigger=float(rev_setup.mss_level or rev_setup.entry_price),
                    zone_id=rev_setup.setup_id, notes=notes_txt, risk_units=risk_units
                ))
                result.zone_log.append(
                    f"{minute.end:%H:%M} ENTRY {rev_setup.setup_id} {rev_setup.direction} "
                    f"@ {rev_setup.entry_price:.2f} SL {rev_setup.sl_price:.2f} (SWEEP-MSS-POI)")

            # A/B exit tracking for active sweep setups at 1-min resolution
            sweep_reversal_engine.update_exits(minute, square_off)

        events = runtime.five_minute_close(candle, market.candles)
        sweep_reversal_engine.update_5m(market.candles, ctx={"pdh": pdh, "pdl": pdl})
        sweep_reversal_engine.update_exits(candle, square_off)
        for zone in runtime.zones.values():
            if zone.zone_id in fired_zone_ids:
                continue
            if zone.status == WAITING:
                continue
            if zone.status in (EXPIRED, STALE):
                result.zone_log.append(
                    f"{candle.end:%H:%M} zone {zone.zone_id} {zone.status}")
            if ready_confirmed_zone(zone, candle.end):
                # REQUIREMENT 4b: GEOMETRY GATE - SL >= 20pts AND target >= 1.5x SL
                stop = zone.stop
                entry = zone.entry_spot
                if entry is None:
                    continue
                risk_pts = entry - stop if zone.direction == "LONG" else stop - entry
                if risk_pts < 20:
                    result.zone_log.append(
                        f"{candle.end:%H:%M} skip {zone.zone_id}: risk {risk_pts:.0f} < 20pts")
                    continue
                # Target check: assume TGT1 = 1.5x SL for confirmation
                tgt = entry + (1.5 * risk_pts) if zone.direction == "LONG" else entry - (1.5 * risk_pts)
                runway, nearest = runtime.runway(zone.entry_spot, zone.direction)
                if runway is not None and runway < settings.ROOM_TO_RUN_MIN_PTS:
                    result.zone_log.append(
                        f"{candle.end:%H:%M} skip {zone.zone_id}: runway {runway:.0f}")
                    continue
                width = runtime.range_width(zone.entry_spot)
                if width is not None and width < settings.RANGE_WIDTH_MIN_PTS:
                    continue
                family = ("LEVEL-FADE" if zone.zone_id.startswith("FADE-")
                          else "MTF-SCALP")
                entry_time = zone.confirm_time or candle.end
                entry_price = float(zone.entry_spot)

                if daily_trade_count >= settings.MAX_TRADES_PER_DAY:
                    result.zone_log.append(
                        f"{candle.end:%H:%M} skip {zone.zone_id}: daily cap "
                        f"({daily_trade_count}/{settings.MAX_TRADES_PER_DAY})")
                    continue
                if trades_per_direction[zone.direction] >= settings.MAX_TRADES_PER_DIRECTION:
                    continue
                cooldown_mins = 20 if day_state.state in ("TREND-UP", "TREND-DOWN") else 30
                if zone.direction in last_entry_time:
                    elapsed = (entry_time - last_entry_time[zone.direction]).total_seconds() / 60
                    if elapsed < cooldown_mins:
                        continue

                gate_ok, risk_units, tag, block_reason = evaluate_short_gate(
                    zone.direction, 90, level_name=zone.zone_id, is_gated=True
                )
                if not gate_ok:
                    result.zone_log.append(f"{candle.end:%H:%M} skip {zone.zone_id}: {block_reason}")
                    continue

                if not _register_fire(family, zone.direction, entry_time, entry_price,
                                      zone.zone_id):
                    result.zone_log.append(
                        f"{candle.end:%H:%M} skip {zone.zone_id}: duplicate entry")
                    continue

                zone.status = "FIRED"
                notes_str = f"confirm {entry_time:%H:%M} state:{day_state.state}"
                if tag:
                    notes_str += f" | {tag}"
                open_trades.append(ReplayTrade(
                    day=result.date, family=family, direction=zone.direction,
                    entry_time=entry_time, entry=float(entry_price), sl=float(zone.stop),
                    trigger=float(zone.trigger), zone_id=zone.zone_id,
                    notes=notes_str, risk_units=risk_units))
                result.zone_log.append(
                    f"{candle.end:%H:%M} ENTRY {zone.zone_id} {zone.direction} "
                    f"@ {entry_price:.2f} SL {zone.stop:.2f}")

        for event in events:
            family = event_family(event)
            if family == "LEVEL-CONT" and event.direction:
                runway, _nearest = runtime.runway(candle.close, event.direction)
                if runway is None or runway < settings.ROOM_TO_RUN_MIN_PTS:
                    continue
                
                if daily_trade_count >= settings.MAX_TRADES_PER_DAY:
                    continue
                if trades_per_direction[event.direction] >= settings.MAX_TRADES_PER_DIRECTION:
                    continue
                cooldown_mins = 20 if day_state.state in ("TREND-UP", "TREND-DOWN") else 30
                if event.direction in last_entry_time:
                    elapsed = (candle.end - last_entry_time[event.direction]).total_seconds() / 60
                    if elapsed < cooldown_mins:
                        continue

                gate_ok, risk_units, tag, block_reason = evaluate_short_gate(
                    event.direction, 90, level_name=event.label, is_gated=True
                )
                if not gate_ok:
                    continue

                if not _register_fire(family, event.direction, candle.end,
                                      float(candle.close)):
                    continue
                notes_str = f"{event.message} state:{day_state.state}"
                if tag:
                    notes_str += f" | {tag}"
                open_trades.append(ReplayTrade(
                    day=result.date, family=family, direction=event.direction,
                    entry_time=candle.end, entry=float(candle.close),
                    sl=float(event.price), trigger=float(event.price),
                    notes=notes_str, risk_units=risk_units))
            if event.kind.startswith("DIVERGENCE") or event.kind.startswith("ZONE-"):
                result.zone_log.append(f"{candle.end:%H:%M} {event.kind} {event.message}")

        div_suppressed = any(event.kind == "DIVERGENCE-TRAP-SKIPPED" for event in events)
        for coil_event in coil_engine.on_five_minute(candle, runtime.grid, div_suppressed):
            if coil_event.kind == "ENTRY" and coil_event.entry is not None:
                if daily_trade_count >= settings.MAX_TRADES_PER_DAY:
                    continue
                if trades_per_direction[coil_event.direction] >= settings.MAX_TRADES_PER_DIRECTION:
                    continue
                cooldown_mins = 20 if day_state.state in ("TREND-UP", "TREND-DOWN") else 30
                if coil_event.direction in last_entry_time:
                    elapsed = (candle.end - last_entry_time[coil_event.direction]).total_seconds() / 60
                    if elapsed < cooldown_mins:
                        continue

                gate_ok, risk_units, tag, block_reason = evaluate_short_gate(
                    coil_event.direction, 90, level_name="COIL", is_gated=True
                )
                if not gate_ok:
                    continue

                if not _register_fire("COIL-SNIPE", coil_event.direction, candle.end,
                                      float(coil_event.entry)):
                    continue
                notes_str = coil_event.message
                if tag:
                    notes_str += f" | {tag}"
                open_trades.append(ReplayTrade(
                    day=result.date, family="COIL-SNIPE",
                    direction=coil_event.direction,
                    entry_time=candle.end, entry=float(coil_event.entry),
                    sl=float(coil_event.stop), trigger=float(coil_event.coil.high
                    if coil_event.direction == "LONG" else coil_event.coil.low),
                    notes=notes_str, risk_units=risk_units))

    for trade in open_trades:
        if trade.exit_time is None and five_minute:
            _simulate_exit(trade, five_minute[-1], square_off)
        if trade.exit_time is None and five_minute:
            last_bar = five_minute[-1]
            trade.exit_time = last_bar.end
            trade.exit_spot = last_bar.close
            trade.exit_reason = "EOD_SQUARE_OFF"
            risk = abs(trade.entry - trade.sl)
            move = ((trade.exit_spot - trade.entry) if trade.direction == "LONG"
                    else (trade.entry - trade.exit_spot))
            trade.result_r = move / risk if risk else 0.0
            if trade.result_r < -1.1:
                trade.result_r = -1.1
        result.mtf_trades.append(trade)

    # Final EOD sweep reversal engine square-off
    if five_minute:
        sweep_reversal_engine.on_end_of_day(five_minute[-1])

    entry_engine = EntryEngine(store=None)
    orb_ctx = {
        "snapshot": None, "spot": None, "vix_spike": False, "event_day": False,
        "regime_allows": lambda _d, _l: (True, ""),
        "caps_ok": lambda _d: (True, ""),
        "option_ltp": lambda _d, _s: 100.0,
        "candle_engine": None, "orb": None, "pdh": pdh, "pdl": pdl,
        "day_state": None, "vwap": None, "vwap_proxy": False, "now": ist_now(),
    }
    history = []
    
    # Import scoring check for replay mode (price-only threshold: 52/65)
    from engines.replay_scoring import should_fire_price_only
    
    for candle in five_minute:
        history.append(candle)
        orb_ctx["orb"] = orb_levels(history)
        orb_ctx["spot"] = candle.close
        prev = history[-2] if len(history) > 1 else None
        for ev in entry_engine.evaluate(candle, prev, orb_ctx):
            # Apply price-only scoring threshold (80% of available 65 pts = 52)
            if ev.fired and should_fire_price_only(ev.score):
                sl = orb_ctx["orb"]["mid"] if orb_ctx["orb"] else candle.close
                result.orb_signals.append({
                    "time": candle.end.isoformat(timespec="seconds"),
                    "direction": ev.direction,
                    "level": ev.level_name,
                    "close": candle.close,
                    "sl": sl,
                    "score": ev.score,
                })
    return result


def _summary(trades: list[ReplayTrade]) -> dict:
    closed = [t for t in trades if t.result_r is not None]
    if not closed:
        return {"count": 0, "win_rate": "—", "avg_r": 0.0}
    wins = sum(1 for t in closed if t.result_r > 0)
    avg_r = sum(t.result_r for t in closed) / len(closed)
    return {"count": len(closed), "win_rate": f"{100 * wins / len(closed):.0f}%",
            "avg_r": avg_r}


def run_replay_mtf(days: int = 5, journal=None) -> dict:
    """Pull recent 1m history (yfinance ~7d cap), replay MTF stack, print report."""
    from journal.replay_store import ReplayJournal

    if journal is None:
        journal = ReplayJournal()
        journal.begin_run("replay-mtf", days)
        own_journal = True
    else:
        own_journal = False

    today = ist_now().date()
    trading_days = []
    cursor = today
    while len(trading_days) < days and (today - cursor).days <= 14:
        if cursor.weekday() < 5:
            trading_days.append(cursor)
        cursor -= timedelta(days=1)

    safe_print("=" * 72)
    safe_print(f" REPLAY-MTF — last {days} session(s) | run_id {journal.run_id}")
    safe_print("=" * 72)
    all_mtf: list[ReplayTrade] = []
    all_orb: list[dict] = []
    for day_num, day in enumerate(reversed(trading_days), 1):
        replay = _replay_day(day)
        if replay is None:
            safe_print(f"\n{day.isoformat()}: no intraday data")
            continue
        safe_print(f"\n--- {replay.date} ---")
        if replay.gaps:
            safe_print(f"  data gaps ({len(replay.gaps)}):")
            for gap in replay.gaps[:5]:
                safe_print(f"    • {gap}")
        for line in replay.zone_log[:40]:
            safe_print(f"  {line}")
        if len(replay.zone_log) > 40:
            safe_print(f"  ... {len(replay.zone_log) - 40} more zone events")
        for trade in replay.mtf_trades:
            exit_txt = (f"{trade.exit_time:%H:%M} {trade.exit_reason} "
                        f"R={trade.result_r:+.2f}"
                        if trade.exit_time and trade.result_r is not None else "open")
            safe_print(
                f"  TRADE [{trade.family}] {trade.direction} entry {trade.entry_time:%H:%M} "
                f"@ {trade.entry:.2f} SL {trade.sl:.2f} | exit {exit_txt}")
            journal.record_trade(
                replay_date=replay.date, day_num=day_num, family=trade.family,
                direction=trade.direction, entry_time=trade.entry_time,
                entry_price=trade.entry, sl_spot=trade.sl, sl_prem=trade.sl_prem,
                exit_time=trade.exit_time, exit_price=trade.exit_spot,
                exit_reason=trade.exit_reason, result_r=trade.result_r,
                zone_id=trade.zone_id or None, notes=trade.notes)
        for sig in replay.orb_signals:
            safe_print(f"  ORB {sig['direction']} {sig['level']} close {sig['close']:.2f} "
                       f"@ {sig['time'][11:16]}")
        all_mtf.extend(replay.mtf_trades)
        all_orb.extend(replay.orb_signals)

    if own_journal:
        from journal.replay_store import print_run_summary, stats_for_run
        journal.finish_run()
        csv_path = journal.export_csv()
        safe_print(f"\nexport: {csv_path}")
        print_run_summary(journal.conn, journal.run_id)
        stats = stats_for_run(journal.conn, journal.run_id)
        journal.close()
        return {"run_id": journal.run_id, "trades": stats["trades_unique"], "orb_signals": len(all_orb)}

    mtf_stats = _summary(all_mtf)
    safe_print("\n" + "=" * 72)
    safe_print(" MTF stack (zones + level engine + coil) ")
    safe_print(f"  trades {mtf_stats['count']} | win rate {mtf_stats['win_rate']} | "
               f"avg R {mtf_stats['avg_r']:+.2f}")
    safe_print(" Plain 5m ORB (same days, price-only threshold in replay)")
    safe_print(f"  signals {len(all_orb)}")
    safe_print("=" * 72)
    return {"run_id": getattr(journal, "run_id", None), "trades": len(all_mtf), "orb_signals": len(all_orb)}
