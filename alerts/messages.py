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
               data_source: str = "live-chain") -> str:
    """🎯 full trade card, exact format per spec (Prompt B module 3, tagged with the
    setup family per Prompt B+ #5, runway per Trend-Day v2.1)."""
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
    lines = [
        f"🎯 SIGNAL #{signal_no} [{family}] — {action} {side} {trade.strike} "
        f"(weekly expiry {trade.expiry})",
        f"⏰ {now_txt} | candle close {candle_close} | "
        f"spot {spot:.2f} | score {trade.score}",
        "WHY:",
        *reasons,
        f"ENTRY: {action} {trade.strike} at ₹{trade.entry_prem:.2f}–₹{band_top:.2f} "
        f"(1 lot = {settings.LOT_SIZE} qty)",
        f"SL spot: {trade.sl_spot:.2f} — candle CLOSE beyond = EXIT",
        f"SL prem: ₹{trade.entry_prem * settings.SL_PREMIUM_FRACTION:.2f}",
        f"TGT 1: spot {trade.tgt1_spot:.2f} → prem ~"
        f"₹{trade.entry_prem + settings.PREMIUM_BETA * trade.one_r:.2f} → "
        f"BOOK 50%, SL to entry",
        f"TGT 2: spot {trade.tgt2_spot:.2f} → prem ~"
        f"₹{trade.entry_prem + settings.PREMIUM_BETA * 2 * trade.one_r:.2f} → "
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
