"""Local lyric catalog, with an optional MongoDB copy for songs this machine has not seen.

Playback reads the local file. MongoDB is asked once, only when that file is
missing. A user-picked version is stored locally and is not uploaded.
"""

from __future__ import annotations

import csv
import hashlib
import json
import threading
import time
import unicodedata
from pathlib import Path

_ROOT = Path(__file__).resolve().parent / ".localLyric"
_UI_PATH = Path(__file__).resolve().parent / ".spotify" / "overlay_ui.json"
_INDEX_PATH = _ROOT / "index.csv"
_INDEX_FIELDS = ("track_name", "artist_name", "album_name", "duration_ms", "sync", "preference", "file")
_URI_PATH = Path(__file__).resolve().parent / ".spotify" / "mongodb_uri.txt"
_ENV_PATH = Path(__file__).resolve().parent / "atlas-credentials.env"
_LOCK = threading.Lock()
_mongo = None
_mongo_disabled_until = 0.0
_mongo_reported = False
_HOLD: dict[str, list[dict]] = {}
_announced: set[tuple[str, str, str]] = set()


def song_key(track_name: str, artist_name: str, album_name: str, duration_ms: int) -> str:
    """Return the catalog key for one song.

    Purpose:
        The same recording should hit one document even when the source
        duration differs by a few milliseconds.

    Parameters:
        track_name (str): Song title.
        artist_name (str): Artist names.
        album_name (str): Album title. Empty is part of the key.
        duration_ms (int): Length in milliseconds.

    Returns:
        str: Normalized title, artist, and album. Duration is stored on the
        document, but it is not part of the key. Windows can report two
        lengths for one play.

    Precondition:
        duration_ms is an integer.

    Postcondition:
        Equal names produce an equal key.
    """
    return "\u001f".join((_norm(track_name), _norm(artist_name), _norm(album_name)))


def local_get(query: dict) -> dict | None:
    """Return the catalog file for this song, or None when this machine has no record."""
    path = _path(query)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def get(query: dict) -> dict | None:
    """Return the local document, or copy one MongoDB document locally.

    Purpose:
        A later play of the same song reads the file and does not call MongoDB.
        A missing URI, a missing package, or a down cluster returns None so
        the caller can search NetEase and LRCLIB.

    Parameters:
        query (dict): track_name, artist_name, album_name, and duration_ms.

    Returns:
        dict | None: The catalog document. None when neither store has this song.

    Precondition:
        query contains the four song fields.

    Postcondition:
        A MongoDB hit is copied into .localLyric only when local storage is on.
        A miss writes nothing.
    """
    local = local_get(query)
    if local is not None:
        return local
    remote = _mongo_find(query)
    if remote is None:
        return None
    remote["preference"] = False
    remote["source_hit"] = "mongo"
    if store_locally():
        with _LOCK:
            _write(remote)
    return remote


def put_shared(query: dict, sync: int, lines: list[dict]) -> None:
    """Save an automatic lyric and upload it when this song is not a personal pick.

    Purpose:
        Word sync, line sync, and a confirmed miss are shared. A file marked
        preference stays on this machine. A word-sync document is not replaced
        by line sync or by a miss.

    Parameters:
        query (dict): Song names and duration.
        sync (int): 1 word, 0 line, -1 none.
        lines (list[dict]): Lyric lines. Empty when sync is -1.

    Returns:
        None.

    Precondition:
        sync is 1, 0, or -1.

    Postcondition:
        MongoDB is updated. A local file is written only when Save locally is on,
        and it does not replace a personal pick or a stored word sync.
    """
    document = _document(query, sync, lines, preference=False)
    if store_locally():
        with _LOCK:
            current = local_get(query)
            if current is not None and current.get("preference"):
                pass
            elif current is not None and int(current.get("sync") or -1) == 1 and sync != 1:
                pass
            else:
                _write(document)
    _mongo_upsert(document)


def put_preference(query: dict, sync: int, lines: list[dict]) -> None:
    """Save a version the user picked. The database is not updated."""
    document = _document(query, sync, lines, preference=True)
    with _LOCK:
        _write(document)


def announce(query: dict, place: str, kind: str) -> None:
    """Print where this song's lyric was found, once per place and kind.

    Purpose:
        Show local, MongoDB, NetEase, or LRCLIB, and whether the lyric is
        word sync, line sync, or missing. The same line is not printed again
        while this process is running.

    Parameters:
        query (dict): Song names and duration.
        place (str): local, MongoDB, NetEase, or LRCLIB.
        kind (str): The lyric that was found.

    Returns:
        None.

    Precondition:
        query contains the song name fields.

    Postcondition:
        One line is written to the terminal, or nothing when this pair was already printed.
    """
    token = (song_key_of(query), place, kind)
    with _LOCK:
        if token in _announced:
            return
        _announced.add(token)
    title = query.get("track_name") or "Unknown title"
    artist = query.get("artist_name") or "Unknown artist"
    print(f"{title} — {artist}: {place}, {kind}", flush=True)


