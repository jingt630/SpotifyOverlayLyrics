"""Read the Spotify desktop app through the Windows media session.

The session reports title, artist, album, play or pause, and a position.
Those become the same shape as a Spotify currently-playing body so lyric
lookup does not care which source filled it in. Nothing here calls the
Spotify Web API.

Windows also raises an event when the song, play state, or position changes.
A one-second read is the backup for an event that does not arrive. Both are
local. They do not use the Web API quota.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

from lrclib_client import media_choice_key

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus,
    )
except ImportError:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus,
    )

# How often to read the session when no event has arrived.
WINDOWS_POLL_SECONDS = 1.0


class WindowsPlayback:
    """Keep the latest Spotify session as a currently-playing body.

    The body uses item.name, item.artists, item.album, item.duration_ms,
    item.id, progress_ms, and is_playing. item.id is the title, artist, and
    album key, because the session has no Spotify track id.
    """

    def __init__(self) -> None:
        """Start with no sample. Call start before latest or command.

        Purpose:
            Hold the lock and the background loop that talks to Windows.

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
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, name="windows-media", daemon=True)
        self._manager = None
        self._watched = None
        self._tokens: list[tuple] = []
        self._refresh_lock: asyncio.Lock | None = None
        self._refresh_scheduled = False
        self._dirty = False

    def start(self) -> None:
        """Read the session once and keep listening for changes.

        Purpose:
            Make latest and command usable from the overlay thread.

        Parameters:
            None.

        Returns:
            None.

        Precondition:
            The WinRT media-control packages are installed for this Python.

        Postcondition:
            The background loop is running, or start raises the first error.
        """
        self._thread.start()
        if not self._ready.wait(10):
            raise TimeoutError("Windows media session did not respond.")
        if self._error is not None:
            raise self._error

    def latest(self) -> tuple[dict | None, int]:
        """Return the newest playback body and a counter that changes each read.

        Purpose:
            Let the overlay apply a new sample as soon as an event or the
            one-second read publishes one.

        Parameters:
            None.

        Returns:
            tuple[dict | None, int]: The currently-playing body, or None when
            Spotify has no session, and the sample counter.

        Precondition:
            start has been called.

        Postcondition:
            The session is unchanged.
        """
        with self._lock:
            return self._now, self._generation

    def command(self, action: str) -> str | None:
        """Send previous, next, pause, or resume to the Spotify session.

        Purpose:
            Drive the desktop app from the overlay without a Web API scope.

        Parameters:
            action (str): previous, next, pause, or resume.

        Returns:
            str | None: An overlay notice key when the command cannot run.
            None when Windows accepted it.

        Precondition:
            start has finished and the loop is running.

        Postcondition:
            A successful command asks Windows to change playback and then
            publishes a new sample.
        """
        loop = self._loop
        if loop is None:
            return "need_player"
        future = asyncio.run_coroutine_threadsafe(self._send(action), loop)
        try:
            return future.result(timeout=3)
        except Exception:
            return "playback_failed"

    def _run(self) -> None:
        """Own the asyncio loop that reads and listens."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._main())
        except Exception as exc:
            self._error = exc
            self._ready.set()

    async def _main(self) -> None:
        """Subscribe to session changes and read about once a second."""
        self._refresh_lock = asyncio.Lock()
        self._manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        wake = lambda *_args: self._schedule_refresh()
        self._manager.add_sessions_changed(wake)
        self._manager.add_current_session_changed(wake)
        await self._read()
        self._ready.set()
        while True:
            await asyncio.sleep(WINDOWS_POLL_SECONDS)
            await self._read()

    def _schedule_refresh(self, *_args) -> None:
        """Ask the loop to read again, collapsing a burst of events into one."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        def kick() -> None:
            self._dirty = True
            if self._refresh_scheduled:
                return
            self._refresh_scheduled = True
            asyncio.create_task(self._drain())

        loop.call_soon_threadsafe(kick)

    async def _drain(self) -> None:
        """Read until no event arrived during the read."""
        while True:
            self._dirty = False
            await self._read()
            if not self._dirty:
                self._refresh_scheduled = False
                return

    async def _read(self) -> None:
        """Publish Spotify's session, or None when it is not open."""
        if self._refresh_lock is None:
            return
        async with self._refresh_lock:
            now = None
            session = None
            try:
                session = _spotify_session(self._manager)
                self._watch(session)
                if session is not None:
                    now = await _currently_playing(session)
            except Exception:
                now = None
                session = None
                self._watch(None)
            self._publish(now)

    def _watch(self, session) -> None:
        """Listen to one Spotify session until a different session appears."""
        if session is self._watched:
            return
        previous = self._watched
        tokens = self._tokens
        self._watched = session
        self._tokens = []
        if previous is not None:
            for remove, token in tokens:
                try:
                    remove(token)
                except Exception:
                    pass
        if session is None:
            return
        handler = lambda *_args: self._schedule_refresh()
        self._tokens = [
            (session.remove_media_properties_changed, session.add_media_properties_changed(handler)),
            (session.remove_playback_info_changed, session.add_playback_info_changed(handler)),
            (session.remove_timeline_properties_changed, session.add_timeline_properties_changed(handler)),
        ]

    def _publish(self, now: dict | None) -> None:
        """Store one sample for the overlay thread."""
        with self._lock:
            self._now = now
            self._generation += 1

    async def _send(self, action: str) -> str | None:
        """Run one transport command on the session thread."""
        await self._read()
        session = self._watched
        if session is None:
            return "need_player"
        try:
            if action == "previous":
                ok = await session.try_skip_previous_async()
            elif action == "next":
                ok = await session.try_skip_next_async()
            elif action == "pause":
                ok = await session.try_pause_async()
            elif action == "resume":
                ok = await session.try_play_async()
            else:
                return "playback_failed"
        except Exception:
            return "playback_failed"
        if ok is False:
            return "playback_failed"
        await self._read()
        return None


