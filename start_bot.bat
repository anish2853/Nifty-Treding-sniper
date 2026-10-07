@echo off
rem ==============================================================
rem  Nifty Alert Bot - daily launcher (Windows Task Scheduler).
rem  The Task fires this file Mon-Fri 08:30; the bot knows the day
rem  lifecycle itself and exits cleanly at 15:45.
rem ==============================================================
rem Always run from the folder this .bat lives in (Task Scheduler
rem otherwise starts in system32 and nothing resolves).
cd /d "%~dp0"
if not exist logs mkdir logs

rem UTF-8 stdout so emoji-bearing console output never breaks the
rem redirected log file.
set "PYTHONIOENCODING=utf-8"

rem Prefer the project venv; fall back to python on PATH.
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem Locale-safe date for the log name (%DATE% can contain '/' on
rem some Windows locales, which is an invalid filename character).
set "D=%DATE:/=-%"

%PY% main.py --mode live >> logs\bot_%D%.log 2>&1
