"""Connect Spotify and show the LRCLIB lyric line for the current position.

Run this file in a terminal. Login and token storage live in
spotify_client/auth.py. Profile and now-playing calls live in
spotify_client/player.py. Lyric lookup lives in lrclib_client.py.

The JSON shapes Spotify returns are in spotify_api_formats.md.
The function and URL flow is in spotify_flow.md.
"""

from __future__ import annotations

import sys
import time

import lrclib_client
from lrclib_client import LyricLookup, LyricWindow
from spotify_client.auth import (
    TOKEN_PATH,
    authorize,
    load_saved_session,
    print_setup,
    print_token_summary,
    prompt,
    save_tokens,
)
from spotify_client.player import fetch_now_playing, print_now_playing, print_profile

# True prints Spotify JSON and the LRCLIB search, then waits for Enter.
# False hides those dumps and follows the current lyric line on its own.
PRINT_API_DETAILS = False
# How often to ask Spotify for the real playback position.
SPOTIFY_POLL_SECONDS = 1.0

# So we keep a local clock to help with lyric switching if between Spotify polls.
# How often to check the local clock for a new lyric pair. The pair itself is
# printed only when it changes.
LYRIC_DISPLAY_SECONDS = 0.1
# A Spotify poll can report progress_ms from a moment before the response
# arrives, so it lands slightly behind the local clock. Treat a rewind smaller
# than this as that delay. A larger rewind is a real seek and is applied.
SEEK_REWIND_MS = 2500


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


def follow_lyrics(session: dict, lyrics: LyricLookup) -> None:
    """Print each lyric pair once, using a local clock corrected by Spotify.

    Purpose:
        Stay aligned with playback without calling Spotify on every check.
        Spotify is polled about once per SPOTIFY_POLL_SECONDS. Between polls,
        progress_ms advances by the time spent only while Spotify reported
        that audio is playing. Each pair is the singing line and the next line
        in parentheses. It is printed when that pair's LRCLIB timestamps change.
        The last lyric is printed once with no next line.

    Parameters:
        session (dict): Saved Spotify login.
        lyrics (LyricLookup): Cache that maps a playback position to a lyric pair.

    Returns:
        None.

    Precondition:
        session can call the currently-playing endpoint.

    Postcondition:
        The latest lyric pair is on the terminal. The function returns when
        the user presses Ctrl+C.
    """
    session, now = fetch_now_playing(session)
    if lyrics.needs_lookup(now):
        lyrics.window_at(now, _progress_ms(now))
    print("Following the current lyric line. Press Ctrl+C to stop.")
    print()
    anchor_progress = _progress_ms(now)
    anchor_at = time.monotonic()
    playing = bool(now and now.get("is_playing"))
    last_poll = anchor_at
    last_track = _track_id(now)
    printed = False
    while True:
        if time.monotonic() - last_poll >= SPOTIFY_POLL_SECONDS:
            session, now = fetch_now_playing(session)
            reported = _progress_ms(now)
            elapsed_ms = int((time.monotonic() - anchor_at) * 1000) if playing else 0
            estimate = anchor_progress + elapsed_ms
            track = _track_id(now)
            small_rewind = (
                track is not None
                and track == last_track
                and reported < estimate
                and estimate - reported < SEEK_REWIND_MS
            )
            anchor_progress = estimate if small_rewind else reported
            anchor_at = time.monotonic()
            last_poll = anchor_at
            playing = bool(now and now.get("is_playing"))
            last_track = track
        elapsed_ms = int((time.monotonic() - anchor_at) * 1000) if playing else 0
        progress = anchor_progress + elapsed_ms
        duration = _duration_ms(now)
        if duration is not None:
            progress = min(progress, duration)
        shown = _lyric_print(lyrics, now, progress)
        if shown is not None:
            if printed:
                print()
            _print_lyric(shown)
            printed = True
        time.sleep(LYRIC_DISPLAY_SECONDS)


def _lyric_print(lyrics: LyricLookup, now: dict | None, progress_ms: int) -> LyricWindow | str | None:
    """Return the next lyric pair to print, or None when that pair is already saved.

    Purpose:
        Print only when the singing timestamp or the next timestamp differs
        from the pair saved on the lookup. The same words at a new timestamp
        are printed. The last lyric is printed once with no next line.

    Parameters:
        lyrics (LyricLookup): Cache that stores the two lines and their timestamps.
        now (dict | None): Spotify currently-playing body, or None.
        progress_ms (int): Playback position in milliseconds.

    Returns:
        LyricWindow | str | None: The pair to print, a status line, or None
        when nothing new should be printed.

    Precondition:
        progress_ms is >= 0.

    Postcondition:
        A returned LyricWindow is saved as singing_line and next_line.
        None leaves the saved pair unchanged.
    """
    if now is None:
        message = "Nothing is currently playing."
        if lyrics.status_is_showing(message):
            return None
        lyrics.note_status(message)
        return message
    track = _track_id(now)
    window = lyrics.window_at(now, progress_ms)
    if window is None:
        message = "No synced lyrics for this track."
        if lyrics.status_is_showing(message, track):
            return None
        lyrics.note_status(message, track)
        return message
    if lyrics.pair_is_showing(track, window):
        return None
    lyrics.remember_pair(track, window)
    return window


def _print_lyric(shown: LyricWindow | str) -> None:
    """Print the singing line and, when the song still has one, the next line."""
    if isinstance(shown, str):
        print(shown)
        return
    print(shown.singing.text)
    if shown.next_line is not None:
        print(f"({shown.next_line.text})")


def _track_id(now: dict | None) -> str | None:
    """Return the Spotify track id, or None when nothing is playing."""
    item = (now or {}).get("item") or {}
    track_id = item.get("id")
    if isinstance(track_id, str) and track_id:
        return track_id
    return None


def _progress_ms(now: dict | None) -> int:
    """Return Spotify's progress_ms, or 0 when nothing is playing."""
    if not now or not isinstance(now.get("progress_ms"), int):
        return 0
    return now["progress_ms"]


def _duration_ms(now: dict | None) -> int | None:
    """Return the track length in milliseconds, or None when Spotify sent no item."""
    item = (now or {}).get("item") or {}
    duration = item.get("duration_ms")
    if isinstance(duration, int):
        return duration
    return None


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

    lrclib_client.VERBOSE = PRINT_API_DETAILS
    lyrics = LyricLookup()
    session = print_profile(session, verbose=PRINT_API_DETAILS)
    if PRINT_API_DETAILS:
        session, now = print_now_playing(session)
        lyrics.show(now)
        watch_keyboard(session, lyrics)
        return
    follow_lyrics(session, lyrics)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(1)
    except Exception as exc:
        print(f"\nCould not connect: {exc}")
        raise SystemExit(1)