def _spotify_session(manager):
    """Return the Spotify desktop session, preferring one that is playing."""
    Status = GlobalSystemMediaTransportControlsSessionPlaybackStatus
    if manager is None:
        return None
    found = []
    for session in manager.get_sessions():
        app_id = session.source_app_user_model_id or ""
        if str(app_id).startswith("Spotify"):
            found.append(session)
    for session in found:
        if session.get_playback_info().playback_status == Status.PLAYING:
            return session
    if found:
        return found[0]
    return None


async def _currently_playing(session) -> dict | None:
    """Copy the session into a currently-playing body, or None when nothing is playing."""
    Status = GlobalSystemMediaTransportControlsSessionPlaybackStatus
    info = session.get_playback_info()
    status = info.playback_status
    if status in (Status.CLOSED, Status.STOPPED):
        return None
    props = await session.try_get_media_properties_async()
    if props is None:
        return None
    title = props.title or ""
    artist = props.artist or ""
    album = props.album_title or ""
    if not title or not artist:
        return None
    timeline = session.get_timeline_properties()
    duration_ms = _span_ms(timeline.end_time - timeline.start_time)
    if duration_ms <= 0:
        return None
    playing = status == Status.PLAYING
    return {
        "is_playing": playing,
        "progress_ms": _progress_ms(timeline, playing, duration_ms),
        "item": {
            "type": "track",
            "id": media_choice_key(title, artist, album),
            "name": title,
            "duration_ms": duration_ms,
            "artists": [{"name": artist}],
            "album": {"name": album},
        },
    }


def _progress_ms(timeline, playing: bool, duration_ms: int) -> int:
    """Return the playback position, moving it forward only while audio is playing."""
    position_ms = _span_ms(timeline.position)
    if playing:
        updated = timeline.last_updated_time
        if isinstance(updated, datetime):
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            elapsed_ms = int((datetime.now(timezone.utc) - updated).total_seconds() * 1000)
            if elapsed_ms > 0:
                position_ms += elapsed_ms
    if position_ms < 0:
        return 0
    if position_ms > duration_ms:
        return duration_ms
    return position_ms


def _span_ms(span) -> int:
    """Return a timedelta length in milliseconds."""
    return int(round(span.total_seconds() * 1000))
