# One-time environment setup on Windows.
# powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1 -Account you@example.com
param([Parameter(Mandatory=$true)][string]$Account)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = (Get-Command py -ErrorAction SilentlyContinue) ?? (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { throw "Install Python 3.10+ from python.org first." }

Write-Host "Creating .venv..."
& $py.Source -3 -m venv .venv 2>$null
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $py.Source -m venv .venv }

Write-Host "Installing dependencies..."
& ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt

Write-Host "`nDiscovering the AC and storing credentials..."
& ".venv\Scripts\python.exe" setup.py --account $Account

Write-Host "`nNow run:  powershell -ExecutionPolicy Bypass -File .\install-plugin.ps1"
