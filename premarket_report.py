"""Module 1 - Factor Engine: Pre-Market Regime Report (~08:45 IST).

Collects global factors (yfinance), FII/DII + India VIX + GIFT Nifty (best effort,
NSE), computes the expected gap (GIFT Nifty - Nifty previous close) and emits a
formatted report to console + Telegram + journal. Every factor can independently
report MISSING without blocking the report.

Run standalone:   python premarket_report.py
main.py also calls run() automatically at 08:45 IST on trading days.
"""
import logging

from alerts import messages
from alerts.telegram_bot import TelegramSender
from data_sources.global_factors import fetch_global_factors
from data_sources.nse_chain import fetch_option_chain_with_retries, parse_option_chain
from data_sources.nse_quote import fii_dii, gift_nifty, india_vix
from data_sources.nse_session import NSESession
from engines.regime import expected_gap_verdict
from journal.store import JournalStore
import settings
from utils import (MISSING, fmt_num, fmt_pct, ist_now, load_date_set, safe_print,
                   setup_logging)

log = logging.getLogger(__name__)

SEP = "=" * 64


def collect(nse: NSESession) -> dict:
    """Gather every pre-market factor; each element degrades gracefully on failure.

    The chain fetch (endpoint variants in order, up to NSE_CHAIN_RETRIES cycles
    NSE_CHAIN_RETRY_GAP_SEC apart) at 08:45 reads the previous session's closing
    state - OI does not move overnight - so its PCR is the previous session's
    closing PCR. Outside market hours a failure is labeled 'awaiting market open',
    never a hard MISSING (Prompt-3 fixes #2/#4)."""
    factors = fetch_global_factors()
    raw, chain_status, chain_tried = fetch_option_chain_with_retries(nse)
    chain = None
    if raw is not None:
        try:
            chain = parse_option_chain(raw)
        except ValueError as exc:
            log.warning("pre-market chain parse failed: %s", exc)
    return {
        "factors": factors,
        "chain": chain,
        "chain_status": chain_status,
        "chain_tried": chain_tried,
        "fii_dii": fii_dii(nse),
        "vix_nse": india_vix(nse),
        "gift": gift_nifty(nse),
        "pdc": factors.nifty_pd.extra.get("pdc") if factors.nifty_pd.status == "OK" else None,
    }


def _vix_line(factors, vix_nse: dict) -> str:
    if factors.india_vix_prev.status == "OK" and factors.india_vix_prev.value is not None:
        return (f"   India VIX      : {fmt_num(factors.india_vix_prev.value)}"
                f"  (prev close, yfinance)")
    if vix_nse.get("value") is not None:
        return f"   India VIX      : {fmt_num(vix_nse['value'])}  (NSE quote fallback)"
    error = factors.india_vix_prev.error or vix_nse.get("error")
    return f"   India VIX      : {MISSING} ({error})"


