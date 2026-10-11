@echo off
echo Starting Nifty Sniper Live Trading Dashboard...
echo.
echo Dashboard will open at: http://localhost:8501
echo.
python -m streamlit run dashboard/app.py --server.fileWatcherType poll
