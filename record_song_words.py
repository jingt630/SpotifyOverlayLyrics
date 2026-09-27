"""Record one song from the speakers, then keep only WhisperX word times.

Play a single song in Spotify and do not skip. Press Enter to start, and
press Enter again to stop. Demucs splits the vocals out of a temporary
recording. WhisperX timestamps the words. The recording and the vocal file
are deleted. The JSON under .spotify/whisperx_words is what remains.

This listens to the default speaker loopback, the mix the speakers are
playing. It also notes the Windows media session so the file can name the song.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

WORD_DIR = Path(__file__).resolve().parent / ".spotify" / "whisperx_words"


def main() -> None:
    """Ask when to record, follow the media session, then write word JSON.

    Purpose:
        Turn one play-through into human-readable word times without keeping
        the audio.

    Parameters:
        None.

    Returns:
        None.

    Precondition:
        The terminal can read a line. Spotify is the audio on the speakers.

    Postcondition:
        A JSON file is written when WhisperX finishes. Temporary audio is removed.
    """
    _ensure_ffmpeg()
    missing = _missing_modules()
    if missing:
        print("Install the recording tools, then run this again:")
        print(f"  py -3.13 -m pip install {' '.join(missing)}")
        print("The first run also downloads the Demucs and WhisperX models.")
        raise SystemExit(1)
    print("Record one song from the speakers.")
    print("Do not skip, seek, or change songs while this is recording.")
    print("WhisperX and Demucs run after you stop, and the audio is deleted.")
    input("Press Enter to start recording. ")
    capture_error = _record_and_process()
    if capture_error:
        print(capture_error)
        raise SystemExit(1)


def _record_and_process() -> str | None:
    """Record until Enter, then replace the audio with a word-time JSON file."""
    from speaker_capture import SpeakerCapture

    holder: dict = {"wave": None, "tracks": [], "lock": threading.Lock(), "stop": False}
    folder = Path(tempfile.mkdtemp(prefix="lyric-record-"))
    wav_path = folder / "speakers.wav"
    capture = SpeakerCapture()

    def on_chunk(pcm: bytes, rate: int, channels: int, _stamp: float) -> None:
        with holder["lock"]:
            handle = holder["wave"]
            if handle is None:
                handle = wave.open(str(wav_path), "wb")
                handle.setnchannels(channels)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                holder["wave"] = handle
            handle.writeframes(pcm)

    capture.add_listener(on_chunk)
    if not capture.start():
        shutil.rmtree(folder, ignore_errors=True)
        return f"Could not open the speaker loopback: {capture.error}"
    watcher = threading.Thread(target=_watch_session, args=(holder,), name="record-session", daemon=True)
    watcher.start()
    print("Recording. Leave this song playing.")
    try:
        input("Press Enter to stop recording. ")
    finally:
        holder["stop"] = True
        capture.stop()
        watcher.join(timeout=2)
        with holder["lock"]:
            handle = holder["wave"]
            if handle is not None:
                handle.close()
                holder["wave"] = None
            tracks = list(holder["tracks"])
    if not wav_path.exists() or wav_path.stat().st_size < 1000:
        shutil.rmtree(folder, ignore_errors=True)
        return "Nothing was recorded from the speakers."
    warning = None
    keys = {(item["track_name"], item["artist_name"]) for item in tracks}
    if len(keys) > 1:
        warning = "More than one song was playing. Word times may cover a mix of songs."
        print(warning)
    song = tracks[0] if tracks else {
        "track_name": "Unknown track",
        "artist_name": "",
        "album_name": "",
        "duration_ms": 0,
    }
    print(f"Processing {song['track_name']} — {song['artist_name']}. This can take several minutes.")
    try:
        words = _whisperx_words(wav_path, folder)
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return f"Word timing failed: {exc}"
    shutil.rmtree(folder, ignore_errors=True)
    WORD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = _safe_name(song["track_name"]) or "song"
    path = WORD_DIR / f"{safe}_{stamp}.json"
    payload = {
        "track_name": song["track_name"],
        "artist_name": song["artist_name"],
        "album_name": song["album_name"],
        "duration_ms": song["duration_ms"],
        "warning": warning,
        "tracks_heard": tracks,
        "whisperx": words,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved word times to {path}")
    print("The recording was deleted.")
    return None


def _watch_session(holder: dict) -> None:
    """Remember each Spotify song the Windows media session reports."""
    last = None
    while not holder["stop"]:
        track = _spotify_track()
        if track is not None:
            identity = (track["track_name"], track["artist_name"], track["album_name"])
            if identity != last:
                last = identity
                track["heard_at"] = datetime.now().isoformat(timespec="seconds")
                with holder["lock"]:
                    holder["tracks"].append(track)
        time.sleep(1)


def _spotify_track() -> dict | None:
    """Read title, artist, album, and duration from the Spotify media session."""
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
    except ImportError:
        return None

    async def once() -> dict | None:
        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        chosen = None
        for session in manager.get_sessions():
            app_id = str(session.source_app_user_model_id or "")
            if app_id.startswith("Spotify"):
                chosen = session
                break
        if chosen is None:
            return None
        props = await chosen.try_get_media_properties_async()
        if props is None or not (props.title or props.artist):
            return None
        timeline = chosen.get_timeline_properties()
        duration_ms = int(round((timeline.end_time - timeline.start_time).total_seconds() * 1000))
        return {
            "track_name": props.title or "",
            "artist_name": props.artist or "",
            "album_name": props.album_title or "",
            "duration_ms": duration_ms,
        }

    try:
        return asyncio.run(once())
    except Exception:
        return None


def _ensure_ffmpeg() -> None:
    """Put the installed ffmpeg folder on PATH for this process.

    Purpose:
        WhisperX starts ffmpeg by name. A terminal opened before the install
        still has the old PATH, so reload the saved user PATH and the WinGet folder.

    Parameters:
        None.

    Returns:
        None.

    Precondition:
        None.

    Postcondition:
        shutil.which("ffmpeg") finds ffmpeg.exe, or the program exits before recording.
    """
    parts = [os.environ.get("PATH", "")]
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            user_path, _kind = winreg.QueryValueEx(key, "Path")
        parts.insert(0, str(user_path))
    except OSError:
        pass
    os.environ["PATH"] = os.pathsep.join(part for part in parts if part)
    if shutil.which("ffmpeg"):
        return
    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    found = next(packages.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"), None)
    if found is not None:
        os.environ["PATH"] = str(found.parent) + os.pathsep + os.environ["PATH"]
    if shutil.which("ffmpeg") is None:
        print("ffmpeg is still not visible to this program. Open a new terminal and run this again.")
        raise SystemExit(1)


def _whisperx_words(wav_path: Path, folder: Path) -> dict:
    """Separate vocals and return WhisperX segments with word times."""
    vocal_dir = folder / "vocals"
    subprocess.run(
        [sys.executable, "-m", "demucs", "--two-stems", "vocals", "-o", str(vocal_dir), str(wav_path)],
        check=True,
    )
    vocals = next(vocal_dir.rglob("vocals.wav"))
    import torch
    import whisperx

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute = "float16" if device == "cuda" else "int8"
    audio = whisperx.load_audio(str(vocals))
    model = whisperx.load_model("base", device, compute_type=compute)
    transcribed = model.transcribe(audio, batch_size=4)
    language = transcribed.get("language") or "en"
    align_model, metadata = whisperx.load_align_model(language_code=language, device=device)
    aligned = whisperx.align(transcribed["segments"], align_model, metadata, audio, device)
    return {"language": language, "segments": _plain(aligned.get("segments") or [])}


def _plain(value):
    """Convert WhisperX numbers and nested lists into JSON values."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, float):
        return round(value, 3)
    if hasattr(value, "item"):
        return _plain(value.item())
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _missing_modules() -> list[str]:
    """Return install names for tools this recording run needs."""
    needed = {
        "pyaudiowpatch": "pyaudiowpatch",
        "demucs": "demucs",
        "whisperx": "whisperx",
        "torch": "torch",
    }
    missing = []
    for module, package in needed.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


def _safe_name(name: str) -> str:
    """Return a short file name made from the song title."""
    cleaned = "".join(char if char.isalnum() else "_" for char in name).strip("_")
    return cleaned[:40]


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(1)
