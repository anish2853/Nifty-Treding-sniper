@echo off
echo ==============================================================
echo  Starting Nifty Sniper Live Trading Dashboard (Model A)
echo ==============================================================
echo.
echo Dashboard will open at: http://localhost:8501
echo Active Model: Trendline + Fibonacci Golden Pocket Retracement
echo Execution Gate: Real-time Paper Trading + Telegram Alerts
echo.
python -m streamlit run dashboard/app.py --server.fileWatcherType poll
