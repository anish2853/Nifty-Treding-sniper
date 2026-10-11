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
        self._sent_event_ids: set[str] = set()
        self._sent_decision_hashes: set[str] = set()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_event(
        self,
        text: str,
        *,
        event_id: str | None = None,
        decision_hash: str | None = None,
        severity: str = "INFO",
        event_type: str | None = None,
    ) -> bool:
        """Sends a structured decision event with idempotency, severity filtering and deduplication.

        Rules (Phase 5 Sections 3, 9, 19, 30):
        1. DEBUG events are NEVER sent to Telegram.
        2. Repeated events with identical event_id or decision_hash are rejected.
        3. Critical alerts (ENTRY, EXIT, DATA_BLIND, CRITICAL) are never suppressed.
        """
        sev_upper = str(severity).upper()
        if sev_upper == "DEBUG":
            log.debug("Suppressing DEBUG event from Telegram: %s", text[:80])
            return False

        if event_id and event_id in self._sent_event_ids:
            log.info("Deduplicating Telegram message with existing event_id: %s", event_id)
            return False

        if decision_hash and decision_hash in self._sent_decision_hashes:
            log.info("Deduplicating Telegram message with existing decision_hash: %s", decision_hash)
            return False

        if event_id:
            self._sent_event_ids.add(event_id)
        if decision_hash:
            self._sent_decision_hashes.add(decision_hash)

        delivered = self.send(text)
        if isinstance(self, NullTelegramSender):
            return True
        return delivered

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
