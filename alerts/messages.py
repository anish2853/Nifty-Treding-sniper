"""Telegram message constructors - the phone is the only UI (Prompt 2), so every
production message is emoji-prefixed to be scannable in the chat list:

  📊 PRE-MARKET | 🌤 REGIME | 🎯 SIGNAL | 🚨 EXIT | ⏹ SQUARE-OFF |
  📕 EOD SUMMARY | ⚠️ ERROR | 🔴 SHUTDOWN

TelegramSender enforces the 4096-char limit and retries 3x; these builders keep
bodies short. Message bodies are ASCII so Windows consoles/redirects never choke;
the emoji live only in the prefix line.
"""
from datetime import timedelta

from utils import ist_now
import settings


def _hhmm() -> str:
    return ist_now().strftime("%H:%M")


def _date() -> str:
    return ist_now().strftime("%a %d-%b-%Y")


def premarket(report_body: str) -> str:
    return f"📊 PRE-MARKET — {_date()}\n{report_body}"


def regime(summary: str, notes=()) -> str:
    lines = [f"🌤 REGIME — {_hhmm()} IST", summary]
    lines += [f"• {note}" for note in notes]
    return "\n".join(lines)


def regime_change(summary: str) -> str:
    return f"🌤 REGIME CHANGE — {_hhmm()} IST\n{summary}"


def signal(*, direction: str, option: str, score, entry_spot, sl_level, reasons) -> str:
    """🎯 trade alert (fired by the Phase B entry engine). `reasons` is a list of
    strings like 'level break (ORB high ...) (+20)'."""
    lines = [
        f"🎯 SIGNAL — {_hhmm()} IST",
        f"{direction} {option}",
        f"score {score} | entry spot {entry_spot} | SL (ORB midpoint) {sl_level}",
        "WHY:",
    ]
    lines += [f"• {reason}" for reason in reasons]
    return "\n".join(lines)


def trade_card(*, signal_no: int, direction: str, trade, spot: float,
               now_txt: str, family: str = "BREAKOUT",
               data_source: str = "live-chain",
               delta: float = 0.0, spot_r: float = 0.0,
               prem_r: float = 0.0) -> str:
    """🎯 full trade card, exact format per spec with Phase 14 Delta-Aware metrics."""
    side = "CE" if direction == "LONG" else "PE"
    action = "BUY"
    reasons = []
    for name, ok, points, detail in trade.components:
        mark = "✓" if ok else "✗"
        suffix = f" — {detail}" if detail else ""
        reasons.append(f"{mark} {name}{suffix} (+{points})")
    band_top = trade.entry_prem + settings.ENTRY_PRICE_BAND
    candle_close = (trade.candle_start + timedelta(minutes=5)).strftime("%H:%M") \
        if trade.candle_start else "—"
    if trade.runway_pts is not None and trade.runway_wall:
        runway_line = f"RUNWAY: {trade.runway_pts:.0f} pts to wall {trade.runway_wall}"
    else:
        runway_line = "RUNWAY: n/a (no measurable wall)"

    delta_beta = delta if delta > 0 else settings.PREMIUM_BETA
    delta_line = f"DELTA: {delta:.2f} (Target ITM 0.65-0.75)" if delta > 0 else ""
    r_line = f"RISK/REWARD: Spot {spot_r:.2f}R | Premium {prem_r:.2f}R" if (spot_r > 0 or prem_r > 0) else ""

    lines = [
        f"🎯 SIGNAL #{signal_no} [{family}] — {action} {side} {trade.strike} "
        f"(weekly expiry {trade.expiry})",
        f"⏰ {now_txt} | candle close {candle_close} | "
        f"spot {spot:.2f} | score {trade.score}",
    ]
    if delta_line:
        lines.append(delta_line)
    if r_line:
        lines.append(r_line)
    lines += [
        "WHY:",
        *reasons,
        f"ENTRY: {action} {trade.strike} at ₹{trade.entry_prem:.2f}–₹{band_top:.2f} "
        f"(1 lot = {settings.LOT_SIZE} qty)",
        f"SL spot: {trade.sl_spot:.2f} — candle CLOSE beyond = EXIT",
        f"SL prem: ₹{max(5.0, trade.entry_prem - (abs(trade.entry_spot - trade.sl_spot) * delta_beta)):.2f}"
        if delta > 0
        else f"SL prem: ₹{trade.entry_prem * settings.SL_PREMIUM_FRACTION:.2f}",
        f"TGT 1: spot {trade.tgt1_spot:.2f} → prem ~"
        f"₹{trade.entry_prem + delta_beta * trade.one_r:.2f} → "
        f"BOOK 50%, SL to entry",
        f"TGT 2: spot {trade.tgt2_spot:.2f} → prem ~"
        f"₹{trade.entry_prem + delta_beta * 2 * trade.one_r:.2f} → "
        f"EXIT rest",
        f"MAX RISK: ₹{settings.RISK_PREM_FRACTION * trade.entry_prem * settings.LOT_SIZE:,.0f} "
        f"| Exit by 15:10",
        runway_line,
        f"DATA: {data_source}",
    ]
    return "\n".join(lines)


