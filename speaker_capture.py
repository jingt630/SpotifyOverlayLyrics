"""Copy the audio the default speakers are already playing.

Windows calls this a WASAPI loopback. Spotify keeps playing to the speakers.
This process reads a duplicate of that mix. It is how the overlay hears the
song: the samples that just went to the sound device, not a downloaded file.

Nothing here writes a permanent audio file.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

ChunkListener = Callable[[bytes, int, int, float], None]


class SpeakerCapture:
    """Read the default speaker loopback on a background thread."""

    def __init__(self) -> None:
        """Create a capture that has not opened the sound device yet.

        Purpose:
            Hold listeners and the recent audio buffer.

        Parameters:
            None.

        Returns:
            None.

        Precondition:
            None.

        Postcondition:
            No audio device is open.
        """
        self._lock = threading.Lock()
        self._listeners: list[ChunkListener] = []
        self._chunks: deque[tuple[float, bytes]] = deque()
        self._rate = 0
        self._channels = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.error: str | None = None
        self._keep_seconds = 45.0

    def add_listener(self, listener: ChunkListener) -> None:
        """Call listener(pcm, sample_rate, channels, monotonic_time) for each buffer."""
        with self._lock:
            self._listeners.append(listener)

    def start(self) -> bool:
        """Open the speaker loopback. Return False when the device cannot be opened."""
        if self._thread is not None and self._thread.is_alive():
            return self.error is None
        self._stop.clear()
        self.error = None
        self._thread = threading.Thread(target=self._run, name="speaker-loopback", daemon=True)
        self._thread.start()
        for _ in range(50):
            if self.error is not None or self._rate:
                break
            time.sleep(0.02)
        return self.error is None and self._rate > 0

    def stop(self) -> None:
        """Close the loopback thread."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def slice(self, start: float, end: float) -> tuple[bytes, int, int] | None:
        """Return PCM captured between two monotonic times.

        Purpose:
            Cut the audio that belonged to one lyric line.

        Parameters:
            start (float): Monotonic time when the line became current.
            end (float): Monotonic time when the next line began.

        Returns:
            tuple[bytes, int, int] | None: 16-bit PCM, sample rate, and channel
            count. None when that span has no audio.

        Precondition:
            end is greater than start.

        Postcondition:
            The stored buffer is unchanged.
        """
        with self._lock:
            rate = self._rate
            channels = self._channels
            pieces = [pcm for stamp, pcm in self._chunks if start <= stamp <= end]
        if not pieces or rate <= 0:
            return None
        return b"".join(pieces), rate, channels

    def _run(self) -> None:
        """Read loopback buffers until stop is set."""
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            self.error = "pyaudiowpatch is not installed"
            return
        audio = pyaudio.PyAudio()
        stream = None
        try:
            device = _default_loopback(audio, pyaudio)
            if device is None:
                self.error = "No speaker loopback device was found"
                return
            rate = int(device["defaultSampleRate"])
            channels = int(device["maxInputChannels"])
            self._rate = rate
            self._channels = channels
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                frames_per_buffer=1024,
                input_device_index=int(device["index"]),
            )
            while not self._stop.is_set():
                pcm = stream.read(1024, exception_on_overflow=False)
                stamp = time.monotonic()
                with self._lock:
                    self._chunks.append((stamp, pcm))
                    listeners = list(self._listeners)
                    cutoff = stamp - self._keep_seconds
                    while self._chunks and self._chunks[0][0] < cutoff:
                        self._chunks.popleft()
                for listener in listeners:
                    listener(pcm, rate, channels, stamp)
        except Exception as exc:
            self.error = str(exc)
        finally:
            if stream is not None:
                stream.stop_stream()
                stream.close()
            audio.terminate()


def _default_loopback(audio, pyaudio) -> dict | None:
    """Return the loopback input that mirrors the default speakers."""
    try:
        wasapi = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError:
        return None
    speakers = audio.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if speakers.get("isLoopbackDevice"):
        return speakers
    for loopback in audio.get_loopback_device_info_generator():
        if speakers["name"] in loopback["name"]:
            return loopback
    return None
