"""Folders for bundled files and for data the overlay is allowed to write.

A source checkout uses the repository root for both. An installed app reads
settings_text and the API URL from the bundle, and writes .spotify, .localLyric,
and .netease under the user's local app data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def resource_dir() -> Path:
    """Return the folder that holds settings_text and other bundled files."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    """Return the folder for settings and saved lyrics.

    Purpose:
        Program Files is not writable. The installed app keeps personal files
        in LocalAppData. A source checkout keeps them in the repository.

    Returns:
        Path: The writable directory. It is created when the app is frozen.
    """
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SpotifyOverlayLyrics"
        base.mkdir(parents=True, exist_ok=True)
        return base
    return Path(__file__).resolve().parent