def build_report(collected: dict, event_day: bool) -> tuple:
    """Format the report; also returns the preliminary-verdict reasons for the journal."""
    factors = collected["factors"]
    us_dir_word = {1: "UP", -1: "DOWN", 0: "UNKNOWN"}[factors.us_direction()]
    lines = [
        SEP,
        " PRE-MARKET REGIME REPORT - NIFTY OPTIONS ASSISTANT",
        f" {ist_now().strftime('%a %Y-%m-%d %H:%M:%S')} IST",
        SEP,
        " GLOBAL CUES (last completed sessions, yfinance)",
        f"   S&P 500        : {fmt_num(factors.sp500.value)}  ({fmt_pct(factors.sp500.pct_change)})",
        f"   Nasdaq         : {fmt_num(factors.nasdaq.value)}  ({fmt_pct(factors.nasdaq.pct_change)})",
        f"   Crude (CL=F)   : {fmt_num(factors.crude.value)}  ({fmt_pct(factors.crude.pct_change)})",
        f"   USD/INR        : {fmt_num(factors.usdinr.value)}  ({fmt_pct(factors.usdinr.pct_change)})",
        _vix_line(factors, collected["vix_nse"]),
    ]
    nifty_pd = factors.nifty_pd
    if nifty_pd.status == "OK":
        extra = nifty_pd.extra
        lines.append(f"   Nifty PD       : PDH {fmt_num(extra.get('pdh'))} | "
                     f"PDL {fmt_num(extra.get('pdl'))} | PDC {fmt_num(extra.get('pdc'))}")
    else:
        lines.append(f"   Nifty PD       : {MISSING} ({nifty_pd.error})")

    chain = collected.get("chain")
    if chain is not None:
        lines.append(f"   Prev close PCR : {fmt_num(chain.pcr_total, 3)} (all expiries) | "
                     f"{fmt_num(chain.pcr_nearest, 3)} (nearest expiry)")
    else:
        tried = collected.get("chain_tried") or []
        status = collected.get("chain_status")
        detail = ", ".join(f"{label}={code if code is not None else 'ERR'}"
                           for label, code in tried)
        fallback = f"last HTTP {status}" if status is not None else "no HTTP response"
        lines.append(f"   Prev close PCR : awaiting market open "
                     f"(chain unavailable pre-open; {detail or fallback})")

    fd = collected["fii_dii"]
    if fd.get("status") == "OK":
        asof = f"  (as of {fd.get('asof')})" if fd.get("asof") else ""
        lines.append(" FII/DII (prev session, NSE)")
        lines.append(f"   FII net: Rs {fmt_num(fd.get('fii_net_cr'))} Cr | "
                     f"DII net: Rs {fmt_num(fd.get('dii_net_cr'))} Cr{asof}")
    else:
        lines.append(f" FII/DII: {MISSING} ({fd.get('error')})")

    gift = collected["gift"]
    if gift.get("value") is not None:
        lines.append(f" GIFT NIFTY: {fmt_num(gift['value'])}")
    else:
        lines.append(f" GIFT NIFTY: {MISSING} ({gift.get('error')})")

    pdc = collected["pdc"]
    gap_pts = None
    if gift.get("value") is not None and pdc:
        gap_pts = gift["value"] - pdc
        lines.append(f" EXPECTED GAP vs PDC: {gap_pts:+,.0f} pts ({gap_pts / pdc:+.2%})")
    else:
        lines.append(f" EXPECTED GAP vs PDC: {MISSING} (needs GIFT Nifty and PDC)")

    state = expected_gap_verdict(gap_pts, pdc, factors.us_direction(), event_day)
    lines.append(" PRELIMINARY REGIME VERDICT")
    lines.append(f"   {state.summary()}")
    lines.append(f"   US close direction (S&P 500): {us_dir_word}")
    lines.extend(f"   - {note}" for note in state.notes)
    if event_day:
        lines.append("   - EVENT CALENDAR date: report kept silent (no Telegram), journal only")
    lines.append(SEP)
    reasons = state.reasons()
    chain = collected.get("chain")
    reasons["prev_close_pcr_total"] = chain.pcr_total if chain else None
    reasons["prev_close_pcr_nearest"] = chain.pcr_nearest if chain else None
    return "\n".join(lines), reasons


def _pd_level(factors, key: str):
    """PDH/PDL with defensive access (yfinance Factor carries .extra; minimal
    stand-ins may not)."""
    try:
        if factors.nifty_pd.status == "OK":
            return (getattr(factors.nifty_pd, "extra", None) or {}).get(key)
    except AttributeError:
        pass
    return None


def run(store: JournalStore | None = None, tg: TelegramSender | None = None,
        nse: NSESession | None = None, send_telegram: bool = True) -> dict:
    """Build + emit the report. Returns {'report', 'reasons', 'pdc', 'us_direction',
    'sources'} so main.py can reuse the PDC / US direction for the 09:15
    classification and feed the per-source health tracker."""
    setup_logging()
    store = store or JournalStore()
    tg = tg or TelegramSender()
    nse = nse or NSESession()
    event_day = ist_now().date().isoformat() in load_date_set(settings.EVENT_CALENDAR_FILE)

    collected = collect(nse)
    factors = collected["factors"]
    report, reasons = build_report(collected, event_day)
    safe_print(report)
    if event_day:
        log.info("Event-calendar date: pre-market report suppressed on Telegram (spec: silent)")
    elif send_telegram:
        tg.send(messages.premarket(report))
    store.journal_event("REGIME", regime=reasons.get("regime"), reasons=reasons,
                        notes="pre-market factor report")
    chain = collected.get("chain")
    sources = {
        "yfinance": factors.nifty_pd.status == "OK",
        "option_chain_premarket": chain is not None,
        "nse_fii_dii": collected["fii_dii"].get("status") == "OK",
        "nse_vix_quote": collected["vix_nse"].get("value") is not None
                         or factors.india_vix_prev.status == "OK",
        "nse_gift_nifty": collected["gift"].get("value") is not None,
    }
    return {"report": report, "reasons": reasons,
            "pdc": collected["pdc"], "us_direction": factors.us_direction(),
            "pdh": _pd_level(factors, "pdh"),
            "pdl": _pd_level(factors, "pdl"),
            "sources": sources}


if __name__ == "__main__":
    run()
