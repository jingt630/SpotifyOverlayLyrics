"""Show LRCLIB and NetEase lyrics for whatever Spotify is playing on this Mac.

Playback comes from the Spotify desktop player. Lyric lookup lives in
lrclib_client.py. NetEase word times live in netease_lyrics.py.
"""

from __future__ import annotations

import os
import sys
import time

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

import lrclib_client
from lyric_overlay import LyricOverlay
import lyric_catalog
from netease_lyrics import NeteaseLookup
from lrclib_client import LrcLibError, LyricLookup, LyricWindow

# True prints the LRCLIB search. False follows the current lyric line on its own.
PRINT_API_DETAILS = False

# The local clock moves the lyric between player samples.
# How often to check the local clock for a new lyric pair. The pair itself is
# printed only when it changes.
LYRIC_DISPLAY_SECONDS = 0.1
# Word fills repaint with the display. 16 ms is about 60 frames a second.
WORD_PAINT_MS = 16
# A Spotify poll can report progress_ms from a moment before the response
# arrives, so it lands slightly behind the local clock. Treat a rewind smaller
# than this as that delay. A larger rewind is a real seek and is applied.
SEEK_REWIND_MS = 2500


def _lyric_print(lyrics: LyricLookup, now: dict | None, progress_ms: int) -> LyricWindow | str | list | None:
    """Return the next lyric pair, a status, or candidate rows.

    Purpose:
        Print only when the singing line or the following line changes.
        A busy lookup, a missing lyric, and an undecided match become
        overlay status instead of a lyric pair.

    Parameters:
        lyrics (LyricLookup): Cache that stores the two lines and their timestamps.
        now (dict | None): Current playback body, or None.
        progress_ms (int): Playback position in milliseconds.

    Returns:
        LyricWindow | str | list | None: The pair to show, a status key, the
        candidate list, or None when nothing new should be drawn.

    Precondition:
        progress_ms is >= 0.

    Postcondition:
        A returned LyricWindow is saved as the pair now showing.
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


def _track_id(now: dict | None) -> str | None:
    """Return the current song key, or None when nothing is playing."""
    item = (now or {}).get("item") or {}
    track_id = item.get("id")
    if isinstance(track_id, str) and track_id:
        return track_id
    return None


def _progress_ms(now: dict | None) -> int:
    """Return the playback position in milliseconds, or 0 when nothing is playing."""
    if not now or not isinstance(now.get("progress_ms"), int):
        return 0
    return now["progress_ms"]


def _duration_ms(now: dict | None) -> int | None:
    """Return the track length in milliseconds, or None when no track is playing."""
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

    def __init__(self, lyrics: LyricLookup, window: LyricOverlay) -> None:
        """Store the lyric cache and the window to update.

        Purpose:
            Prepare one timer tick of playback following.

        Parameters:
            lyrics (LyricLookup): Cache that maps a playback position to a pair.
            window (LyricOverlay): Surface that draws the pair.

        Returns:
            None.

        Precondition:
            A QApplication is already running.

        Postcondition:
            No media sample has been read. The timer is not started.
        """
        super().__init__()
        self.playback = None
        self.lyrics = lyrics
        self.netease = NeteaseLookup()
        self._netease_announced: tuple | None = None
        self._seen_feedback = ""
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
        problem = self.playback.problem() if self.playback is not None else None
        if problem and now is None:
            self.playing = False
            self.window.set_playing(False)
            if self._shown != problem:
                self._shown = problem
                self.window.set_status(problem)
            return
        elapsed_ms = int((time.monotonic() - self.anchor_at) * 1000) if self.playing else 0
        progress = self.anchor_progress + elapsed_ms
        duration = _duration_ms(now)
        if duration is not None:
            progress = min(progress, duration)
        self.window.set_playing(self.playing)
        self.netease.consider(now, self._seeked)
        self._sync_feedback()
        self._seeked = False
        phase = self.netease.phase()
        song_key = self.netease.song_key()
        if phase == "choose":
            choice_key = ("netease-choose", song_key)
            if self._shown != choice_key:
                self._shown = choice_key
                self.window.set_choices(self.netease.candidates(), self.netease.accept, "netease_pick")
            return
        if phase == "lines":
            slots = self.netease.line_slots(progress, self.window.chinese_script())
            if slots is not None and slots != self._shown:
                self._shown = slots
                self.window.set_lines(slots[0], slots[1], slots[2])
            return
        if phase == "miss":
            self._set_paint_interval(False)
            self._show_lrclib(now, progress)
            return
        if phase == "search":
            self._warm_lrclib(now, progress)
            if self._shown != "netease_search":
                self._shown = "netease_search"
                self.window.set_status("netease_search")
            return
        if phase in ("none", "error"):
            token = (song_key, phase, self.netease.reason_key())
            if self._netease_announced != token:
                self._netease_announced = token
                status = "netease_error" if phase == "error" else "netease_none"
                if self.netease.reason_key():
                    status = self.netease.reason_key()
                sentence = self.window.text(status)
                detail = self.netease.detail().strip()
                if detail:
                    sentence = f"{sentence} {detail}"
                self.window.set_feedback(sentence)
                self._seen_feedback = lyric_catalog.feedback_line()
        if phase in ("saved", "words"):
            frame = self.netease.frame_at(progress, self.window.chinese_script())
            if frame is not None:
                self._set_paint_interval(self.playing)
                if frame != self._shown:
                    self._shown = frame
                    self.window.set_words(frame[1], frame[2], frame[0])
                return
        self._set_paint_interval(False)
        self._show_lrclib(now, progress)

    def _sync_feedback(self) -> None:
        """Copy the latest lyric-source line into the corner."""
        line = lyric_catalog.feedback_line()
        if not line or line == self._seen_feedback:
            return
        self._seen_feedback = line
        self.window.set_feedback(line)

    def _show_lrclib(self, now: dict | None, progress: int) -> None:
        """Draw LRCLIB lines, or NetEase line lyrics when LRCLIB has none."""
        try:
            shown = _lyric_print(self.lyrics, now, progress)
        except LrcLibError:
            shown = "busy"
        if shown is None:
            self._settle_catalog(now, progress)
            return
        if isinstance(shown, list):
            self._shown = ("choose", _track_id(now) or "")
            self.window.set_choices(shown, self.lyrics.accept_candidate)
            return
        if isinstance(shown, str):
            if shown == "none":
                self._settle_catalog(now, progress)
                return
            if shown == self._shown:
                return
            self._shown = shown
            self.window.set_status(shown)
            return
        slots = self.lyrics.slots_at(now, progress, self.window.chinese_script())
        if slots is None or slots == self._shown:
            return
        self._shown = slots
        self.window.set_lines(slots[0], slots[1], slots[2])

    def _settle_catalog(self, now: dict | None, progress: int) -> None:
        """Store a shared miss, or keep NetEase line lyrics when LRCLIB has none.

        Purpose:
            A personal pick is already on disk. This runs only after both
            lookups have finished without asking the user to choose.

        Parameters:
            now (dict | None): Current playback body.
            progress (int): Playback position in milliseconds.

        Returns:
            None.

        Precondition:
            The NetEase phase is no longer search or choose.

        Postcondition:
            A held NetEase line lyric is shared and drawn. A double miss is
            recorded so the next play does not search again.
        """
        if self.netease.phase() in ("search", "choose", "error", "lines", "words", "saved"):
            return
        query = lrclib_client.spotify_track_query(now)
        if query is None or lyric_catalog.local_get(query) is not None:
            return
        track = query["key"]
        if self.lyrics.pending_choices(track) or self.lyrics.is_busy(track):
            return
        if self.lyrics.has_match(track):
            lyric_catalog.drop_hold(query)
            return
        if not self.lyrics.lacks_synced_lyrics(track):
            return
        held = lyric_catalog.held_lines(query)
        if held:
            lyric_catalog.drop_hold(query)
            lyric_catalog.put_shared(query, 0, held)
            lyric_catalog.announce(query, "NetEase", "line sync")
            self.netease.adopt_lines(query, held)
            slots = self.netease.line_slots(progress, self.window.chinese_script())
            if slots is not None:
                self._shown = slots
                self.window.set_lines(slots[0], slots[1], slots[2])
            return
        lyric_catalog.put_miss(query)
        lyric_catalog.announce(query, "NetEase and LRCLIB", "no synced lyric")
        self.netease.mark_miss(query)

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
        error = self.playback.command(action)
        if error:
            self.playing = not self.playing
            self.window.set_playing(self.playing)
            self.window.set_notice(error)

    def _command(self, action: str) -> None:
        """Send one playback command and surface a refusal on the overlay."""
        error = self.playback.command(action)
        if error:
            self.window.set_notice(error)

    def _poll_if_due(self) -> dict | None:
        """Return the latest Spotify player sample."""
        return self._poll_playback()

    def _poll_playback(self) -> dict | None:
        """Apply a player sample when the one-second read published one."""
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


def run_overlay(lyrics: LyricLookup) -> None:
    """Open the overlay and follow Spotify through the Mac player.

    Purpose:
        Show the same lyric pair as the Web API path, using title, artist,
        album, and position from the desktop app.

    Parameters:
        lyrics (LyricLookup): Cache whose choice map is the player song key.

    Returns:
        None.

    Precondition:
        This Mac can ask Spotify for the current song.

    Postcondition:
        The function returns when the overlay window is closed. Spotify's
        LRCLIB id maps under .spotify are not written.
    """
    from macos_media import MacPlayback

    app = QApplication.instance() or QApplication([])
    try:
        playback = MacPlayback()
        playback.start()
    except Exception as exc:
        QMessageBox.critical(
            None,
            "Spotify Lyric Overlay",
            f"Could not read Spotify: {exc}",
        )
        raise SystemExit(1)
    window = LyricOverlay()
    window.set_status("loading")
    follow = OverlayFollow(lyrics, window)
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


def main() -> None:
    """Open the overlay and follow the Spotify player.

    Purpose:
        Read the song playing on this Mac and show its lyrics.

    Parameters:
        None.

    Returns:
        None.

    Precondition:
        This Mac can ask Spotify for the current song.

    Postcondition:
        The function returns when the overlay window is closed.
    """
    # The packaged app has no console. A source run still prints safely.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    lrclib_client.VERBOSE = PRINT_API_DETAILS
    lrclib_client.OVERLAY_CHOICES = not PRINT_API_DETAILS
    lyrics = LyricLookup()
    run_overlay(lyrics)


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
