#!/bin/sh
# Builds dist/SpotifyOverlayLyrics.app
# A new Mac can open that app. It calls the URL in lyric_api_url.txt.
# MongoDB credentials are not packaged.

set -eu
cd "$(dirname "$0")"

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-overlay.txt pyinstaller
.venv/bin/python -m PyInstaller --noconfirm SpotifyOverlayLyrics.spec
codesign --force --deep --sign - dist/SpotifyOverlayLyrics.app
ditto -c -k --keepParent dist/SpotifyOverlayLyrics.app dist/SpotifyOverlayLyrics-mac.zip

echo ""
echo "Built: $(pwd)/dist/SpotifyOverlayLyrics.app"
echo "Zip:   $(pwd)/dist/SpotifyOverlayLyrics-mac.zip"
