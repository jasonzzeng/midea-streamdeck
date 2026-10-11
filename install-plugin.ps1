# Install the Midea AC Stream Deck plugin on Windows.
# Run from the repo root:  powershell -ExecutionPolicy Bypass -File .\install-plugin.ps1
$ErrorActionPreference = "Stop"

$src  = Join-Path $PSScriptRoot "streamdeck-plugin\com.jasonzeng.acdeck.sdPlugin"
$dest = Join-Path $env:APPDATA "Elgato\StreamDeck\Plugins\com.jasonzeng.acdeck.sdPlugin"

if (-not (Test-Path (Join-Path $src "manifest.json"))) {
    throw "Plugin source not found at $src"
}
if (-not (Test-Path (Join-Path $PSScriptRoot ".venv\Scripts\python.exe"))) {
    throw "No .venv found. Run setup first (see the Windows section of README.md)."
}

Get-Process "StreamDeck" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "Stopping Stream Deck..."
    $_.CloseMainWindow() | Out-Null
    Start-Sleep -Seconds 3
    if (-not $_.HasExited) { $_ | Stop-Process -Force }
}

# plugin.bat can't reach ..\..\.venv through the junction, so link it in.
$venvLink = Join-Path $src ".venv"
if (-not (Test-Path $venvLink)) {
    cmd /c mklink /J "$venvLink" (Join-Path $PSScriptRoot ".venv") | Out-Null
}

# Remove only the junction itself; a recursive delete would follow it into the repo.
if (Test-Path $dest) { cmd /c rmdir "$dest" }

# A directory junction keeps the plugin pointed at the repo, like the mac symlink.
cmd /c mklink /J "$dest" "$src" | Out-Null
Write-Host "Linked $dest"

$sd = Join-Path ${env:ProgramFiles} "Elgato\StreamDeck\StreamDeck.exe"
if (Test-Path $sd) { Start-Process $sd; Write-Host "Stream Deck restarted." }
else { Write-Host "Start Stream Deck manually." }
Write-Host "Actions appear under the 'Midea AC' category."
