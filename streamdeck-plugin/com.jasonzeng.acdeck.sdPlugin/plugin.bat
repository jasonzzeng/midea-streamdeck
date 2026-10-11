@echo off
REM Stream Deck launches this with -port/-pluginUUID/-registerEvent/-info.
cd /d "%~dp0"
REM Through the Plugins junction, ..\.. resolves inside the Elgato folder, so
REM install-plugin.ps1 links .venv here. Fall back for a plain repo checkout.
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" plugin.py %*
) else (
    "%~dp0..\..\.venv\Scripts\python.exe" plugin.py %*
)
