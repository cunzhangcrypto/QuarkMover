@echo off
chcp 65001 >nul
cd /d "%~dp0"
python quark_mover.py
pause
