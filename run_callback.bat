@echo off
echo ======================================================================
echo Starting NIFTY SNIPER Dedicated SmartAPI Callback Server (Port 8000)...
echo ======================================================================
echo.
echo Local endpoint: http://127.0.0.1:8000/smartapi/callback
echo Health check:   http://127.0.0.1:8000/health
echo Status check:   http://127.0.0.1:8000/smartapi/status
echo.
.venv\Scripts\python.exe -m uvicorn backend.smartapi_callback:app --host 127.0.0.1 --port 8000 --reload