def book_alert(*, option: str, spot: float, pnl_text: str) -> str:
    """+1R touched: book half, stop to breakeven, trail the rest (Prompt B 4g)."""
    return (f"🚨 TGT 1 HIT — {_hhmm()} IST\n"
            f"{option}\n"
            f"spot touched +1R ({spot:.2f})\n"
            f"BOOK 50% now, SL to breakeven (entry spot), trail the rest to TGT 2\n"
            f"mark P&L: {pnl_text}")


def exit_alert(*, option: str, reason: str, pnl_text: str,
               saved_text: str | None = None, extra: str | None = None,
               grade: str = "OI-N/A") -> str:
    """🚨 exit alert: grade tag (OI-STRONG / OI-MODERATE / WALL-UNWINDING / OI-N/A)
    + reason + current P&L + amount saved vs hard SL."""
    lines = [f"🚨 EXIT [{grade}] — {_hhmm()} IST", option,
             f"reason: {reason}", f"P&L: {pnl_text}"]
    if saved_text:
        lines.append(f"saved vs hard SL: {saved_text}")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def square_off(trade_lines) -> str:
    return "⏹ SQUARE-OFF — 15:10 IST\n" + "\n".join(trade_lines)


def eod(body: str) -> str:
    return f"📕 EOD SUMMARY — {_date()}\n\n{body}"


def error(short: str, restart_no: int, restart_limit: int) -> str:
    return (f"⚠️ ERROR — day loop crashed: {short}\n"
            f"restarting in 60 s (restart {restart_no}/{restart_limit} today; "
            f"full traceback in the log file)")


def feed_alert(source: str, threshold: int, detail: str | None = None) -> str:
    extra = f" ({detail})" if detail else ""
    return (f"⚠️ DATA FEED — {source} failed {threshold}x in a row{extra}; "
            f"the bot keeps running with this factor marked MISSING")


def shutdown(reason: str) -> str:
    return f"🔴 SHUTDOWN — {reason}"


def test_mode_warning() -> str:
    return "⚠️ TEST MODE during market hours — use --mode live for monitoring"


def data_mismatch(detail: str) -> str:
    return f"⚠️ DATA MISMATCH — signal suppressed ({detail})"


def trading_halted(reason: str) -> str:
    return f"🛑 TRADING HALTED — {reason}"


def skipped_alert(*, side: str, level: str, reason: str, score) -> str:
    return f"🚫 SKIPPED: {side} break {level} | reason: {reason} | score {score}"


