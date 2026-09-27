"""Connect Spotify and show the LRCLIB lyric line for the current position.

Run this file in a terminal. Login and token storage live in
spotify_client/auth.py. Profile and now-playing calls live in
spotify_client/player.py. Lyric lookup lives in lrclib_client.py.

The JSON shapes Spotify returns are in spotify_api_formats.md.
The function and URL flow is in spotify_flow.md.
"""

from __future__ import annotations

import os
import sys
import time

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

import lrclib_client
from lyric_overlay import LyricOverlay
from netease_lyrics import NeteaseLookup
from lrclib_client import LrcLibError, LyricLookup, LyricWindow, format_clock, windows_choice_store
from spotify_client.auth import (
    TOKEN_PATH,
    authorize,
    load_saved_session,
    print_setup,
    print_token_summary,
    prompt,
    save_tokens,
)
from spotify_client.player import fetch_now_playing, player_command, print_now_playing, print_profile

# True prints Spotify JSON and the LRCLIB search, then waits for Enter.
# False hides those dumps and follows the current lyric line on its own.
PRINT_API_DETAILS = False
# "windows" reads the Spotify desktop app from the Windows media session.
# "spotify" polls the Web API. The lyric overlay and LRCLIB filtering are the same.
PLAYBACK_SOURCE = "windows"
# How often to ask Spotify for the real playback position.
SPOTIFY_POLL_SECONDS = 10.0
# A track with no synced lyrics only needs a check to notice a song change.
SPOTIFY_POLL_NO_LYRICS_SECONDS = 30.0

# So we keep a local clock to help with lyric switching if between Spotify polls.
# How often to check the local clock for a new lyric pair. The pair itself is
# printed only when it changes.
LYRIC_DISPLAY_SECONDS = 0.1
# Word fills repaint with the display. 16 ms is about 60 frames a second.
WORD_PAINT_MS = 16
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
        Spotify is polled about once per SPOTIFY_POLL_SECONDS, or once per
        SPOTIFY_POLL_NO_LYRICS_SECONDS when this track has no synced lyrics.
        Between polls, progress_ms advances by the time spent only while
        Spotify reported that audio is playing. Each pair is the singing line
        and the next line in parentheses. It is
        printed when that pair's LRCLIB timestamps change. A new track prints
        its title and artist first. While paused, Spotify still returns the
        item and progress_ms with is_playing false, and the pause position is
        printed once as (currently paused at m:ss).

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
    prev_track_id: str | None = None
    pause_at_ms: int | None = None
    showing_pause = False
    printed = False
    while True:
        if time.monotonic() - last_poll >= _poll_wait_seconds(lyrics, now):
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
        track = _track_id(now)
        song = _track_header(now)
        artist = _track_artists(now)
        if track is not None and track != prev_track_id:
            if printed:
                print()
            print(_format_track_label(song, artist))
            printed = True
            showing_pause = False
            prev_track_id = track
            pause_at_ms = None
        shown = _lyric_print(lyrics, now, progress)
        if shown is not None:
            if printed:
                print()
            _print_lyric(shown)
            printed = True
        if now is not None and track is not None and not playing:
            if not showing_pause or pause_at_ms != progress:
                if printed:
                    print()
                print(f"(currently paused at {format_clock(progress)})")
                printed = True
                showing_pause = True
                pause_at_ms = progress
        elif playing:
            showing_pause = False
            pause_at_ms = None
        if now is None:
            prev_track_id = None
            showing_pause = False
            pause_at_ms = None
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
        if lyrics.status_is_showing("idle"):
            return None
        lyrics.note_status("idle")
        return "idle"
    track = _track_id(now)
    window = lyrics.window_at(now, progress_ms)
    if lyrics.is_busy(track):
        if lyrics.status_is_showing("busy", track):
            return None
        lyrics.note_status("busy", track)
        return "busy"
    pending = lyrics.pending_choices(track)
    if pending is not None:
        if lyrics.status_is_showing("choose", track):
            return None
        lyrics.note_status("choose", track)
        return pending
    if window is None:
        if lyrics.status_is_showing("none", track):
            return None
        lyrics.note_status("none", track)
        return "none"
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


def _format_track_label(song: str | None, artist: str | None) -> str:
    """Return one line with the song title and artist names."""
    if song and artist:
        return f"{song} — {artist}"
    if song:
        return song
    return "Unknown track"


