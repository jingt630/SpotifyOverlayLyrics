# Builds dist\SpotifyOverlayLyrics\SpotifyOverlayLyrics.exe
# Friends run that exe. It calls the URL in lyric_api_url.txt.
# MongoDB credentials are not packaged.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install -r requirements-overlay.txt pyinstaller
python -m PyInstaller --noconfirm SpotifyOverlayLyrics.spec

Write-Host ""
Write-Host "Built: $PSScriptRoot\dist\SpotifyOverlayLyrics\SpotifyOverlayLyrics.exe"
Write-Host "Zip that folder, or compile installer.iss with Inno Setup."