def explainable_trade_signal(
    *,
    strategy: str,
    direction: str,
    regime: str,
    entry_range: str,
    sl_spot: str,
    risk_r: str,
    management: str,
    evidence_items: list[str],
    invalidation_items: list[str],
    trade_id: str | None = None,
    next_liquidity_target: str | None = None,
) -> str:
    """Master explainable trade signal alert (Project Bible Section 35).
    Zero hallucinations: any missing metric is explicitly marked as unavailable."""
    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        "🎯 NIFTY SNIPER — TRADE SIGNAL",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Strategy:   {strategy}",
        f"Direction:  {direction}",
        f"Regime:     {regime}",
        f"Entry:      {entry_range}",
        f"SL:         {sl_spot}",
        f"Risk:       {risk_r}",
        f"Management: {management}",
    ]
    if next_liquidity_target:
        lines.append(f"Target:     {next_liquidity_target}")
    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "WHY THIS TRADE?",
        "━━━━━━━━━━━━━━━━━━━━",
    ])
    for idx, item in enumerate(evidence_items, 1):
        lines.append(f"{idx}. {item}")

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "INVALIDATION",
        "━━━━━━━━━━━━━━━━━━━━",
    ])
    for item in invalidation_items:
        lines.append(f"• {item}")

    id_txt = f" | ID: {trade_id}" if trade_id else ""
    lines.extend([
        f"⏰ {_hhmm()} IST{id_txt}",
        "━━━━━━━━━━━━━━━━━━━━",
    ])
    return "\n".join(lines)


def no_trade_alert(*, strategy: str, candidate_direction: str, reason: str) -> str:
    """Informative alert when an active setup candidate is rejected (Section 36)."""
    return (
        f"ℹ️ NO TRADE — {_hhmm()} IST\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Candidate: {strategy} ({candidate_direction})\n"
        f"Reason:    {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


def trade_management_update(
    *,
    strategy: str,
    direction: str,
    entry_price: float,
    current_price: float,
    current_r: float,
    trend_status: str = "VALID",
    structure: str = "5m bearish",
    next_liquidity: str = "—",
    action: str = "HOLD + TRAIL",
    reason: str = "Structure swing updated",
    trade_id: str | None = None,
) -> str:
    """Material trade update alert for active runner management (Phase 4 Section 23)."""
    id_line = f" | ID: {trade_id}" if trade_id else ""
    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"NIFTY SNIPER\n"
        f"TRADE UPDATE — {_hhmm()} IST{id_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{strategy}\n"
        f"{direction}\n\n"
        f"Entry:     {entry_price:.2f}\n"
        f"Current:   {current_price:.2f}\n"
        f"Current R: {current_r:+.2f}R\n\n"
        f"Trend:\n"
        f"{trend_status}\n\n"
        f"Structure:\n"
        f"{structure}\n\n"
        f"Next liquidity:\n"
        f"{next_liquidity}\n\n"
        f"Action:\n"
        f"{action}\n\n"
        f"Reason:\n"
        f"{reason}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


def format_telegram_trade_signal(
    *,
    strategy: str,
    direction: str,
    regime: str,
    entry: float | str,
    initial_sl: float | str,
    risk: float | str,
    score: float | int | str,
    reasons: list[str],
    next_liquidity: str,
    room: float | str,
    invalidation: str,
    confidence: str,
    strike: int | str | None = None,
    expiry: str | None = None,
    entry_prem: float | None = None,
    sl_prem: float | None = None,
    tgt1_prem: float | None = None,
    tgt2_prem: float | None = None,
    tgt1_spot: float | None = None,
    tgt2_spot: float | None = None,
    rr_ratio: float | None = None,
    max_risk_rs: float | None = None,
) -> str:
    """Exact Phase 5 Section 5 Trade Signal format with Mandatory Option Leg & Targets (Phase 13.4)."""
    entry_str = f"{entry:.2f}" if isinstance(entry, float) else str(entry)
    sl_str = f"{initial_sl:.2f}" if isinstance(initial_sl, float) else str(initial_sl)
    risk_str = f"{risk:.1f}R" if isinstance(risk, float) else str(risk)
    score_str = str(score)
    room_str = f"{room:.0f} pts" if isinstance(room, (int, float)) else str(room)

    why_lines = "\n".join(f"✓ {r}" for r in reasons)

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        "NIFTY SNIPER",
        "TRADE SIGNAL",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Strategy:\n{strategy}",
        "",
        f"Direction:\n{direction}",
    ]

    # Mandatory Option Leg (Phase 13.3 + 13.4)
    if strike and entry_prem is not None:
        side = "CE" if direction == "LONG" else "PE"
        exp_txt = f" (Expiry: {expiry})" if expiry else ""
        prem_band = f"₹{entry_prem:.2f}–₹{entry_prem + 10.0:.2f}"
        sl_prem_txt = f"₹{sl_prem:.2f}" if sl_prem is not None else f"₹{entry_prem * 0.75:.2f}"
        lines.extend([
            "",
            f"Instrument:\nBUY {strike} {side}{exp_txt}",
            "",
            f"Entry:\nSpot {entry_str} | Prem {prem_band}",
            "",
            f"SL:\nPrem {sl_prem_txt} (Spot ref: {sl_str})",
        ])
        if tgt1_prem is not None and tgt2_prem is not None:
            tgt1_spot_txt = f"Spot {tgt1_spot:.1f} → " if tgt1_spot else ""
            tgt2_spot_txt = f"Spot {tgt2_spot:.1f} → " if tgt2_spot else ""
            rr_txt = f" | R:R 1:{rr_ratio:.1f}" if rr_ratio else ""
            lines.extend([
                "",
                f"Targets{rr_txt}:\nTGT1: {tgt1_spot_txt}Prem ₹{tgt1_prem:.2f} (Book 50%, SL to entry)\nTGT2: {tgt2_spot_txt}Prem ₹{tgt2_prem:.2f} (Runner exit)",
            ])
        if max_risk_rs is not None:
            lines.extend([
                "",
                f"Max Risk:\n₹{max_risk_rs:,.0f} (1 lot = {settings.LOT_SIZE} qty)",
            ])
    else:
        lines.extend([
            "",
            f"Entry:\n{entry_str}",
            "",
            f"Initial SL:\n{sl_str}",
        ])

    lines.extend([
        "",
        f"Regime:\n{regime}",
        "",
        f"Risk:\n{risk_str}",
        "",
        f"Score:\n{score_str}",
        "",
        f"WHY:\n{why_lines}",
        "",
        f"NEXT LIQUIDITY:\n{next_liquidity}",
        "",
        f"ROOM:\n{room_str}",
        "",
        f"INVALIDATION:\n{invalidation}",
        "",
        f"CONFIDENCE:\n{confidence}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
    ])
    return "\n".join(lines)


