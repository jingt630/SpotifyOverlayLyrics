"""Read the Spotify desktop app through its Mac player.

Spotify reports the title, artist, album, play or pause, and a position.
Those become the same shape as a Spotify currently-playing body so lyric
lookup does not care which source filled it in. Nothing here listens to
audio or calls the Spotify Web API.

A one-second read is the backup for a change that arrives between checks.
Both the read and the previous, next, pause, and play commands stay on
this Mac. They do not use the Web API quota.
"""

from __future__ import annotations

import queue
import subprocess
import threading

from lrclib_client import media_choice_key

# How often to read the player when no command has arrived.
MAC_POLL_SECONDS = 1.0

_READ_SCRIPT = r'''
tell application "Spotify"
    set stateText to player state as text
    if stateText is "stopped" then
        return "NONE"
    end if
    set trackName to name of current track
    set trackArtist to artist of current track
    set trackAlbum to album of current track
    set trackDuration to duration of current track
    set trackPosition to player position
end tell
set sep to (ASCII character 31)
return trackName & sep & trackArtist & sep & trackAlbum & sep & (trackDuration as text) & sep & (trackPosition as text) & sep & stateText
'''

_COMMANDS = {
    "previous": 'tell application "Spotify" to previous track',
    "next": 'tell application "Spotify" to next track',
    "pause": 'tell application "Spotify" to pause',
    "resume": 'tell application "Spotify" to play',
}


class MacPlayback:
    """Keep the latest Spotify player sample as a currently-playing body.

    The body uses item.name, item.artists, item.album, item.duration_ms,
    item.id, progress_ms, and is_playing. item.id is the title, artist, and
    album key, because the player has no Spotify track id.
    """

    def __init__(self) -> None:
        """Start with no sample. Call start before latest or command.

        Purpose:
            Hold the lock and the background thread that talks to Spotify.

        Parameters:
            None.

        Returns:
            None.

        Precondition:
            None.

        Postcondition:
            latest returns no song until start has finished.
        """
        self._lock = threading.Lock()
        self._now: dict | None = None
        self._generation = 0
        self._problem: str | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._commands: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="macos-media", daemon=True)

    def start(self) -> None:
        """Read the player once and keep reading about once a second.

        Purpose:
            Make latest and command usable from the overlay thread.

        Parameters:
            None.

        Returns:
            None.

        Precondition:
            Spotify's scripting interface can be used from this app.

        Postcondition:
            The background thread is running, or start raises the first error.
        """
        self._thread.start()
        if not self._ready.wait(120):
            raise TimeoutError(
                "Spotify did not respond. If macOS asked to control Spotify, allow it and open the app again."
            )
        if self._error is not None:
            raise self._error

    def latest(self) -> tuple[dict | None, int]:
        """Return the newest playback body and a counter that changes each read.

        Purpose:
            Let the overlay apply a new sample as soon as the one-second read
            publishes one.

        Parameters:
            None.

        Returns:
            tuple[dict | None, int]: The currently-playing body, or None when
            Spotify has no song, and the sample counter.

        Precondition:
            start has been called.

        Postcondition:
            The player is unchanged.
        """
        with self._lock:
            return self._now, self._generation

    def problem(self) -> str | None:
        """Return an overlay notice key when macOS is blocking Spotify."""
        with self._lock:
            return self._problem

    def command(self, action: str) -> str | None:
        """Send previous, next, pause, or resume to the Spotify player.

        Purpose:
            Drive the desktop app from the overlay without a Web API scope.

        Parameters:
            action (str): previous, next, pause, or resume.

        Returns:
            str | None: An overlay notice key when the command cannot run.
            None when Spotify accepted it.

        Precondition:
            start has finished and the thread is running.

        Postcondition:
            A successful command asks Spotify to change playback and then
            publishes a new sample.
        """
        if action not in _COMMANDS:
            return "playback_failed"
        done = threading.Event()
        box: dict[str, str | None] = {}
        self._commands.put((action, box, done))
        if not done.wait(8):
            return "playback_failed"
        return box.get("error")

    def _run(self) -> None:
        """Own the thread that reads the player and sends commands."""
        try:
            self._read()
        except Exception as exc:
            self._error = exc
        self._ready.set()
        if self._error is not None:
            return
        while True:
            try:
                action, box, done = self._commands.get(timeout=MAC_POLL_SECONDS)
            except queue.Empty:
                self._read()
                continue
            try:
                box["error"] = self._send(action)
            except Exception:
                box["error"] = "playback_failed"
            done.set()
            self._read()

    def _read(self) -> None:
        """Publish Spotify's song, or None when it is not open."""
        if not _spotify_running():
            self._publish(None, None)
            return
        text, problem = _execute(_READ_SCRIPT)
        if problem == "automation" and not self._ready.is_set():
            self._error = RuntimeError(
                "macOS blocked control of Spotify. Open System Settings → Privacy & Security → Automation, "
                "allow this app to control Spotify, then open it again."
            )
        now = None
        if problem is None and text and text != "NONE":
            now = _currently_playing(text)
        self._publish(now, "automation" if problem == "automation" else None)

    def _publish(self, now: dict | None, problem: str | None) -> None:
        """Store one sample for the overlay thread."""
        with self._lock:
            self._now = now
            self._problem = problem
            self._generation += 1

    def _send(self, action: str) -> str | None:
        """Run one transport command on the player thread."""
        if not _spotify_running():
            return "need_player"
        _text, problem = _execute(_COMMANDS[action])
        if problem == "automation":
            return "automation"
        if problem is not None:
            return "playback_failed"
        return None


def _spotify_running() -> bool:
    """Return True when the Spotify app process is open."""
    completed = subprocess.run(
        ["/usr/bin/pgrep", "-x", "Spotify"],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def _execute(source: str) -> tuple[str | None, str | None]:
    """Run one Spotify script.

    Returns:
        tuple[str | None, str | None]: The script text and None, or None and
        "automation" or "failed".
    """
    from Foundation import NSAppleScript

    script = NSAppleScript.alloc().initWithSource_(source)
    if script is None:
        return None, "failed"
    result, error = script.executeAndReturnError_(None)
    if error is not None:
        number = _error_number(error)
        if number == -1743:
            return None, "automation"
        return None, "failed"
    if result is None:
        return "", None
    text = result.stringValue()
    return ("" if text is None else str(text).strip()), None


def _error_number(error) -> int | None:
    """Return the AppleScript error code, or None when it is missing."""
    try:
        return int(error["NSAppleScriptErrorNumber"])
    except (KeyError, TypeError, ValueError):
        return None


def _currently_playing(text: str) -> dict | None:
    """Copy one script result into a currently-playing body."""
    parts = text.split("\x1f")
    if len(parts) != 6:
        return None
    title, artist, album, duration_text, position_text, state = parts
    title = title.strip()
    artist = artist.strip()
    album = album.strip()
    if not title or not artist:
        return None
    try:
        duration_ms = int(round(float(duration_text)))
        progress_ms = int(round(float(position_text) * 1000))
    except ValueError:
        return None
    if duration_ms <= 0:
        return None
    if progress_ms < 0:
        progress_ms = 0
    if progress_ms > duration_ms:
        progress_ms = duration_ms
    return {
        "is_playing": state.strip().lower() == "playing",
        "progress_ms": progress_ms,
        "item": {
            "type": "track",
            "id": media_choice_key(title, artist, album),
            "name": title,
            "duration_ms": duration_ms,
            "artists": [{"name": artist}],
            "album": {"name": album},
        },
    }