def _poll_wait_seconds(lyrics: LyricLookup, now: dict | None) -> float:
    """Return how long to wait before the next currently-playing request.

    Purpose:
        Check every 10 seconds while synced lyrics can move, and every 30
        seconds after LRCLIB has no synced record for this track. While audio
        is playing and less than that interval remains in the song, the next
        check is when the track should end, so the next song's lyrics can start.

    Parameters:
        lyrics (LyricLookup): Cache that remembers a confirmed miss.
        now (dict | None): Latest currently-playing body, or None.

    Returns:
        float: Seconds until the next Spotify poll.

    Precondition:
        lyrics has already recorded the lookup result for this track, if any.

    Postcondition:
        No network call is made.
    """
    if lyrics.lacks_synced_lyrics(_track_id(now)):
        base = SPOTIFY_POLL_NO_LYRICS_SECONDS
    else:
        base = SPOTIFY_POLL_SECONDS
    if not (now and now.get("is_playing")):
        return base
    duration = _duration_ms(now)
    if duration is None:
        return base
    remaining = (duration - _progress_ms(now)) / 1000
    if remaining <= 0:
        return 1.0
    if remaining < base:
        return remaining
    return base


def _track_header(now: dict | None) -> str | None:
    """Return the track title from a currently-playing body, or None."""
    item = (now or {}).get("item") or {}
    title = item.get("name")
    if isinstance(title, str) and title:
        return title
    return None


def _track_artists(now: dict | None) -> str | None:
    """Return artist names joined with ', ', or None when nothing is playing."""
    item = (now or {}).get("item") or {}
    names = [artist.get("name", "") for artist in item.get("artists", []) if artist.get("name")]
    if not names:
        return None
    return ", ".join(names)


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