def format_telegram_trade_exit(
    *,
    strategy: str,
    direction: str,
    entry: float | str,
    exit_price: float | str,
    result_r: float | str,
    mfe_r: float | str,
    mae_r: float | str,
    exit_reason: str,
    thesis: str = "VALID",
) -> str:
    """Exact Phase 5 Section 8 Trade Exit format."""
    entry_str = f"{entry:.2f}" if isinstance(entry, float) else str(entry)
    exit_str = f"{exit_price:.2f}" if isinstance(exit_price, float) else str(exit_price)
    res_str = f"{result_r:+.2f}R" if isinstance(result_r, float) else str(result_r)
    mfe_str = f"{mfe_r:+.2f}R" if isinstance(mfe_r, float) else str(mfe_r)
    mae_str = f"{mae_r:+.2f}R" if isinstance(mae_r, float) else str(mae_r)

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "NIFTY SNIPER\n"
        "TRADE EXIT\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Strategy:\n{strategy}\n\n"
        f"Direction:\n{direction}\n\n"
        f"Entry:\n{entry_str}\n\n"
        f"Exit:\n{exit_str}\n\n"
        f"Result:\n{res_str}\n\n"
        f"MFE:\n{mfe_str}\n\n"
        f"MAE:\n{mae_str}\n\n"
        f"Exit reason:\n{exit_reason}\n\n"
        f"Thesis:\n{thesis}\n\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