def put_miss(query: dict) -> None:
    """Remember that NetEase and LRCLIB both had no synced lyric."""
    put_shared(query, -1, [])


def hold_lines(query: dict, lines: list[dict]) -> None:
    """Keep NetEase line lyrics in memory until LRCLIB finishes."""
    with _LOCK:
        _held()[song_key_of(query)] = lines


def held_lines(query: dict) -> list[dict] | None:
    """Return NetEase line lyrics waiting on LRCLIB, or None."""
    with _LOCK:
        lines = _held().get(song_key_of(query))
    if not lines:
        return None
    return lines


def drop_hold(query: dict) -> None:
    """Drop NetEase line lyrics that LRCLIB has replaced."""
    with _LOCK:
        _held().pop(song_key_of(query), None)


def sync_of(query: dict) -> int | None:
    """Return the local sync code, or None when this song has no local file."""
    document = local_get(query)
    if document is None or "sync" not in document:
        return None
    try:
        return int(document["sync"])
    except (TypeError, ValueError):
        return None


def store_locally() -> bool:
    """Return whether shared lyrics should also be written under .localLyric.

    Purpose:
        Personal picks are always stored locally. Shared lyrics stay in
        MongoDB unless this setting is on. The default is off.

    Parameters:
        None.

    Returns:
        bool: True when overlay settings ask for a local copy.

    Precondition:
        None.

    Postcondition:
        The settings file is unchanged.
    """
    if not _UI_PATH.exists():
        return False
    try:
        raw = json.loads(_UI_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return raw.get("store_local") is True


def song_key_of(query: dict) -> str:
    """Return the catalog key stored on a playback query."""
    return song_key(
        str(query.get("track_name") or ""),
        str(query.get("artist_name") or ""),
        str(query.get("album_name") or ""),
        int(query.get("duration_ms") or 0),
    )


def _norm(value: str) -> str:
    """Fold case and spacing so the same name hits one key."""
    text = unicodedata.normalize("NFKC", value or "")
    return " ".join(text.split()).casefold()


def _path(query: dict) -> Path:
    """Return the local JSON path for this song."""
    digest = hashlib.sha256(song_key_of(query).encode("utf-8")).hexdigest()
    return _ROOT / f"{digest}.json"


def _document(query: dict, sync: int, lines: list[dict], preference: bool) -> dict:
    """Build one catalog document."""
    return {
        "key": song_key_of(query),
        "track_name": query.get("track_name") or "",
        "artist_name": query.get("artist_name") or "",
        "album_name": query.get("album_name") or "",
        "duration_ms": int(query.get("duration_ms") or 0),
        "sync": sync,
        "preference": preference,
        "lines": lines,
    }


def _write(document: dict) -> None:
    """Write one catalog file and refresh the readable index.csv."""
    _ROOT.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(document["key"]).encode("utf-8")).hexdigest()
    path = _ROOT / f"{digest}.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    _refresh_index()


def _refresh_index() -> None:
    """Rebuild index.csv from every JSON file in the catalog folder."""
    rows: list[dict[str, str]] = []
    for path in sorted(_ROOT.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        rows.append(
            {
                "track_name": str(raw.get("track_name") or ""),
                "artist_name": str(raw.get("artist_name") or ""),
                "album_name": str(raw.get("album_name") or ""),
                "duration_ms": str(raw.get("duration_ms") or ""),
                "sync": str(raw.get("sync") if raw.get("sync") is not None else ""),
                "preference": "yes" if raw.get("preference") else "",
                "file": path.name,
            }
        )
    rows.sort(key=lambda row: (row["track_name"].casefold(), row["artist_name"].casefold()))
    with _INDEX_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _held() -> dict[str, list[dict]]:
    """Return the in-memory map of NetEase line lyrics still waiting."""
    return _HOLD


def _uri() -> str:
    """Return the MongoDB URI from the environment or from the local credential file.

    Purpose:
        The process environment wins. Otherwise MONGODB_URI is read from
        atlas-credentials.env, then from .spotify/mongodb_uri.txt.

    Parameters:
        None.

    Returns:
        str: The URI, or an empty string when none is configured.

    Precondition:
        None.

    Postcondition:
        The credential file is not rewritten.
    """
    import os

    env = os.environ.get("MONGODB_URI", "").strip()
    if env:
        return env
    from_file = _env_value(_ENV_PATH, "MONGODB_URI")
    if from_file:
        return from_file
    if not _URI_PATH.exists():
        return ""
    try:
        return _URI_PATH.read_text(encoding="utf-8").strip().strip('"').strip("'")
    except OSError:
        return ""


def _env_value(path: Path, name: str) -> str:
    """Return one KEY=VALUE from a local env file, without quotes."""
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    prefix = name + "="
    for line in lines:
        text = line.strip()
        if text.startswith(prefix):
            return text[len(prefix) :].strip().strip('"').strip("'")
    return ""


def _collection():
    """Return the songs collection, or None when MongoDB is not configured or not reachable."""
    global _mongo, _mongo_disabled_until
    if _mongo is False:
        return None
    if time.monotonic() < _mongo_disabled_until:
        return None
    if _mongo is not None:
        return _mongo
    uri = _uri()
    if not uri:
        _mongo = False
        _report("MongoDB is not configured. Lyrics stay on this computer.")
        return None
    try:
        from pymongo import MongoClient
    except ImportError:
        _mongo = False
        _report("MongoDB client pymongo is not installed. Lyrics stay on this computer.")
        return None
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=1500, connectTimeoutMS=1500)
        client.admin.command("ping")
        collection = client["lyrics"]["songs"]
        collection.create_index("key", unique=True)
    except Exception as exc:
        _mongo_disabled_until = time.monotonic() + 60
        _report(f"MongoDB did not connect ({exc.__class__.__name__}). Lyrics stay on this computer for now.")
        return None
    _mongo = collection
    _report("MongoDB connected. Database lyrics, collection songs.")
    return collection