class OverlayFollow(QObject):
    """Push the current lyric pair into the glass window.

    Between playback samples the local clock moves the pair forward only
    while audio is playing. The window is repainted when the singing line
    or the next line changes.
    """

    def __init__(self, session: dict | None, lyrics: LyricLookup, window: LyricOverlay) -> None:
        """Store the login, the lyric cache, and the window to update.

        Purpose:
            Prepare one timer tick of playback following.

        Parameters:
            session (dict): Saved Spotify login.
            lyrics (LyricLookup): Cache that maps a playback position to a pair.
            window (LyricOverlay): Glass surface that draws the pair.

        Returns:
            None.

        Precondition:
            A QApplication is already running.

        Postcondition:
            No Spotify call has been made. The timer is not started.
        """
        super().__init__()
        self.session = session
        self.playback = None
        self.lyrics = lyrics
        self.netease = NeteaseLookup()
        self._netease_announced: tuple | None = None
        self._netease_hold_until = 0.0
        self._seeked = False
        self.window = window
        self.anchor_progress = 0
        self.anchor_at = time.monotonic()
        self.playing = False
        self.last_poll = 0.0
        self.last_track: str | None = None
        self._seen_generation = -1
        self._now: dict | None = None
        self._shown: tuple | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(int(LYRIC_DISPLAY_SECONDS * 1000))
        self.timer.timeout.connect(self._tick)

    def start(self) -> None:
        """Read playback once the window is on screen, then keep updating it."""
        QTimer.singleShot(0, self._tick)
        self.timer.start()

    def _tick(self) -> None:
        """Advance the local clock and repaint when the lyric pair changes."""
        now = self._poll_if_due()
        elapsed_ms = int((time.monotonic() - self.anchor_at) * 1000) if self.playing else 0
        progress = self.anchor_progress + elapsed_ms
        duration = _duration_ms(now)
        if duration is not None:
            progress = min(progress, duration)
        self.window.set_playing(self.playing)
        self.netease.consider(now, self._seeked)
        self._seeked = False
        phase = self.netease.phase()
        song_key = self.netease.song_key()
        if phase == "choose":
            choice_key = ("netease-choose", song_key)
            if self._shown != choice_key:
                self._shown = choice_key
                self.window.set_choices(self.netease.candidates(), self.netease.accept, "netease_pick")
            return
        if phase == "search":
            self._warm_lrclib(now, progress)
            if self._shown != "netease_search":
                self._shown = "netease_search"
                self.window.set_status("netease_search")
            return
        if phase in ("saved", "none", "error"):
            token = (song_key, phase, self.netease.reason_key())
            if self._netease_announced != token:
                self._netease_announced = token
                hold = 2.0 if phase == "saved" else 4.0
                self._netease_hold_until = time.monotonic() + hold
                status = {"saved": "netease_saved", "none": "netease_none", "error": "netease_error"}[phase]
                if phase != "saved" and self.netease.reason_key():
                    status = self.netease.reason_key()
                self._shown = ("netease", song_key, status)
                self.window.set_status(status, self.netease.detail())
                return
            if time.monotonic() < self._netease_hold_until:
                return
        if phase in ("saved", "words"):
            frame = self.netease.frame_at(progress)
            if frame is not None:
                self._set_paint_interval(self.playing)
                if frame != self._shown:
                    self._shown = frame
                    self.window.set_words(frame[1], frame[2], frame[0])
                return
        self._set_paint_interval(False)
        try:
            shown = _lyric_print(self.lyrics, now, progress)
        except LrcLibError:
            shown = "busy"
        if shown is None:
            return
        if isinstance(shown, list):
            self._shown = ("choose", _track_id(now) or "")
            self.window.set_choices(shown, self.lyrics.accept_candidate)
            return
        if isinstance(shown, str):
            if shown == self._shown:
                return
            self._shown = shown
            self.window.set_status(shown)
            return
        slots = self.lyrics.slots_at(now, progress)
        if slots is None or slots == self._shown:
            return
        self._shown = slots
        self.window.set_lines(slots[0], slots[1], slots[2])

    def _set_paint_interval(self, fast: bool) -> None:
        """Repaint word fills at display rate, and line changes on the slower clock.

        Purpose:
            A 100 ms step is enough to switch lyric lines. A word wipe needs
            a new frame about 60 times a second or the color jumps forward.

        Parameters:
            fast (bool): True while a NetEase word line is on screen and audio
                is playing.

        Returns:
            None.

        Precondition:
            The overlay timer exists.

        Postcondition:
            The timer interval is WORD_PAINT_MS or LYRIC_DISPLAY_SECONDS.
        """
        interval = WORD_PAINT_MS if fast else int(LYRIC_DISPLAY_SECONDS * 1000)
        if self.timer.interval() != interval:
            self.timer.setInterval(interval)

    def _warm_lrclib(self, now: dict | None, progress_ms: int) -> None:
        """Load the LRCLIB line lyric while NetEase is still being checked.

        Purpose:
            The lyric area shows the NetEase status first. The LRCLIB lines
            are ready when that check finishes.

        Parameters:
            now (dict | None): Current playback body.
            progress_ms (int): Local playback position.

        Returns:
            None.

        Precondition:
            progress_ms is >= 0.

        Postcondition:
            A busy LRCLIB response is ignored. The overlay text is unchanged.
        """
        try:
            self.lyrics.window_at(now, progress_ms)
        except LrcLibError:
            return

    def previous(self) -> None:
        """Ask the active source to go to the previous track."""
        self._command("previous")

    def next(self) -> None:
        """Ask the active source to go to the next track."""
        self._command("next")

    def toggle_pause(self) -> None:
        """Pause when audio is playing, or resume when it is paused."""
        action = "pause" if self.playing else "resume"
        self.playing = not self.playing
        self.window.set_playing(self.playing)
        if self.playback is not None:
            error = self.playback.command(action)
        else:
            self.session, error = player_command(self.session, action)
        if error:
            self.playing = not self.playing
            self.window.set_playing(self.playing)
            self.window.set_notice(error)

    def _command(self, action: str) -> None:
        """Send one playback command and surface a refusal on the overlay."""
        if self.playback is not None:
            error = self.playback.command(action)
        else:
            self.session, error = player_command(self.session, action)
        if error:
            self.window.set_notice(error)

    def _poll_if_due(self) -> dict | None:
        """Return the latest playback body, reading when a new sample is due."""
        if self.playback is not None:
            return self._poll_windows()
        wait = _poll_wait_seconds(self.lyrics, self._now)
        if time.monotonic() - self.last_poll < wait and self.last_poll != 0.0:
            return self._now
        self.session, now = fetch_now_playing(self.session)
        reported = _progress_ms(now)
        elapsed_ms = int((time.monotonic() - self.anchor_at) * 1000) if self.playing else 0
        estimate = self.anchor_progress + elapsed_ms
        track = _track_id(now)
        small_rewind = (
            track is not None
            and track == self.last_track
            and reported < estimate
            and estimate - reported < SEEK_REWIND_MS
        )
        self._seeked = _position_jumped(track, self.last_track, reported, estimate, small_rewind)
        self.anchor_progress = estimate if small_rewind else reported
        self.anchor_at = time.monotonic()
        self.last_poll = self.anchor_at
        self.playing = bool(now and now.get("is_playing"))
        self.last_track = track
        self._now = now
        return now

    def _poll_windows(self) -> dict | None:
        """Apply a Windows sample when an event or the one-second read published one."""
        now, generation = self.playback.latest()
        if generation == self._seen_generation:
            return self._now
        self._seen_generation = generation
        reported = _progress_ms(now)
        elapsed_ms = int((time.monotonic() - self.anchor_at) * 1000) if self.playing else 0
        estimate = self.anchor_progress + elapsed_ms
        track = _track_id(now)
        small_rewind = (
            track is not None
            and track == self.last_track
            and reported < estimate
            and estimate - reported < SEEK_REWIND_MS
        )
        self._seeked = _position_jumped(track, self.last_track, reported, estimate, small_rewind)
        self.anchor_progress = estimate if small_rewind else reported
        self.anchor_at = time.monotonic()
        self.last_poll = self.anchor_at
        self.playing = bool(now and now.get("is_playing"))
        self.last_track = track
        self._now = now
        return now