def format_telegram_trade_update(
    *,
    strategy: str,
    direction: str,
    entry: float | str,
    current: float | str,
    current_r: float | str,
    trend: str = "VALID",
    structure: str = "5m bearish",
    next_liquidity: str = "—",
    action: str = "HOLD + TRAIL",
    reason: str = "New lower-high confirmed",
) -> str:
    """Exact Phase 5 Section 7 Trade Update format."""
    entry_str = f"{entry:.0f}" if isinstance(entry, float) else str(entry)
    curr_str = f"{current:.0f}" if isinstance(current, float) else str(current)
    r_str = f"{current_r:+.1f}R" if isinstance(current_r, float) else str(current_r)

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "NIFTY SNIPER\n"
        "TRADE UPDATE\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{strategy} {direction}\n\n"
        f"Entry:\n{entry_str}\n\n"
        f"Current:\n{curr_str}\n\n"
        f"Current R:\n{r_str}\n\n"
        f"Trend:\n{trend}\n\n"
        f"Structure:\n{structure}\n\n"
        f"Next liquidity:\n{next_liquidity}\n\n"
        f"Action:\n{action}\n\n"
        f"Reason:\n{reason}\n\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


def format_critical_system_halt(reason: str) -> str:
    """Exact Phase 5 Section 27 Critical System Halt format."""
    return f"CRITICAL SYSTEM HALT\n\nReason:\n{reason}"


def format_data_blind_alert(detail: str) -> str:
    """Exact Phase 5 Section 23 Data Blindness format."""
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚨 DATA BLINDNESS ALERT\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Candle feed lag > 5 min during market hours.\n"
        f"Trading HALTED. No new trades allowed.\n\n"
        f"Detail: {detail}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


# ---- MODEL A SNIPER PRODUCTION TELEGRAM ALERTS (OPTIONS DRIVEN) ----

def format_sniper_entry_alert(
    *,
    trade_id: str,
    direction: str,
    setup_name: str,
    entry_price: float,
    initial_sl: float,
    risk_pts: float,
    tgt1_price: float,
    tgt2_price: float,
    fib_50: float = 0.0,
    fib_618: float = 0.0,
    fib_705: float = 0.0,
    notes: str = "",
    strike: int = 0,
    option_type: str = "",
    option_symbol: str = "",
    entry_prem: float = 0.0,
    sl_prem: float = 0.0,
    tgt1_prem: float = 0.0,
    tgt2_prem: float = 0.0,
    expiry: str = "",
    delta: float = 0.65,
    spot_r: float = 0.0,
    prem_r: float = 0.0,
    confluence_bonus: bool = False,
    ride_target_prem_pts: float = 0.0,
) -> str:
    """🎯 Production Telegram alert for Option Trade Entry (Phase 15.1 TL-FIB v2)."""
    side_badge = "🟢 BUY CE (CALL)" if direction == "LONG" else "🔴 BUY PE (PUT)"
    opt_name = option_symbol or (f"NIFTY {strike} {option_type}" if strike else side_badge)
    if expiry and expiry not in opt_name:
        opt_name = f"{opt_name} ({expiry} Expiry)"
    lot_size = getattr(settings, "LOT_SIZE", 75)
    prem_txt = f"₹{entry_prem:.1f} – ₹{entry_prem + 10.0:.1f}" if entry_prem > 0 else "Market Order"
    sl_prem_txt = f"₹{sl_prem:.1f}" if sl_prem > 0 else "—"
    tgt1_prem_txt = f"₹{tgt1_prem:.1f}" if tgt1_prem > 0 else "—"
    tgt2_prem_txt = f"₹{tgt2_prem:.1f}" if tgt2_prem > 0 else "—"
    capital_txt = f"₹{entry_prem * lot_size:,.0f}" if entry_prem > 0 else "—"

    valid_fibs = [f for f in (fib_50, fib_618, fib_705) if f and f > 0]
    fib_lo = min(valid_fibs) if valid_fibs else 0.0
    fib_hi = max(valid_fibs) if valid_fibs else 0.0
    bonus_str = " 🔥 [OI Zone Confluence +10]" if confluence_bonus else ""
    fib_txt = f"• Golden Pocket: [{fib_lo:.1f} – {fib_hi:.1f}]{bonus_str}\n" if (fib_lo > 0 and fib_hi > 0) else ""
    delta_val = delta if delta > 0 else 0.65
    delta_txt = f"• Strike Delta: {delta_val:.2f} (Slightly ITM)\n"

    prem_gain = ride_target_prem_pts if ride_target_prem_pts > 0 else (tgt2_prem - entry_prem if (tgt2_prem and entry_prem) else 20.0)

    is_sweep = "SMW" in trade_id or "Sweep" in setup_name
    is_zone_reversal = trade_id.startswith("ZR-") or "Zone Reversal" in setup_name or "Trendline" in setup_name
    if is_zone_reversal:
        title = "ZONE REVERSAL"
        rule_desc = "Arm 3: 2-Candle Liquidity Zone Reversal | PDH/PDL/OI Levels | Trendline Rejection | Structural Trail"
    elif is_sweep:
        title = "LIQUIDITY SWEEP"
        rule_desc = "Arm 1 Institutional Liquidity Sweep | MSS Confirmed | Fib Targets"
    else:
        title = "TL-FIB v2"
        rule_desc = "Phase 15.1 TL-FIB v2 | With-Trend Only | Scalp-Killer Active"

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 NIFTY SNIPER — {title} ENTRY\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"INSTRUMENT: {opt_name}\n"
        f"ACTION: {side_badge} at {prem_txt}\n"
        f"LOT SIZE: {lot_size} Qty (1 Lot) | Capital: ~{capital_txt}\n\n"
        f"{delta_txt}"
        f"• Entry Spot: ₹{entry_price:.1f}\n"
        f"• Stop Loss: {sl_prem_txt} Prem (Spot ref: ₹{initial_sl:.1f} | Risk: {risk_pts:.1f} pts)\n"
        f"• Checkpoint (1st Pivot): {tgt1_prem_txt} Prem (Spot ref: ₹{tgt1_price:.1f})\n"
        f"  ↳ CHECKPOINT ONLY — NEVER AUTO-EXIT (Structure Trailing Active)\n"
        f"• Ride Target: +{prem_gain:.1f} prem pts (Spot ref: ₹{tgt2_price:.1f} | {tgt2_prem_txt} Prem)\n\n"
        f"{fib_txt}"
        "THE RIDE RULES:\n"
        "↳ Single trade — zero early scalps (1R auto-exit removed)\n"
        "↳ Trailing 5-min structure from entry (Runner holds)\n"
        "↳ 15:10 Square-Off\n\n"
        f"Setup: {setup_name}\n"
        f"Trade ID: {trade_id}\n"
        f"Rule: {rule_desc}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


