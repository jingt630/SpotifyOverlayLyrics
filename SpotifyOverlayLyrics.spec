# -*- mode: python ; coding: utf-8 -*-
"""Windows build of the lyric overlay. The lyric server is not included."""

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
    "winsdk",
    "winsdk._winrt",
    "winsdk.system",
    "winsdk.windows.media",
    "winsdk.windows.media.control",
    "winsdk.windows.foundation",
    "winsdk.windows.foundation.collections",
    "winsdk.windows.storage",
    "winsdk.windows.storage.streams",
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
