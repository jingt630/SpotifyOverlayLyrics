# Builds dist\SpotifyOverlayLyrics\SpotifyOverlayLyrics.exe
# Friends run that exe. It calls the URL in lyric_api_url.txt.
# MongoDB credentials are not packaged.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.13 @Args
        return
    }
    & python @Args
}

Invoke-Python -m pip install -r requirements-overlay.txt pyinstaller
Invoke-Python -m PyInstaller --noconfirm SpotifyOverlayLyrics.spec

Write-Host ""
Write-Host "Built: $PSScriptRoot\dist\SpotifyOverlayLyrics\SpotifyOverlayLyrics.exe"
Write-Host "Zip that folder, or compile installer.iss with Inno Setup."
