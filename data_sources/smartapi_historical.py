"""Angel One SmartAPI Historical Data Client.

Implements the official Angel One SmartAPI Historical Candle Data API:
Specification: https://smartapi.angelone.in/docs/Historical
Endpoint: POST https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData

Supported Intervals:
- ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE, TEN_MINUTE, FIFTEEN_MINUTE, THIRTY_MINUTE, ONE_HOUR, ONE_DAY

Standard Indices / Symbols:
- NIFTY 50: exchange="NSE", symboltoken="99926000"
- BANKNIFTY: exchange="NSE", symboltoken="99926009"
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Optional
import pandas as pd
import pytz

import settings
from backend.auth_state import get_auth_state

log = logging.getLogger("smartapi.historical")
IST = pytz.timezone("Asia/Kolkata")


class SmartApiHistoricalClient:
    """Client for fetching historical candle data via Angel One SmartAPI."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        client_code: Optional[str] = None,
    ) -> None:
        self.api_key = (
            api_key
            or getattr(settings, "SMARTAPI_API_KEY", "")
            or os.getenv("ANGEL_API_KEY", "")
        ).strip()
        self.client_code = (
            client_code
            or getattr(settings, "SMARTAPI_CLIENT_CODE", "")
            or os.getenv("ANGEL_CLIENT_ID", "")
        ).strip()
        self._smart_connect: Any = None

    def _get_authenticated_client(self) -> Any:
        """Obtain an authenticated SmartConnect instance.
        
        Priority 1: Active session tokens from SmartApiAuthState (OAuth / Publisher callback)
        Priority 2: Direct session generation via TOTP + PIN in .env
        """
        if self._smart_connect is not None:
            return self._smart_connect

        if not self.api_key:
            log.warning("SmartAPI api_key is missing. Configure SMARTAPI_API_KEY in config/.env.")
            return None

        try:
            from SmartApi import SmartConnect  # type: ignore
        except ImportError:
            log.warning("smartapi-python SDK not installed.")
            return None

        smart = SmartConnect(api_key=self.api_key)

        # 1. Check if authenticated via the OAuth / HTTPS tunnel callback
        auth_state = get_auth_state()
        if auth_state.is_authenticated():
            token = auth_state.get_auth_token()
            feed_token = auth_state.get_feed_token()
            if token:
                smart.setAccessToken(token)
                if feed_token:
                    smart.setFeedToken(feed_token)
                self._smart_connect = smart
                log.info("Initialized SmartConnect using active OAuth callback session.")
                return self._smart_connect

        # 2. Fallback: Authenticate directly via TOTP if configured in .env
        pin = os.getenv("ANGEL_PIN", "").strip()
        totp_key = os.getenv("ANGEL_TOTP_KEY", "").strip()
        if self.client_code and pin and totp_key:
            try:
                import pyotp  # type: ignore
                totp_val = pyotp.TOTP(totp_key).now()
                session_data = smart.generateSession(self.client_code, pin, totp_val)
                if session_data.get("status"):
                    log.info("Initialized SmartConnect using direct TOTP session generation.")
                    self._smart_connect = smart
                    return self._smart_connect
                else:
                    log.warning("SmartAPI TOTP generateSession failed: %s", session_data.get("message"))
            except Exception as exc:
                log.warning("SmartAPI TOTP login exception: %s", exc)

        log.info("No active SmartAPI session available. Please authenticate via the dashboard.")
        return None

    def fetch_candles(
        self,
        symboltoken: str = "99926000",
        exchange: str = "NSE",
        interval: str = "FIVE_MINUTE",
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> pd.DataFrame:
        """Fetch historical candles from Angel One getCandleData endpoint.
        
        Returns a DataFrame with index 'timestamp' (tz=Asia/Kolkata) and columns:
        ['Open', 'High', 'Low', 'Close', 'Volume'].
        """
        smart = self._get_authenticated_client()
        if smart is None:
            return pd.DataFrame()

        start_dt_str = f"{start_date.strftime('%Y-%m-%d')} 09:15" if start_date else "2026-01-01 09:15"
        end_dt_str = f"{end_date.strftime('%Y-%m-%d')} 15:30" if end_date else f"{date.today().strftime('%Y-%m-%d')} 15:30"

        params = {
            "exchange": exchange,
            "symboltoken": symboltoken,
            "interval": interval,
            "fromdate": start_dt_str,
            "todate": end_dt_str,
        }

        try:
            log.info(
                "Requesting getCandleData: exchange=%s, token=%s, interval=%s, from=%s, to=%s",
                exchange, symboltoken, interval, start_dt_str, end_dt_str
            )
            resp = smart.getCandleData(params)

            if not resp or not resp.get("status"):
                err_msg = resp.get("message") if resp else "Empty response"
                err_code = resp.get("errorcode") if resp else "UNKNOWN"
                log.warning("SmartAPI getCandleData failed: code=%s, msg=%s", err_code, err_msg)
                return pd.DataFrame()

            rows = resp.get("data", [])
            if not rows:
                log.info("SmartAPI getCandleData returned 0 rows.")
                return pd.DataFrame()

            # Format: [timestamp, open, high, low, close, volume]
            df = pd.DataFrame(rows, columns=["timestamp", "Open", "High", "Low", "Close", "Volume"])
            
            # Robust datetime parsing
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            if df["timestamp"].dt.tz is None:
                df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
            else:
                df["timestamp"] = df["timestamp"].dt.tz_convert(IST)

            df.set_index("timestamp", inplace=True)
            df.sort_index(inplace=True)

            # Ensure numeric types
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            log.info("Successfully fetched %d candles from SmartAPI.", len(df))
            return df

        except Exception as exc:
            log.warning("Exception during SmartAPI getCandleData request: %s", exc)
            return pd.DataFrame()


def get_historical_client() -> SmartApiHistoricalClient:
    """Access default SmartApiHistoricalClient singleton."""
    return SmartApiHistoricalClient()
