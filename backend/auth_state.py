"""SmartAPI Authentication State & Session Manager.

Thread-safe, secure management of Angel One SmartAPI credentials and sessions.
CRITICAL SECURITY RULES:
- Never log, display, or commit tokens (auth_token, feed_token, refresh_token, TOTP).
- Expose only safe metadata (status, masked client code, token presence flags).
- Synchronize state safely between FastAPI callback process and Streamlit dashboard.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

import settings

log = logging.getLogger("smartapi.auth")

# Session persistence path (local development only, strictly ignored by git)
SESSION_FILE = settings.DATA_DIR / ".smartapi_session.json"


class SmartApiAuthStatus(str, Enum):
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    AUTHENTICATED = "AUTHENTICATED"
    ERROR = "ERROR"


def mask_identifier(val: Optional[str]) -> str:
    """Mask sensitive string, showing at most first 1-2 and last 2 characters."""
    if not val:
        return "UNKNOWN"
    val = str(val).strip()
    if len(val) <= 4:
        return "****"
    return f"{val[:2]}****{val[-2:]}"


class SmartApiAuthState:
    """Thread-safe container for SmartAPI session state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status: SmartApiAuthStatus = SmartApiAuthStatus.NOT_AUTHENTICATED
        self.authenticated_at: Optional[str] = None
        self.client_code: Optional[str] = None
        self.last_error: Optional[str] = None

        # Private in-memory token holders - NEVER exposed via public dicts or logs
        self._auth_token: Optional[str] = None
        self._feed_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._raw_client_code: Optional[str] = None

        # Attempt to load cached session on startup
        self._load_from_disk()

    def set_authenticated(
        self,
        auth_token: str,
        feed_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        client_code: Optional[str] = None,
    ) -> None:
        """Record successful SmartAPI authentication."""
        with self._lock:
            self.status = SmartApiAuthStatus.AUTHENTICATED
            self.authenticated_at = datetime.now(timezone.utc).isoformat()
            self._raw_client_code = client_code or settings.SMARTAPI_CLIENT_CODE or "USER"
            self.client_code = mask_identifier(self._raw_client_code)
            self.last_error = None

            self._auth_token = auth_token
            self._feed_token = feed_token
            self._refresh_token = refresh_token

            self._save_to_disk()

        log.info(
            "SmartAPI authentication succeeded. client=%s, has_feed_token=%s, has_refresh_token=%s",
            self.client_code,
            bool(feed_token),
            bool(refresh_token),
        )

    def set_error(self, error_message: str) -> None:
        """Record authentication failure."""
        with self._lock:
            self.status = SmartApiAuthStatus.ERROR
            self.last_error = str(error_message)[:200]
            self._save_to_disk()

        log.warning("SmartAPI authentication error recorded: %s", self.last_error)

    def clear(self) -> None:
        """Clear active authentication session."""
        with self._lock:
            self.status = SmartApiAuthStatus.NOT_AUTHENTICATED
            self.authenticated_at = None
            self.client_code = None
            self.last_error = None
            self._auth_token = None
            self._feed_token = None
            self._refresh_token = None
            self._raw_client_code = None

            if SESSION_FILE.exists():
                try:
                    SESSION_FILE.unlink()
                except Exception as exc:
                    log.warning("Failed to remove session file: %s", exc)

        log.info("SmartAPI session cleared.")

    def is_authenticated(self) -> bool:
        """Return True if session is authenticated and token is present."""
        # Refresh from disk if running in a separate process
        if not self._auth_token and SESSION_FILE.exists():
            self._load_from_disk()
        return self.status == SmartApiAuthStatus.AUTHENTICATED and bool(self._auth_token)

    def get_auth_token(self) -> Optional[str]:
        """Retrieve private auth_token (only for authorized backend clients)."""
        with self._lock:
            return self._auth_token

    def get_feed_token(self) -> Optional[str]:
        """Retrieve private feed_token."""
        with self._lock:
            return self._feed_token

    def get_safe_summary(self) -> dict[str, Any]:
        """Return safe, non-sensitive metadata for health checks, APIs, and dashboard."""
        # Sync from disk if separate process
        if SESSION_FILE.exists():
            self._load_from_disk()

        with self._lock:
            return {
                "status": self.status.value,
                "authenticated": self.is_authenticated(),
                "authenticated_at": self.authenticated_at,
                "client_code": self.client_code,
                "has_auth_token": bool(self._auth_token),
                "has_feed_token": bool(self._feed_token),
                "has_refresh_token": bool(self._refresh_token),
                "execution_mode": getattr(settings, "EXECUTION_MODE", "PAPER"),
                "last_error": self.last_error,
            }

    def _save_to_disk(self) -> None:
        """Save session to local restricted data file for cross-process synchronization."""
        try:
            settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "status": self.status.value,
                "authenticated_at": self.authenticated_at,
                "client_code": self.client_code,
                "last_error": self.last_error,
                "auth_token": self._auth_token,
                "feed_token": self._feed_token,
                "refresh_token": self._refresh_token,
                "raw_client_code": self._raw_client_code,
            }
            tmp_path = SESSION_FILE.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            tmp_path.replace(SESSION_FILE)
        except Exception as exc:
            log.warning("Could not persist session to disk: %s", exc)

    def _load_from_disk(self) -> None:
        """Load session from local disk if available."""
        if not SESSION_FILE.exists():
            return
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.status = SmartApiAuthStatus(data.get("status", SmartApiAuthStatus.NOT_AUTHENTICATED.value))
            self.authenticated_at = data.get("authenticated_at")
            self.client_code = data.get("client_code")
            self.last_error = data.get("last_error")
            self._auth_token = data.get("auth_token")
            self._feed_token = data.get("feed_token")
            self._refresh_token = data.get("refresh_token")
            self._raw_client_code = data.get("raw_client_code")
        except Exception as exc:
            log.debug("Session disk load skipped/failed: %s", exc)

    def __repr__(self) -> str:
        return f"<SmartApiAuthState status={self.status.value} client={self.client_code}>"


# Global singleton instance
_GLOBAL_AUTH_STATE = SmartApiAuthState()


def get_auth_state() -> SmartApiAuthState:
    """Access global SmartAPI authentication state."""
    return _GLOBAL_AUTH_STATE


def get_smartapi_login_url() -> str:
    """Generate the official Angel One SmartAPI publisher login URL."""
    api_key = getattr(settings, "SMARTAPI_API_KEY", "") or os.getenv("ANGEL_API_KEY", "")
    if not api_key:
        return "https://smartapi.angelone.in/publisher-login"

    base_url = "https://smartapi.angelone.in/publisher-login"
    login_url = f"{base_url}?api_key={api_key}"

    redirect_url = getattr(settings, "SMARTAPI_REDIRECT_URL", "")
    if redirect_url:
        login_url += f"&redirect_url={quote_plus(redirect_url)}"

    return login_url
