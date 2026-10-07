"""Telegram alerts via plain HTTPS POST to the Bot API (no frameworks).

Send failures are logged and swallowed - a Telegram outage must never crash the
assistant or interrupt data collection. Messages auto-split at Telegram's 4096-char
limit. Plain text only (no parse_mode) so no escaping can corrupt alerts.
"""
import logging

import requests

import settings

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4000  # safety margin under Telegram's 4096 limit


class TelegramSender:
    def __init__(self, token=None, chat_id=None):
        self.token = token or settings.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or settings.TELEGRAM_CHAT_ID
        self._warned_unconfigured = False

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        """Send one message; returns True only if every chunk was accepted."""
        if not self.configured:
            if not self._warned_unconfigured:
                log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / "
                            "TELEGRAM_CHAT_ID in config/.env) - alerts go to console only")
                self._warned_unconfigured = True
            return False
        delivered = True
        for chunk in _split(text, MAX_MESSAGE_LEN):
            delivered = self._post(chunk) and delivered
        return delivered

    def _post(self, text: str) -> bool:
        url = f"{API_URL}/bot{self.token}/sendMessage"
        for attempt in range(1, settings.TELEGRAM_RETRIES + 1):
            try:
                resp = requests.post(url, json={"chat_id": self.chat_id, "text": text},
                                     timeout=settings.TELEGRAM_TIMEOUT_SEC)
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                if resp.status_code == 200 and data.get("ok"):
                    return True
                log.warning("Telegram send failed (attempt %d/%d): HTTP %s %s",
                            attempt, settings.TELEGRAM_RETRIES, resp.status_code,
                            data.get("description", ""))
            except requests.RequestException as exc:
                log.warning("Telegram send error (attempt %d/%d): %s",
                            attempt, settings.TELEGRAM_RETRIES, exc)
        return False


class NullTelegramSender(TelegramSender):
    """Test/diagnostics sender: never transmits, never warns. Tests and offline
    smokes MUST use this so synthetic data can never reach a real chat again."""

    def __init__(self):
        super().__init__(token=None, chat_id=None)
        self.sent: list = []

    @property
    def configured(self) -> bool:
        return False

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return False


def _split(text: str, limit: int):
    """Yield text in chunks <= limit, preferring line boundaries."""
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text:
        yield text