def format_sniper_tgt1_alert(
    *,
    trade_id: str,
    direction: str,
    entry_price: float,
    tgt1_price: float,
    tgt2_price: float,
    strike: int = 0,
    option_type: str = "",
    option_symbol: str = "",
    entry_prem: float = 0.0,
    tgt1_prem: float = 0.0,
    tgt2_prem: float = 0.0,
    expiry: str = "",
) -> str:
    """🎯 Production Telegram alert when First Pivot Checkpoint is reached (checkpoint only)."""
    opt_name = option_symbol or (f"NIFTY {strike} {option_type}" if strike else ("CE LONG" if direction == "LONG" else "PE SHORT"))
    if expiry and expiry not in opt_name:
        opt_name = f"{opt_name} ({expiry} Expiry)"
    gain_pts = max(0.0, tgt1_prem - entry_prem) if (tgt1_prem and entry_prem) else 0.0

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 TL-FIB v2 — FIRST PIVOT CHECKPOINT REACHED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"INSTRUMENT: {opt_name} ({trade_id})\n"
        f"Checkpoint Reached at: ₹{tgt1_prem:.1f} Prem (+{gain_pts:.1f} pts)\n"
        f"Spot Index: ₹{tgt1_price:.1f}\n\n"
        "ℹ️ STATUS: Checkpoint cleared! Do NOT exit.\n"
        "↳ 5-min Higher Lows / Lower Highs structure trailing is ACTIVE.\n"
        "↳ Full runner is holding for the complete trend ride.\n"
        f"• Ride Target: ₹{tgt2_prem:.1f} Prem (Spot ref: ₹{tgt2_price:.1f})\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


