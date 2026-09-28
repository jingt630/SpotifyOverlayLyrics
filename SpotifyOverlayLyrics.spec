# -*- mode: python ; coding: utf-8 -*-
"""Windows build of the lyric overlay. The lyric server is not included."""

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [
    ("settings_text", "settings_text"),
    ("chinese_script_data", "chinese_script_data"),
    ("lyric_api_url.txt", "."),
]
datas += collect_data_files("opencc")
binaries = []
hidden = ["opencc", "certifi"]
winrt_datas, winrt_binaries, winrt_hidden = collect_all("winrt")
datas += winrt_datas
binaries += winrt_binaries
hidden += winrt_hidden

a = Analysis(
    ["connect_spotify.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pymongo", "dns"],
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
