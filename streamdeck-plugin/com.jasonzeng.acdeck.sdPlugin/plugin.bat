@echo off
REM Stream Deck launches this with -port/-pluginUUID/-registerEvent/-info.
cd /d "%~dp0"
"%~dp0..\..\.venv\Scripts\python.exe" plugin.py %*