def format_sniper_exit_alert(
    *,
    trade_id: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    realized_r: float,
    exit_reason: str,
    notes: str = "",
    strike: int = 0,
    option_type: str = "",
    option_symbol: str = "",
    entry_prem: float = 0.0,
    exit_prem: float = 0.0,
    expiry: str = "",
) -> str:
    """🚨 Production Telegram alert for TL-FIB v2 Option Trade Exit."""
    opt_name = option_symbol or (f"NIFTY {strike} {option_type}" if strike else ("CE LONG" if direction == "LONG" else "PE SHORT"))
    if expiry and expiry not in opt_name:
        opt_name = f"{opt_name} ({expiry} Expiry)"
    lot_size = getattr(settings, "LOT_SIZE", 75)
    res_badge = f"✅ {realized_r:+.2f} R" if realized_r > 0 else (f"🛡️ BE {realized_r:+.2f} R" if realized_r == 0 else f"❌ {realized_r:+.2f} R")
    pnl_pts = (exit_prem - entry_prem) if (exit_prem and entry_prem) else 0.0
    pnl_rs = pnl_pts * lot_size
    pnl_txt = f" ({pnl_pts:+.1f} prem pts | ₹{pnl_rs:+,.0f}/lot)" if pnl_pts != 0 else ""

    is_sweep = "SMW" in trade_id or "Sweep" in notes
    title = "LIQUIDITY SWEEP" if is_sweep else "TL-FIB v2"

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🚨 NIFTY SNIPER — {title} TRADE CLOSED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"INSTRUMENT: {opt_name} ({trade_id})\n"
        f"Buy Premium: ₹{entry_prem:.1f} | Exit Premium: ₹{exit_prem:.1f}\n"
        f"Spot Entry: ₹{entry_price:.1f} | Spot Exit: ₹{exit_price:.1f}\n\n"
        f"OUTCOME: {res_badge}{pnl_txt}\n"
        f"Exit Reason: {exit_reason}\n"
        f"Notes: {notes or '5-min Structure Trail Exit'}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


def format_zone_ride_entry_alert(
    *,
    trade_id: str,
    direction: str,
    anchor_zone_name: str,
    anchor_zone_range: str,
    entry_spot: float,
    initial_sl_spot: float,
    risk_pts: float,
    opposing_zone_name: str,
    room_spot_pts: float,
    room_prem_pts: float,
    spot_target: float,
    prem_target: float,
    strike: int,
    option_type: str,
    option_symbol: str = "",
    entry_prem: float = 0.0,
    sl_prem: float = 0.0,
    expiry: str = "",
    delta: float = 0.70,
    confluence_factors: list[str] | None = None,
    is_trend_day: bool = False,
) -> str:
    """🎯 Production Telegram alert for Phase 15 Zone-Ride Trade Entry."""
    side_badge = "🟢 BUY CE (CALL)" if direction == "LONG" else "🔴 BUY PE (PUT)"
    opt_name = option_symbol or f"NIFTY {strike} {option_type}"
    if expiry and expiry not in opt_name:
        opt_name = f"{opt_name} ({expiry} Expiry)"
    lot_size = getattr(settings, "LOT_SIZE", 75)
    capital = entry_prem * lot_size
    conf_txt = f" | Confluence: {', '.join(confluence_factors)}" if confluence_factors else ""
    target_type = "Trend Day Measured Move (50-100+ pts)" if is_trend_day else "Structure Target (20-40 pts)"

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 NIFTY SNIPER — ZONE-RIDE TRADE ENTRY\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"INSTRUMENT: {opt_name}\n"
        f"ACTION: {side_badge} at ₹{entry_prem:.1f} – ₹{entry_prem + 10.0:.1f}\n"
        f"LOT SIZE: {lot_size} Qty (1 Lot) | Capital: ~₹{capital:,.0f}\n\n"
        f"• Anchor Zone: {anchor_zone_name} [{anchor_zone_range}] STRONG (Tested Today{conf_txt})\n"
        f"• Entry Spot: ₹{entry_spot:.1f} (Confirming Candle Close)\n"
        f"• Target Delta: {delta:.2f} (ITM Strike)\n"
        f"• Stop Loss: ₹{sl_prem:.1f} Prem (Spot ref: ₹{initial_sl_spot:.1f} | Risk: {risk_pts:.1f} pts beyond zone far edge)\n"
        f"• Room to Next Zone: {room_spot_pts:.1f} spot pts → ~{room_prem_pts:.1f} premium pts (Passed >= 20.0 pts gate)\n"
        f"• Structure Target: Spot ₹{spot_target:.1f} (+{abs(spot_target - entry_spot):.1f} pts) → Premium ~₹{prem_target:.1f} (+{abs(prem_target - entry_prem):.1f} pts) [{target_type}]\n\n"
        "THE RIDE:\n"
        "↳ Single trade — zero early scalps / no +5 pt bookings\n"
        "↳ Trailing 5-min Higher Lows structure (Runner holds)\n"
        f"↳ Optional single 30% booking ONLY at major opposing wall ({opposing_zone_name})\n\n"
        f"Trade ID: {trade_id}\n"
        f"Rule: Phase 15 Zone-Ride Model | With-Trend Only | {'Trend Day Ride to Close' if is_trend_day else '15:10 Square-Off'}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


