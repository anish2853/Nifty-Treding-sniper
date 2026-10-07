"""Historical replay: MTF zone -> confirm -> entry vs plain ORB on the same days."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import settings
from engines.candles import Candle, orb_levels
from engines.coil_snipe import CoilSnipeEngine
from engines.day_state import DayStateEngine
from engines.level_runtime import LevelRuntime
from engines.mtf_live import compute_mtf_tgt1, event_family, ready_confirmed_zone
from engines.mtf_scalp import EXPIRED, STALE, WAITING
from engines.entry import EntryEngine
from utils import ist_now, safe_print

log = logging.getLogger("replay_mtf")


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


@dataclass
class DayReplay:
    date: str
    gaps: list[str] = field(default_factory=list)
    mtf_trades: list[ReplayTrade] = field(default_factory=list)
    orb_signals: list[dict] = field(default_factory=list)
    zone_log: list[str] = field(default_factory=list)


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
    import yfinance as yf

    ticker = yf.Ticker("^NSEI")
    start = day.isoformat()
    end = (day + timedelta(days=1)).isoformat()
    frame = ticker.history(start=start, end=end, interval="1m")
    if frame.empty:
        return [], [], None, None, None
    one_minute = []
    for ts, row in frame.iterrows():
        try:
            start_ts = ts.tz_convert(settings.IST)
        except (TypeError, AttributeError):
            continue
        if start_ts.date() != day:
            continue
        volume = row.get("Volume")
        one_minute.append(Candle(
            start=start_ts, open=float(row["Open"]), high=float(row["High"]),
            low=float(row["Low"]), close=float(row["Close"]),
            volume=float(volume) if volume and volume > 0 else None,
            source="yfinance-replay", interval_minutes=1))
    one_minute.sort(key=lambda c: c.start)
    daily = ticker.history(start=(day - timedelta(days=10)).isoformat(),
                           end=(day + timedelta(days=1)).isoformat(), interval="1d")
    pdh = pdl = pdc = None
    if len(daily) >= 2:
        prior = daily.iloc[-2]
        pdc = float(prior["Close"])
        pdh = float(prior["High"])
        pdl = float(prior["Low"])
    return one_minute, _aggregate_five_minute(one_minute), pdh, pdl, pdc


def _simulate_exit(trade: ReplayTrade, bar: Candle, square_off: datetime) -> bool:
    if trade.exit_time is not None:
        return True
    stop = trade.sl
    tgt1 = compute_mtf_tgt1(trade.entry, trade.sl, trade.direction, [])
    if trade.direction == "LONG":
        if bar.close <= stop:
            trade.exit_time, trade.exit_spot = bar.end, bar.close
            trade.exit_reason = "SL"
        elif bar.close >= tgt1:
            trade.exit_time, trade.exit_spot = bar.end, bar.close
            trade.exit_reason = "TGT1"
        elif bar.end >= square_off:
            trade.exit_time, trade.exit_spot = bar.end, bar.close
            trade.exit_reason = "SQUARE_OFF"
    else:
        if bar.close >= stop:
            trade.exit_time, trade.exit_spot = bar.end, bar.close
            trade.exit_reason = "SL"
        elif bar.close <= tgt1:
            trade.exit_time, trade.exit_spot = bar.end, bar.close
            trade.exit_reason = "TGT1"
        elif bar.end >= square_off:
            trade.exit_time, trade.exit_spot = bar.end, bar.close
            trade.exit_reason = "SQUARE_OFF"
    if trade.exit_time is not None:
        risk = abs(trade.entry - trade.sl)
        move = ((trade.exit_spot - trade.entry) if trade.direction == "LONG"
                else (trade.entry - trade.exit_spot))
        trade.result_r = move / risk if risk else None
    return trade.exit_time is not None


def _replay_day(day: datetime.date) -> DayReplay | None:
    one_minute, five_minute, pdh, pdl, pdc = _fetch_intraday(day)
    if not five_minute:
        return None
    result = DayReplay(date=day.isoformat(), gaps=_find_gaps(one_minute))
    day_state_engine = DayStateEngine()
    runtime = LevelRuntime()
    coil_engine = CoilSnipeEngine()
    open_trades: list[ReplayTrade] = []
    square_off = datetime.combine(day, settings.SQUARE_OFF_TIME, tzinfo=settings.IST)
    minute_index = {c.start: i for i, c in enumerate(one_minute)}

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
        day_state = day_state_engine.update(market.candles, pdh, pdl, candle.close)
        runtime.refresh_grid(market, day_state)
        coil_engine.update_coil(market.candles, candle.close, day_state.state,
                                day_state.vwap, runtime.grid)
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

        events = runtime.five_minute_close(candle, market.candles)
        for zone in runtime.zones.values():
            if zone.status == WAITING:
                continue
            if zone.status in (EXPIRED, STALE):
                result.zone_log.append(
                    f"{candle.end:%H:%M} zone {zone.zone_id} {zone.status}")
            if ready_confirmed_zone(zone, candle.end):
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
                open_trades.append(ReplayTrade(
                    day=result.date, family=family, direction=zone.direction,
                    entry_time=zone.confirm_time or candle.end,
                    entry=float(zone.entry_spot), sl=float(zone.stop),
                    trigger=float(zone.trigger), zone_id=zone.zone_id,
                    notes=f"confirm {zone.confirm_time:%H:%M}"))
                result.zone_log.append(
                    f"{candle.end:%H:%M} ENTRY {zone.zone_id} {zone.direction} "
                    f"@ {zone.entry_spot:.2f} SL {zone.stop:.2f}")

        for event in events:
            family = event_family(event)
            if family == "LEVEL-CONT" and event.direction:
                runway, _nearest = runtime.runway(candle.close, event.direction)
                if runway is None or runway < settings.ROOM_TO_RUN_MIN_PTS:
                    continue
                open_trades.append(ReplayTrade(
                    day=result.date, family=family, direction=event.direction,
                    entry_time=candle.end, entry=float(candle.close),
                    sl=float(event.price), trigger=float(event.price),
                    notes=event.message))
            if event.kind.startswith("DIVERGENCE") or event.kind.startswith("ZONE-"):
                result.zone_log.append(f"{candle.end:%H:%M} {event.kind} {event.message}")

        div_suppressed = any(event.kind == "DIVERGENCE-TRAP-SKIPPED" for event in events)
        for coil_event in coil_engine.on_five_minute(candle, runtime.grid, div_suppressed):
            if coil_event.kind == "ENTRY" and coil_event.entry is not None:
                open_trades.append(ReplayTrade(
                    day=result.date, family="COIL-SNIPE",
                    direction=coil_event.direction,
                    entry_time=candle.end, entry=float(coil_event.entry),
                    sl=float(coil_event.stop), trigger=float(coil_event.coil.high
                    if coil_event.direction == "LONG" else coil_event.coil.low),
                    notes=coil_event.message))

    for trade in open_trades:
        if trade.exit_time is None and five_minute:
            _simulate_exit(trade, five_minute[-1], square_off)
        result.mtf_trades.append(trade)

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
    for candle in five_minute:
        history.append(candle)
        orb_ctx["orb"] = orb_levels(history)
        orb_ctx["spot"] = candle.close
        prev = history[-2] if len(history) > 1 else None
        for ev in entry_engine.evaluate(candle, prev, orb_ctx):
            if ev.fired:
                sl = orb_ctx["orb"]["mid"] if orb_ctx["orb"] else candle.close
                result.orb_signals.append({
                    "time": candle.end.isoformat(timespec="seconds"),
                    "direction": ev.direction,
                    "level": ev.level_name,
                    "close": candle.close,
                    "sl": sl,
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


def run_replay_mtf(days: int = 5) -> None:
    """Pull recent 1m history (yfinance ~7d cap), replay MTF stack, print report."""
    today = ist_now().date()
    trading_days = []
    cursor = today
    while len(trading_days) < days and (today - cursor).days <= 14:
        if cursor.weekday() < 5:
            trading_days.append(cursor)
        cursor -= timedelta(days=1)

    safe_print("=" * 72)
    safe_print(f" REPLAY-MTF — last {days} session(s) | yfinance 1m (7-day API cap)")
    safe_print("=" * 72)
    all_mtf: list[ReplayTrade] = []
    all_orb: list[dict] = []
    for day in reversed(trading_days):
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
        for sig in replay.orb_signals:
            safe_print(f"  ORB {sig['direction']} {sig['level']} close {sig['close']:.2f} "
                       f"@ {sig['time'][11:16]}")
        all_mtf.extend(replay.mtf_trades)
        all_orb.extend(replay.orb_signals)

    mtf_stats = _summary(all_mtf)
    safe_print("\n" + "=" * 72)
    safe_print(" MTF stack (zones + level engine + coil) ")
    safe_print(f"  trades {mtf_stats['count']} | win rate {mtf_stats['win_rate']} | "
               f"avg R {mtf_stats['avg_r']:+.2f}")
    safe_print(" Plain 5m ORB (same days, score>=80, OI/regime gates bypassed in replay)")
    safe_print(f"  signals {len(all_orb)}")
    safe_print("=" * 72)
