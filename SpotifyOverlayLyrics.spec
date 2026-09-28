# -*- mode: python ; coding: utf-8 -*-
"""Mac build of the lyric overlay. The lyric server is not included."""

from PyInstaller.utils.hooks import collect_data_files

datas = [
    ("settings_text", "settings_text"),
    ("chinese_script_data", "chinese_script_data"),
    ("lyric_api_url.txt", "."),
]
datas += collect_data_files("opencc")

hidden = [
    "zhconv",
    "opencc",
    "objc",
    "Foundation",
    "AppKit",
    "CoreFoundation",
]

a = Analysis(
    ["connect_spotify.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pymongo", "dns", "winsdk"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SpotifyOverlayLyrics",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SpotifyOverlayLyrics",
)

app = BUNDLE(
    coll,
    name="SpotifyOverlayLyrics.app",
    icon=None,
    bundle_identifier="com.spotifyoverlaylyrics.desktop",
    info_plist={
        "CFBundleName": "Spotify Lyric Overlay",
        "CFBundleDisplayName": "Spotify Lyric Overlay",
        "NSAppleEventsUsageDescription": "Reads the song Spotify is playing so lyrics can follow it, and can pause, resume, and skip.",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
