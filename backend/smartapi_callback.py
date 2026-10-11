"""Dedicated Angel One SmartAPI Authentication Callback Service.

Provides a lightweight, isolated FastAPI endpoint for SmartAPI OAuth/Publisher redirects.
Exposes:
- GET /health: Health check returning {"status": "ok"}
- GET /smartapi/callback: Receives SmartAPI redirect parameters over HTTPS tunnel
- GET /smartapi/status: Safe metadata status check (no secrets exposed)

CRITICAL ARCHITECTURE RULES:
- Binds to 127.0.0.1 (local interface only).
- Exposes only this callback service through the Cloudflare Tunnel.
- Streamlit dashboard (port 8502) is NEVER exposed through this tunnel.
- No live order routing or order placement code exists in this service.
- Never logs auth_token, feed_token, refresh_token, or client secrets.
"""
from __future__ import annotations

import logging
from typing import Optional
from fastapi import FastAPI, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

import settings
from backend.auth_state import (
    get_auth_state,
    SmartApiAuthStatus,
    mask_identifier,
)

# Configure secure logger
log = logging.getLogger("smartapi.callback")
logging.basicConfig(
    level=getattr(settings, "LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="NIFTY SNIPER SmartAPI Callback Service",
    description="Dedicated authentication callback service for Angel One SmartAPI",
    version="1.0.0",
)


def _render_html_response(
    title: str,
    status_type: str,
    heading: str,
    message: str,
    details: Optional[str] = None,
) -> str:
    """Generate clean, self-contained HTML page for user's browser."""
    color = "#10b981" if status_type == "success" else "#ef4444"
    icon = "✓" if status_type == "success" else "✕"
    
    details_html = f'<div style="margin-top:15px;padding:12px;background:#1e293b;border-radius:6px;font-family:monospace;font-size:13px;color:#94a3b8;word-break:break-all;">{details}</div>' if details else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{
      background: #0f172a;
      color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
      margin: 0;
      padding: 20px;
    }}
    .card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      max-width: 520px;
      width: 100%;
      padding: 32px;
      text-align: center;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
    }}
    .badge {{
      width: 60px;
      height: 60px;
      line-height: 60px;
      border-radius: 50%;
      background: {color}22;
      color: {color};
      font-size: 28px;
      margin: 0 auto 20px;
      font-weight: bold;
    }}
    h1 {{
      font-size: 22px;
      margin: 0 0 10px;
      color: #ffffff;
    }}
    p {{
      color: #94a3b8;
      font-size: 15px;
      line-height: 1.5;
      margin: 0 0 16px;
    }}
    .footer {{
      margin-top: 24px;
      font-size: 12px;
      color: #64748b;
      border-top: 1px solid #334155;
      padding-top: 16px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">{icon}</div>
    <h1>{heading}</h1>
    <p>{message}</p>
    {details_html}
    <div class="footer">
      NIFTY SNIPER Engine &bull; Mode: <strong>{getattr(settings, "EXECUTION_MODE", "PAPER")}</strong> &bull; You may close this window.
    </div>
  </div>
</body>
</html>"""


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Return 204 No Content for browser favicon requests to avoid 404 logs."""
    from fastapi import Response
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse)
def root_index() -> str:
    """Friendly root landing page with service health and endpoint links."""
    auth_state = get_auth_state()
    auth_badge = (
        '<span style="color:#10b981;font-weight:bold;">● AUTHENTICATED</span>'
        if auth_state.is_authenticated()
        else '<span style="color:#eab308;font-weight:bold;">○ NOT_AUTHENTICATED</span>'
    )
    client_txt = f"({auth_state.client_code})" if auth_state.client_code else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NIFTY SNIPER — SmartAPI Gateway</title>
  <style>
    body {{
      background: #0f172a;
      color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
      margin: 0;
      padding: 20px;
    }}
    .card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      max-width: 560px;
      width: 100%;
      padding: 32px;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
    }}
    h1 {{
      font-size: 22px;
      margin: 0 0 8px;
      color: #ffffff;
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    p {{
      color: #94a3b8;
      font-size: 14px;
      line-height: 1.5;
      margin: 0 0 20px;
    }}
    .status-box {{
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 20px;
      font-size: 14px;
    }}
    .status-row {{
      display: flex;
      justify-content: space-between;
      padding: 6px 0;
      border-bottom: 1px solid #1e293b;
    }}
    .status-row:last-child {{
      border-bottom: none;
    }}
    .endpoints {{
      margin-top: 16px;
      text-align: left;
    }}
    .endpoints a {{
      color: #38bdf8;
      text-decoration: none;
      font-family: monospace;
      font-size: 13px;
    }}
    .endpoints a:hover {{
      text-decoration: underline;
    }}
    .footer {{
      margin-top: 24px;
      font-size: 12px;
      color: #64748b;
      border-top: 1px solid #334155;
      padding-top: 16px;
      text-align: center;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>🎯 NIFTY SNIPER Gateway</h1>
    <p>Dedicated local SmartAPI authentication callback server.</p>
    
    <div class="status-box">
      <div class="status-row">
        <span>Server Status:</span>
        <strong style="color:#10b981;">ONLINE</strong>
      </div>
      <div class="status-row">
        <span>Execution Mode:</span>
        <strong>{getattr(settings, "EXECUTION_MODE", "PAPER")}</strong>
      </div>
      <div class="status-row">
        <span>SmartAPI Status:</span>
        <span>{auth_badge} {client_txt}</span>
      </div>
    </div>

    <div class="endpoints">
      <div style="font-size:12px;font-weight:600;color:#94a3b8;margin-bottom:8px;text-transform:uppercase;">Registered Endpoints</div>
      <ul style="padding-left:20px;margin:0;line-height:1.8;">
        <li><a href="/health">GET /health</a> &mdash; Health check</li>
        <li><a href="/smartapi/status">GET /smartapi/status</a> &mdash; Safe metadata status</li>
        <li><a href="/smartapi/callback">GET /smartapi/callback</a> &mdash; SmartAPI redirect target</li>
      </ul>
    </div>

    <div class="footer">
      Streamlit Dashboard runs privately on <a href="http://localhost:8502" target="_blank" style="color:#38bdf8;">http://localhost:8502</a>
    </div>
  </div>
</body>
</html>"""


@app.get("/health")
def health_check() -> dict[str, str]:
    """Health check endpoint required by monitoring & cloudflared tunnel."""
    return {"status": "ok"}


@app.get("/smartapi/status")
def smartapi_status() -> dict:
    """Safe status endpoint returning only unredacted metadata."""
    auth_state = get_auth_state()
    return auth_state.get_safe_summary()


@app.get("/smartapi/callback", response_class=HTMLResponse)
def smartapi_callback(
    request: Request,
    auth_token: Optional[str] = Query(None),
    feed_token: Optional[str] = Query(None),
    refresh_token: Optional[str] = Query(None),
    request_token: Optional[str] = Query(None),
    client_code: Optional[str] = Query(None),
    client_id: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    status_param: Optional[str] = Query(None, alias="status"),
    message: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
):
    """Handle SmartAPI redirect authentication callback.
    
    Accepts tokens from Angel One Publisher/OAuth redirection.
    NEVER logs sensitive token contents.
    """
    client_ip = request.client.host if request.client else "unknown"
    auth_state = get_auth_state()

    # Safe diagnostic logging (strictly metadata only)
    log.info(
        "SmartAPI callback request received from %s | params: auth_token=%s, feed_token=%s, "
        "refresh_token=%s, request_token=%s, status=%s, error=%s",
        client_ip,
        bool(auth_token),
        bool(feed_token),
        bool(refresh_token),
        bool(request_token),
        status_param,
        bool(error),
    )

    # 1. Check for explicit error responses from SmartAPI
    if error or (status_param is not None and str(status_param).lower() in ("false", "0", "failed", "error")):
        err_msg = error_description or message or error or "Authentication was rejected or cancelled by Angel One."
        auth_state.set_error(err_msg)
        return HTMLResponse(
            content=_render_html_response(
                title="SmartAPI Auth Failed",
                status_type="error",
                heading="Authentication Failed",
                message="Angel One SmartAPI returned an error or the login was cancelled.",
                details=f"Reason: {err_msg}",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # 2. Extract valid token (auth_token or request_token)
    resolved_token = auth_token or request_token
    resolved_client = client_code or client_id

    if not resolved_token:
        err_msg = "No auth_token or request_token found in callback parameters."
        log.warning("SmartAPI callback missing token parameters.")
        auth_state.set_error(err_msg)
        return HTMLResponse(
            content=_render_html_response(
                title="Invalid Callback",
                status_type="error",
                heading="Missing Authentication Parameters",
                message="The SmartAPI redirection completed without valid authentication tokens.",
                details="Expected 'auth_token' or 'request_token' query parameter.",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # 3. Store authentication session safely
    auth_state.set_authenticated(
        auth_token=resolved_token,
        feed_token=feed_token,
        refresh_token=refresh_token,
        client_code=resolved_client,
    )

    masked_client = auth_state.client_code or "SmartAPI Account"

    return HTMLResponse(
        content=_render_html_response(
            title="SmartAPI Authenticated",
            status_type="success",
            heading="SmartAPI Connected Successfully",
            message=f"Angel One session for client <strong>{masked_client}</strong> is active and verified.",
            details="Execution mode remains strictly <strong>PAPER</strong>. Orders cannot be sent to the broker.",
        ),
        status_code=status.HTTP_200_OK,
    )
