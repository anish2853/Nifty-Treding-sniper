"""Shared helpers: IST clock, rotating file logging, config loaders, formatting."""
import json
import logging
from datetime import date, datetime
from logging.handlers import RotatingFileHandler

import settings

MISSING = "MISSING"


def ist_now() -> datetime:
    return datetime.now(tz=settings.IST)


def ist_today() -> date:
    return ist_now().date()


def setup_logging() -> None:
    """Root logger with a rotating file handler + console handler."""
    settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if root.handlers:  # already configured (e.g. main -> premarket_report)
        return
    root.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    file_handler = RotatingFileHandler(
        settings.LOG_FILE, maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)


def load_date_set(path) -> set:
    """Load {"dates": ["YYYY-MM-DD", ...]} from a config JSON file; empty set on any problem."""
    log = logging.getLogger(__name__)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return {str(d) for d in data.get("dates", [])}
    except FileNotFoundError:
        log.warning("Config file missing: %s (treated as empty)", path)
    except (ValueError, OSError) as exc:
        log.warning("Could not parse %s: %s (treated as empty)", path, exc)
    return set()


def fmt_num(value, nd: int = 2) -> str:
    """ASCII-safe number for console/log output; MISSING for None."""
    if value is None:
        return MISSING
    return f"{value:,.{nd}f}"


def fmt_pct(value, nd: int = 2) -> str:
    """Format a fraction (0.0042) as a signed percent string ('+0.42%')."""
    if value is None:
        return MISSING
    return f"{value * 100:+.{nd}f}%"


def safe_print(text: str) -> None:
    """print() that survives Windows consoles/redirects without UTF-8: emoji-bearing
    Telegram texts degrade to '?'-marks on screen but stay intact on Telegram."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))
