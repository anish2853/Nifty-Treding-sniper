"""Shared NSE HTTP session.

One requests.Session stays alive for the whole run. Handshake before ANY API call:
GET /option-chain with full browser headers (current Chrome UA, html Accept,
Accept-Language, Referer, Sec-Fetch set) so Akamai sets cookies in the session;
API calls then go out with JSON Accept headers over the same cookies. Hard rate
limit of NSE_MIN_REQUEST_GAP_SEC between ANY two NSE requests, retries with
exponential backoff. Every getter returns None on final failure instead of raising,
so callers can degrade that factor to MISSING and keep running.
"""
import logging
import time

import requests

import settings

log = logging.getLogger(__name__)

_HANDSHAKE_URL = settings.NSE_BASE_URL + "/option-chain"


class NSESession:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": settings.NSE_UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        })
        self._last_request_monotonic = 0.0
        self.last_status = None  # HTTP status of the most recent response (or None)

    # -- header sets -------------------------------------------------------
    def _browser_headers(self) -> dict:
        """Full browser-like headers for the /option-chain handshake (Prompt-4 #3)."""
        return {
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8"),
            "Referer": _HANDSHAKE_URL,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            'sec-ch-ua': '"Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            'sec-ch-ua-platform': '"Windows"',
        }

    def _api_headers(self) -> dict:
        """XHR-style headers for JSON API calls over the warmed session."""
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": _HANDSHAKE_URL,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    # -- rate limiting -----------------------------------------------------
    def _respect_rate_limit(self):
        elapsed = time.monotonic() - self._last_request_monotonic
        wait = settings.NSE_MIN_REQUEST_GAP_SEC - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_monotonic = time.monotonic()

    # -- handshake -----------------------------------------------------------
    def warm_up(self) -> bool:
        """GET /option-chain with full browser headers; cookies stay in the session.
        Must run before any API call."""
        try:
            self._respect_rate_limit()
            resp = self._session.get(_HANDSHAKE_URL,
                                     timeout=settings.NSE_HTTP_TIMEOUT_SEC,
                                     headers=self._browser_headers())
            self.last_status = resp.status_code
            log.info("NSE handshake %s -> HTTP %s (cookies: %d)",
                     _HANDSHAKE_URL, resp.status_code, len(self._session.cookies))
        except requests.RequestException as exc:
            self.last_status = None
            log.warning("NSE handshake failed for %s: %s", _HANDSHAKE_URL, exc)
        return bool(self._session.cookies)

    # -- JSON GETs ------------------------------------------------------------
    def get_json(self, url: str):
        """Rate-limited GET returning parsed JSON, or None after retries. Never raises.

        401/403/429 and non-JSON bodies (bot block) trigger a fresh handshake before
        the next attempt; exponential backoff separates attempts.
        """
        for attempt in range(1, settings.NSE_MAX_RETRIES + 1):
            try:
                self._respect_rate_limit()
                resp = self._session.get(url, timeout=settings.NSE_HTTP_TIMEOUT_SEC,
                                         headers=self._api_headers())
                self.last_status = resp.status_code
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        log.warning("NSE returned non-JSON (possible bot block) on %s", url)
                else:
                    log.warning("NSE HTTP %s on %s (attempt %d/%d)",
                                resp.status_code, url, attempt, settings.NSE_MAX_RETRIES)
            except requests.RequestException as exc:
                self.last_status = None
                log.warning("NSE request error on %s (attempt %d/%d): %s",
                            url, attempt, settings.NSE_MAX_RETRIES, exc)
            self.warm_up()  # refresh cookies before the next attempt
            time.sleep(min(settings.NSE_BACKOFF_BASE_SEC * 2 ** (attempt - 1), 30))
        log.error("NSE get_json failed after %d attempts: %s", settings.NSE_MAX_RETRIES, url)
        return None

    def get_json_once(self, url: str):
        """Single rate-limited JSON GET attempt - no internal retries. Used when the
        CALLER owns the retry loop (endpoint discovery, 5x5 s chain fetch)."""
        try:
            self._respect_rate_limit()
            resp = self._session.get(url, timeout=settings.NSE_HTTP_TIMEOUT_SEC,
                                     headers=self._api_headers())
            self.last_status = resp.status_code
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    log.warning("NSE returned non-JSON (possible bot block) on %s", url)
            else:
                log.warning("NSE HTTP %s on %s", resp.status_code, url)
        except requests.RequestException as exc:
            self.last_status = None
            log.warning("NSE request error on %s: %s", url, exc)
        return None

    def get_debug(self, url: str) -> dict:
        """One rate-limited GET returning raw handshake diagnostics - exact URL,
        status, the prepared request headers (Cookie redacted) and the first 500
        characters of the body. Powers --mode chain-test."""
        self._respect_rate_limit()
        try:
            resp = self._session.get(url, timeout=settings.NSE_HTTP_TIMEOUT_SEC,
                                     headers=self._api_headers())
            self.last_status = resp.status_code
            body = resp.text or ""
            sent = dict(resp.request.headers)
            for key in list(sent):
                if key.lower() == "cookie":
                    sent[key] = f"<redacted, {len(resp.request.headers[key])} chars>"
            headers_txt = "\n".join(f"  {k}: {v}" for k, v in sent.items())
            return {"status": resp.status_code, "url": resp.url,
                    "length": len(body), "snippet": body[:500],
                    "request_headers": headers_txt}
        except requests.RequestException as exc:
            self.last_status = None
            return {"status": None, "url": url, "length": 0,
                    "snippet": f"<request error: {exc}>", "request_headers": ""}

    def get_text(self, url: str):
        """Rate-limited GET returning response text or None (best-effort scrapes,
        possibly off-domain - uses the plain session headers)."""
        try:
            self._respect_rate_limit()
            resp = self._session.get(url, timeout=settings.GIFT_NIFTY_HTTP_TIMEOUT_SEC)
            self.last_status = resp.status_code
            if resp.status_code == 200:
                return resp.text
            log.warning("HTTP %s on %s", resp.status_code, url)
        except requests.RequestException as exc:
            self.last_status = None
            log.warning("Request error on %s: %s", url, exc)
        return None

    def close(self):
        self._session.close()