def format_zone_ride_wall_booking_alert(
    *,
    trade_id: str,
    direction: str,
    option_symbol: str,
    entry_prem: float,
    wall_prem: float,
    entry_spot: float,
    current_spot: float,
    opposing_wall_name: str,
) -> str:
    """🎯 Production Telegram alert when major opposing wall is reached (optional 30% booking)."""
    lot_size = getattr(settings, "LOT_SIZE", 75)
    pnl_pts = max(0.0, wall_prem - entry_prem)
    pnl_rs = pnl_pts * lot_size * 0.30

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 MAJOR OPPOSING WALL REACHED — OPTIONAL 30% BOOKED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"INSTRUMENT: {option_symbol} ({trade_id})\n"
        f"Wall Encountered: {opposing_wall_name} (Spot ₹{current_spot:.1f})\n"
        f"30% Booked at Premium: ₹{wall_prem:.1f} (+{pnl_pts:.1f} pts | ~₹{pnl_rs:+,.0f})\n\n"
        "THE RIDE CONTINUES:\n"
        "↳ 70% RUNNER HOLDING & RIDING 5-MIN STRUCTURE\n"
        "↳ Stop Loss locked to Breakeven / Structural Higher Low\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


def format_zone_ride_exit_alert(
    *,
    trade_id: str,
    direction: str,
    option_symbol: str,
    entry_prem: float,
    exit_prem: float,
    entry_spot: float,
    exit_spot: float,
    realized_r: float,
    exit_reason: str,
    notes: str = "",
) -> str:
    """🚨 Production Telegram alert for Zone-Ride structural trailing exit."""
    lot_size = getattr(settings, "LOT_SIZE", 75)
    res_badge = f"✅ {realized_r:+.2f} R" if realized_r > 0 else (f"🛡️ BE {realized_r:+.2f} R" if realized_r == 0 else f"❌ {realized_r:+.2f} R")
    pnl_pts = (exit_prem - entry_prem) if (exit_prem and entry_prem) else 0.0
    pnl_rs = pnl_pts * lot_size
    pnl_txt = f" ({pnl_pts:+.1f} pts | ₹{pnl_rs:+,.0f}/lot)" if pnl_pts != 0 else ""

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚨 NIFTY SNIPER — ZONE-RIDE TRADE CLOSED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"INSTRUMENT: {option_symbol} ({trade_id})\n"
        f"Buy Premium: ₹{entry_prem:.1f} | Exit Premium: ₹{exit_prem:.1f}\n"
        f"Spot Entry: ₹{entry_spot:.1f} | Spot Exit: ₹{exit_spot:.1f}\n\n"
        f"OUTCOME: {res_badge}{pnl_txt}\n"
        f"Exit Reason: {exit_reason}\n"
        f"Notes: {notes or '5-min Structure Trail Exit'}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


