@echo off
echo ======================================================================
echo Starting Free Cloudflare HTTPS Tunnel for NIFTY SNIPER SmartAPI...
echo ======================================================================
echo.
echo Tunneling: http://127.0.0.1:8000
echo Streamlit (port 8502) is NOT exposed and remains private.
echo.
echo Look for the line below starting with 'https://...trycloudflare.com':
echo Copy that URL and append '/smartapi/callback' for your SmartAPI Redirect URL.
echo.
cloudflared.exe tunnel --url http://127.0.0.1:8000
pause