def _position_jumped(
    track: str | None,
    last_track: str | None,
    reported: int,
    estimate: int,
    small_rewind: bool,
) -> bool:
    """Return True when playback skipped inside the same song.

    Purpose:
        A small rewind is the delay between a playback sample and the local
        clock. A larger jump, forward or back, is the user skipping inside
        the track.

    Parameters:
        track (str | None): Current track id.
        last_track (str | None): Track id from the previous sample.
        reported (int): Position from the new sample, in milliseconds.
        estimate (int): Position the local clock expected.
        small_rewind (bool): True when the sample is only slightly behind.

    Returns:
        bool: True for a skip inside the current song.

    Precondition:
        reported and estimate are millisecond positions.

    Postcondition:
        A song change is not reported as a skip.
    """
    if track is None or track != last_track or small_rewind:
        return False
    return abs(reported - estimate) >= SEEK_REWIND_MS


def run_windows_overlay(lyrics: LyricLookup) -> None:
    """Open the overlay and follow Spotify through the Windows media session.

    Purpose:
        Show the same lyric pair as the Web API path, using title, artist,
        album, and position from the desktop app.

    Parameters:
        lyrics (LyricLookup): Cache whose choice map is the Windows song key.

    Returns:
        None.

    Precondition:
        The WinRT media-control packages are installed.

    Postcondition:
        The function returns when the overlay window is closed. Spotify's
        lyric_choices.json and lyric_catalog.csv are not written.
    """
    from windows_media import WindowsPlayback

    app = QApplication.instance() or QApplication([])
    try:
        playback = WindowsPlayback()
        playback.start()
    except Exception as exc:
        QMessageBox.critical(
            None,
            "Spotify Lyric Overlay",
            f"Could not read the Windows media session: {exc}",
        )
        raise SystemExit(1)
    window = LyricOverlay()
    window.set_status("loading")
    follow = OverlayFollow(None, lyrics, window)
    follow.playback = playback
    window.set_transport(follow.previous, follow.toggle_pause, follow.next)
    frame = window.frameGeometry()
    screen = app.primaryScreen().availableGeometry().center()
    frame.moveCenter(screen)
    window.move(frame.topLeft())
    window.show()
    window.raise_()
    window.activateWindow()
    follow.start()
    raise SystemExit(app.exec())


def run_overlay(session: dict, lyrics: LyricLookup) -> None:
    """Open the glass window and follow the Spotify lyric pair in it.

    Purpose:
        Show the singing line and the next line on the overlay instead of
        the terminal.

    Parameters:
        session (dict): Saved Spotify login that can call currently-playing.
        lyrics (LyricLookup): Cache for the current track.

    Returns:
        None.

    Precondition:
        session can call the currently-playing endpoint.

    Postcondition:
        The function returns when the overlay window is closed.
    """
    app = QApplication.instance() or QApplication([])
    window = LyricOverlay()
    window.set_status("loading")
    follow = OverlayFollow(session, lyrics, window)
    window.set_transport(follow.previous, follow.toggle_pause, follow.next)
    frame = window.frameGeometry()
    screen = app.primaryScreen().availableGeometry().center()
    frame.moveCenter(screen)
    window.move(frame.topLeft())
    window.show()
    window.raise_()
    window.activateWindow()
    follow.start()
    raise SystemExit(app.exec())


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
    # pythonw.exe, used by the desktop shortcut, has no console.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    lrclib_client.VERBOSE = PRINT_API_DETAILS
    lrclib_client.OVERLAY_CHOICES = not PRINT_API_DETAILS
    if PLAYBACK_SOURCE == "windows":
        lyrics = LyricLookup(choices=windows_choice_store())
        run_windows_overlay(lyrics)
        return

    session = load_saved_session()
    if session:
        if sys.stdin is not None and sys.stdin.isatty():
            print("Using the Spotify login already saved in .spotify/tokens.json")
    elif sys.stdin is None or not sys.stdin.isatty():
        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(
            None,
            "Spotify Lyric Overlay",
            "Spotify login is not saved yet. Run connect_spotify.py in a terminal once, then open this shortcut again.",
        )
        raise SystemExit(1)
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
    session = print_profile(session, verbose=PRINT_API_DETAILS)
    if PRINT_API_DETAILS:
        session, now = print_now_playing(session)
        lyrics.show(now)
        watch_keyboard(session, lyrics)
        return
    run_overlay(session, lyrics)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(1)
    except Exception as exc:
        message = f"Could not connect: {exc}"
        if sys.stdin is None or not sys.stdin.isatty():
            app = QApplication.instance() or QApplication([])
            QMessageBox.critical(None, "Spotify Lyric Overlay", message)
        else:
            print(f"\n{message}")
        raise SystemExit(1)
