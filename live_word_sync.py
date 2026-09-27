"""Build word times from speaker audio and the current LRCLIB line.

The overlay does not draw these times. When a lyric line ends, the audio
captured during that line is split on voiced sound, and the line's words are
laid across those sounds. The result is appended to a JSON file.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from lrclib_client import spotify_track_query
from speaker_capture import SpeakerCapture

LIVE_WORD_PATH = Path(__file__).resolve().parent / ".spotify" / "live_word_lyrics.json"


class LiveWordSync:
    """Capture the speakers and store word times for each finished lyric line."""

    def __init__(self, path: Path = LIVE_WORD_PATH) -> None:
        """Prepare an idle capture and load songs already stored.

        Purpose:
            Keep word times across overlay restarts.

        Parameters:
            path (Path): JSON file. Defaults to LIVE_WORD_PATH.

        Returns:
            None.

        Precondition:
            If path exists, it is a JSON object.

        Postcondition:
            The speaker device is not open yet.
        """
        self.path = path
        self._capture = SpeakerCapture()
        self._lock = threading.Lock()
        self._started = False
        self._open_key: tuple[str, int] | None = None
        self._open_at = 0.0
        self._open_text = ""
        self._open_song_ms = 0
        self._song: dict | None = None
        self._saved: dict[str, dict] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            if isinstance(raw, dict):
                self._saved = raw

    def note(self, now: dict | None, lyrics, progress_ms: int, playing: bool) -> None:
        """Watch the current line and store word times when it ends.

        Purpose:
            Run beside the overlay clock without changing what is drawn.

        Parameters:
            now (dict | None): Currently-playing body, or None.
            lyrics: LyricLookup that can return a lyric window.
            progress_ms (int): Playback position in milliseconds.
            playing (bool): True while audio is moving.

        Returns:
            None.

        Precondition:
            progress_ms is >= 0.

        Postcondition:
            A finished line may have been queued for alignment. The overlay
            text is unchanged.
        """
        try:
            self._note(now, lyrics, progress_ms, playing)
        except Exception:
            return

    def _note(self, now: dict | None, lyrics, progress_ms: int, playing: bool) -> None:
        """Start capture when playback is running, and close a line when it changes."""
        query = spotify_track_query(now)
        if query is None or not playing:
            self._close_line()
            return
        if not self._started:
            self._started = True
            if not self._capture.start():
                return
        window = lyrics.window_at(now, progress_ms)
        if window is None or not window.singing.text.strip():
            self._close_line()
            return
        key = _song_key(query)
        line_key = (key, window.singing.time_ms)
        if line_key == self._open_key:
            return
        self._close_line()
        self._open_key = line_key
        self._open_at = time.monotonic()
        self._open_text = window.singing.text
        self._open_song_ms = window.singing.time_ms
        self._song = query

    def _close_line(self) -> None:
        """Align the open line against the audio captured while it was current."""
        if self._open_key is None or self._song is None:
            self._open_key = None
            return
        song_key, line_start = self._open_key
        text = self._open_text
        started = self._open_at
        song = self._song
        self._open_key = None
        self._song = None
        audio = self._capture.slice(started, time.monotonic())
        if audio is None:
            return
        threading.Thread(
            target=self._store,
            args=(song_key, song, line_start, text, audio),
            name="live-word-line",
            daemon=True,
        ).start()

    def _store(self, song_key: str, song: dict, line_start: int, text: str, audio: tuple[bytes, int, int]) -> None:
        """Write one aligned line into the JSON file."""
        pcm, rate, channels = audio
        words = align_words(pcm, rate, channels, text, line_start)
        if not words:
            return
        with self._lock:
            record = self._saved.get(song_key)
            if not isinstance(record, dict):
                record = {
                    "track_name": song["track_name"],
                    "artist_name": song["artist_name"],
                    "album_name": song["album_name"],
                    "duration_ms": song["duration_ms"],
                    "lines": [],
                }
            lines = [line for line in record.get("lines", []) if line.get("start_ms") != line_start]
            lines.append({"start_ms": line_start, "text": text, "words": words})
            lines.sort(key=lambda line: int(line.get("start_ms") or 0))
            record["lines"] = lines
            self._saved[song_key] = record
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self._saved, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)


def align_words(pcm: bytes, sample_rate: int, channels: int, text: str, line_start_ms: int) -> list[dict]:
    """Lay the line's tokens across the voiced parts of one PCM slice.

    Purpose:
        Give each word a start and a length inside this lyric line.

    Parameters:
        pcm (bytes): 16-bit little-endian PCM from the speaker loopback.
        sample_rate (int): Frames per second.
        channels (int): Channel count in pcm.
        text (str): The LRCLIB line.
        line_start_ms (int): Song time when this line starts.

    Returns:
        list[dict]: Words with text, start_ms, and duration_ms. Empty when
        the slice is quiet or has no tokens.

    Precondition:
        sample_rate is positive. channels is >= 1.

    Postcondition:
        pcm and text are unchanged.
    """
    tokens = _tokens(text)
    samples = _mono_samples(pcm, channels)
    if not tokens or sample_rate <= 0 or len(samples) < sample_rate // 5:
        return []
    frame = max(1, int(sample_rate * 0.02))
    loudness: list[float] = []
    for index in range(0, len(samples) - frame + 1, frame):
        block = samples[index : index + frame]
        energy = sum(sample * sample for sample in block) / frame
        loudness.append(energy ** 0.5)
    if not loudness:
        return []
    floor = sorted(loudness)[len(loudness) // 5]
    limit = max(floor * 2.5, 180.0)
    voiced_frames = [index for index, level in enumerate(loudness) if level >= limit]
    if len(voiced_frames) < 2:
        return []
    weights = [max(1, len(token)) for token in tokens]
    total = sum(weights)
    words = []
    cursor = 0
    for token, weight in zip(tokens, weights):
        start_index = min(len(voiced_frames) - 1, int(cursor * len(voiced_frames) / total))
        cursor += weight
        end_index = min(len(voiced_frames), max(start_index + 1, int(cursor * len(voiced_frames) / total)))
        start_frame = voiced_frames[start_index]
        end_frame = voiced_frames[end_index - 1] + 1
        start_ms = line_start_ms + int(start_frame * frame / sample_rate * 1000)
        duration_ms = max(1, int((end_frame - start_frame) * frame / sample_rate * 1000))
        words.append({"text": token, "start_ms": start_ms, "duration_ms": duration_ms})
    return words


def _song_key(query: dict) -> str:
    """Return a stable key from title, artist, and album."""
    return f"{query['track_name']}\u001f{query['artist_name']}\u001f{query['album_name']}"


def _tokens(text: str) -> list[str]:
    """Split a lyric line into Chinese characters or whitespace-separated words."""
    tokens: list[str] = []
    latin: list[str] = []

    def flush() -> None:
        if latin:
            tokens.append("".join(latin))
            latin.clear()

    for char in text.strip():
        if char.isspace():
            flush()
            continue
        if "\u4e00" <= char <= "\u9fff" or "\u3040" <= char <= "\u30ff":
            flush()
            tokens.append(char)
            continue
        latin.append(char)
    flush()
    return tokens


def _mono_samples(pcm: bytes, channels: int) -> list[int]:
    """Average 16-bit channels into one sample list."""
    count = len(pcm) // 2
    if count == 0 or channels <= 0:
        return []
    values = list(memoryview(pcm).cast("h"))
    frames = count // channels
    if channels == 1:
        return values[:frames]
    mixed = []
    for frame_index in range(frames):
        base = frame_index * channels
        mixed.append(sum(values[base : base + channels]) // channels)
    return mixed
