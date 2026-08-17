@echo off
title TableForge Local Pipeline Server
echo ==========================================================
echo Starting TableForge local HTTP server on port 8055...
echo ==========================================================
cd /d "%~dp0scripts\python"
python tableforge.py --server
pause