def _mongo_find(query: dict) -> dict | None:
    """Return one song document, collapsing copies that differ only by duration.

    Purpose:
        A Windows session can report two lengths for one play. Those used to
        become two catalog keys. Title, artist, and album are one song.

    Parameters:
        query (dict): Song names and duration.

    Returns:
        dict | None: The kept document, without MongoDB's id. None when the
        song is absent or MongoDB cannot be reached.

    Precondition:
        query contains the song name fields.

    Postcondition:
        Extra documents for the same names are deleted. The kept document
        uses the duration-free key.
    """
    collection = _collection()
    if collection is None:
        return None
    key = song_key_of(query)
    names = _name_filter(query)
    try:
        docs = list(collection.find({"$or": [{"key": key}, names]}))
    except Exception:
        return None
    if not docs:
        return None
    duration = int(query.get("duration_ms") or 0)
    docs.sort(key=lambda doc: _doc_rank(doc, duration))
    keep = docs[0]
    extras = [doc["_id"] for doc in docs[1:]]
    payload = _stored_fields(keep, key)
    try:
        if keep.get("key") != key:
            collection.replace_one({"_id": keep["_id"]}, payload)
        if extras:
            collection.delete_many({"_id": {"$in": extras}})
    except Exception:
        pass
    return payload


def _mongo_upsert(document: dict) -> None:
    """Insert or replace the shared document. Word sync is left in place."""
    collection = _collection()
    if collection is None:
        return
    payload = _stored_fields(document, str(document["key"]))
    names = _name_filter(payload)
    try:
        existing = list(collection.find(names, {"sync": 1, "key": 1}))
        if any(int(doc.get("sync") or -1) == 1 for doc in existing) and payload["sync"] != 1:
            return
        current = next((doc for doc in existing if doc.get("key") == payload["key"]), None)
        collection.replace_one({"key": payload["key"]}, payload, upsert=True)
        collection.delete_many({**names, "key": {"$ne": payload["key"]}})
        if current is None:
            label = {1: "word sync", 0: "line sync", -1: "no synced lyric"}.get(int(payload["sync"]), "lyric")
            title = payload.get("track_name") or "Unknown title"
            artist = payload.get("artist_name") or "Unknown artist"
            _report(f"MongoDB saved {title} — {artist} ({label}).")
    except Exception as exc:
        _report(f"MongoDB did not save {payload.get('track_name') or 'this song'} ({exc.__class__.__name__}).")
        return


def _name_filter(query: dict) -> dict:
    """Return the MongoDB filter for one title, artist, and album."""
    return {
        "track_name": query.get("track_name") or "",
        "artist_name": query.get("artist_name") or "",
        "album_name": query.get("album_name") or "",
    }


def _stored_fields(document: dict, key: str) -> dict:
    """Return the fields written to MongoDB."""
    return {
        "key": key,
        "track_name": document.get("track_name") or "",
        "artist_name": document.get("artist_name") or "",
        "album_name": document.get("album_name") or "",
        "duration_ms": int(document.get("duration_ms") or 0),
        "sync": int(document.get("sync") if document.get("sync") is not None else -1),
        "lines": document.get("lines") if isinstance(document.get("lines"), list) else [],
    }


def _doc_rank(document: dict, duration_ms: int) -> tuple[int, int]:
    """Prefer word sync, then line sync, then the duration closest to playback."""
    try:
        sync = int(document.get("sync"))
    except (TypeError, ValueError):
        sync = -1
    try:
        stored = int(document.get("duration_ms") or 0)
    except (TypeError, ValueError):
        stored = 0
    return (-sync, abs(stored - duration_ms))


def _report(message: str) -> None:
    """Print one catalog line. Connection success and failure are each printed once."""
    global _mongo_reported
    if message.startswith("MongoDB connected") or message.startswith("MongoDB is not") or message.startswith("MongoDB client") or message.startswith("MongoDB did not connect"):
        if _mongo_reported:
            return
        _mongo_reported = True
    print(message, flush=True)
