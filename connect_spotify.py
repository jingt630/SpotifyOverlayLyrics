"""Connect Spotify and show the LRCLIB lyric line for the current position.

Run this file in a terminal. Login and token storage live in
spotify_client/auth.py. Profile and now-playing calls live in
spotify_client/player.py. Lyric lookup lives in lrclib_client.py.

The JSON shapes Spotify returns are in spotify_api_formats.md.
The function and URL flow is in spotify_flow.md.
"""

from __future__ import annotations

import sys

from lrclib_client import LyricLookup
from spotify_client.auth import (
    TOKEN_PATH,
    authorize,
    load_saved_session,
    print_setup,
    print_token_summary,
    prompt,
    save_tokens,
)
from spotify_client.player import print_now_playing, print_profile


def watch_keyboard(session: dict, lyrics: LyricLookup) -> None:
    """Read the current song and its lyric line after each typed line.

    Purpose:
        Keep reading playback until the user asks to stop. Enter alone counts
        as a request. The lines q, quit, and exit end the loop. LRCLIB is
        contacted only when the Spotify track id changes.

    Parameters:
        session (dict): Saved Spotify login passed to print_now_playing.
        lyrics (LyricLookup): Cache that turns progress_ms into a lyric line.

    Returns:
        None.

    Precondition:
        stdin can read lines, and session can call the currently-playing endpoint.

    Postcondition:
        Each non-quit line has printed one currently-playing response and one
        lyric lookup. The function returns after a quit line.
    """
    print()
    print("Type anything and press Enter to read the current song and lyric line.")
    print("Type q and press Enter to stop.")
    while True:
        typed = input("> ")
        if typed.strip().lower() in {"q", "quit", "exit"}:
            print("Stopped.")
            return
        session, now = print_now_playing(session)
        lyrics.show(now)


def main() -> None:
    """Log in if needed, print the profile, then reread the song and lyric line.

    Purpose:
        Run Spotify login and the LRCLIB line lookup from the command line.

    Parameters:
        None. A missing token file causes a Client ID prompt via stdin.

    Returns:
        None.

    Precondition:
        stdout and stdin are available. Either TOKEN_PATH holds a usable
        session, or the user can complete browser login.

    Postcondition:
        The profile, one currently-playing response, and one lyric lookup have
        been printed, unless an exception stopped the run. watch_keyboard then
        owns the terminal until the user quits.
    """
    # Song titles can contain characters the default Windows console cannot print.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    session = load_saved_session()
    if session:
        print("Using the Spotify login already saved in .spotify/tokens.json")
    else:
        print_setup()
        client_id = prompt("Paste your Spotify Client ID: ")
        token = authorize(client_id)
        if "access_token" not in token:
            raise RuntimeError(f"Spotify did not return an access token: {token}")
        session = save_tokens(client_id, token)
        print()
        print(f"Tokens saved to {TOKEN_PATH}")
        print_token_summary(token)

    lyrics = LyricLookup()
    session = print_profile(session)
    session, now = print_now_playing(session)
    lyrics.show(now)
    watch_keyboard(session, lyrics)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(1)
    except Exception as exc:
        print(f"\nCould not connect: {exc}")
        raise SystemExit(1)
