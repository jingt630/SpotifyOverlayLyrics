"""Public lyric catalog. The MongoDB password stays on this host.

The desktop app calls this server over the internet. It never receives the
database URI. Reads are limited to 30 per minute per address. Writes are
limited to 1 per minute per address. A write can only upsert one song.
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from lyric_catalog import _doc_rank, _name_filter, _stored_fields, song_key_of

_HOST = "0.0.0.0"
_PORT = int(os.environ.get("PORT", "8787"))
_MAX_BODY = 1_000_000
_READS_PER_MINUTE = 30
_WRITE_INTERVAL_SECONDS = 60
_reads: dict[str, list[float]] = {}
_writes: dict[str, float] = {}
_collection = None


def main() -> None:
    """Listen for catalog reads and writes."""
    collection = _songs()
    print(f"Lyric catalog listening on http://{_HOST}:{_PORT}", flush=True)
    print(f"MongoDB connected. Database lyrics, collection {collection.name}.", flush=True)
    server = ThreadingHTTPServer((_HOST, _PORT), Handler)
    server.serve_forever()


class Handler(BaseHTTPRequestHandler):
    """One catalog request."""

    def do_GET(self) -> None:  # noqa: N802 — stdlib handler name
        """Return one song, or confirm the server is up."""
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"ok": True})
            return
        if path != "/songs":
            self._send(404, {"error": "not found"})
            return
        if not _allow_read(_client_address(self)):
            self._send(429, {"error": "too many reads"})
            return
        query = {key: values[0] for key, values in parse_qs(urlparse(self.path).query).items()}
        try:
            duration = int(query.get("duration_ms") or 0)
        except ValueError:
            self._send(400, {"error": "duration_ms must be an integer"})
            return
        song = {
            "track_name": query.get("track_name") or "",
            "artist_name": query.get("artist_name") or "",
            "album_name": query.get("album_name") or "",
            "duration_ms": duration,
        }
        if not song["track_name"] or not song["artist_name"]:
            self._send(400, {"error": "track_name and artist_name are required"})
            return
        found = _find(song)
        if found is None:
            self._send(404, {"error": "not found"})
            return
        self._send(200, found)

    def do_PUT(self) -> None:  # noqa: N802 — stdlib handler name
        """Upsert one song document."""
        if urlparse(self.path).path != "/songs":
            self._send(404, {"error": "not found"})
            return
        address = _client_address(self)
        if not _allow_write(address):
            self._send(429, {"error": "one write per minute"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_BODY:
            self._send(413, {"error": "lyric body is empty or larger than 1 MB"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "body must be JSON"})
            return
        document = _valid_song(payload)
        if document is None:
            self._send(400, {"error": "song document was rejected"})
            return
        try:
            saved, created = _upsert(document)
        except Exception as exc:
            _writes.pop(address, None)
            print(f"MongoDB rejected {document.get('track_name') or 'a song'}: {exc}", flush=True)
            self._send(500, {"error": "database rejected the song"})
            return
        if not saved:
            self._send(200, {"ok": True, "replaced": False})
            return
        if created:
            title = document.get("track_name") or "Unknown title"
            artist = document.get("artist_name") or "Unknown artist"
            label = {1: "word sync", 0: "line sync", -1: "no synced lyric"}.get(int(document["sync"]), "lyric")
            print(f"MongoDB saved {title} — {artist} ({label}).", flush=True)
        self._send(200, {"ok": True, "created": created})

    def log_message(self, fmt: str, *args) -> None:
        """Keep the process quiet except for catalog lines printed above."""
        return

    def _send(self, status: int, payload: dict) -> None:
        """Write one JSON response."""
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _songs():
    """Open the songs collection. The URI is read on this machine only."""
    global _collection
    if _collection is not None:
        return _collection
    from pymongo import MongoClient

    from lyric_catalog import _uri

    uri = _uri()
    if not uri:
        raise SystemExit("MongoDB URI is missing. Put it in atlas-credentials.env.")
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    client.admin.command("ping")
    collection = client["lyrics"]["songs"]
    collection.create_index("key", unique=True)
    _ensure_validator(collection)
    _collection = collection
    return collection


def _find(query: dict) -> dict | None:
    """Return one song and drop extra copies that differ only by duration."""
    collection = _songs()
    key = song_key_of(query)
    names = _name_filter(query)
    docs = list(collection.find({"$or": [{"key": key}, names]}))
    if not docs:
        return None
    duration = int(query.get("duration_ms") or 0)
    docs.sort(key=lambda doc: _doc_rank(doc, duration))
    keep = docs[0]
    extras = [doc["_id"] for doc in docs[1:]]
    payload = _stored_fields(keep, key)
    if keep.get("key") != key:
        collection.replace_one({"_id": keep["_id"]}, payload)
    if extras:
        collection.delete_many({"_id": {"$in": extras}})
    return payload


def _upsert(document: dict) -> tuple[bool, bool]:
    """Insert or replace one song. A word-sync document is not replaced by less."""
    collection = _songs()
    names = _name_filter(document)
    existing = list(collection.find(names, {"sync": 1, "key": 1}))
    if any(int(doc.get("sync") or -1) == 1 for doc in existing) and document["sync"] != 1:
        return False, False
    current = next((doc for doc in existing if doc.get("key") == document["key"]), None)
    collection.replace_one({"key": document["key"]}, document, upsert=True)
    collection.delete_many({**names, "key": {"$ne": document["key"]}})
    return True, current is None


def _valid_song(payload: object) -> dict | None:
    """Return a stored document, or None when the body is not one song."""
    if not isinstance(payload, dict):
        return None
    track = payload.get("track_name")
    artist = payload.get("artist_name")
    album = payload.get("album_name")
    if not isinstance(track, str) or not isinstance(artist, str) or not isinstance(album, str):
        return None
    if not track.strip() or not artist.strip() or len(track) > 500 or len(artist) > 500 or len(album) > 500:
        return None
    try:
        duration = int(payload.get("duration_ms"))
        sync = int(payload.get("sync"))
    except (TypeError, ValueError):
        return None
    if duration < 0 or sync not in (-1, 0, 1):
        return None
    lines = payload.get("lines")
    if not isinstance(lines, list) or len(lines) > 8000 or not _valid_lines(lines):
        return None
    query = {"track_name": track, "artist_name": artist, "album_name": album, "duration_ms": duration}
    return _stored_fields({**query, "sync": sync, "lines": lines}, song_key_of(query))


def _ensure_validator(collection) -> None:
    """Replace the songs validator with one the lyric documents can pass.

    Purpose:
        A validator stored without $jsonSchema compares the song to the
        schema text and rejects every save. This installs a schema that
        accepts the catalog fields, including 32-bit and 64-bit timestamps.

    Parameters:
        collection: The open songs collection.

    Returns:
        None.
    """
    number = {"bsonType": ["int", "long", "double"]}
    text = {"bsonType": "string"}
    word = {
        "bsonType": "object",
        "required": ["text"],
        "properties": {
            "text": text,
            "text_s": text,
            "text_t": text,
            "start_ms": number,
            "duration_ms": number,
        },
    }
    line = {
        "bsonType": "object",
        "required": ["start_ms", "text"],
        "properties": {
            "start_ms": number,
            "duration_ms": number,
            "text": text,
            "text_s": text,
            "text_t": text,
            "words": {"bsonType": "array", "items": word},
        },
    }
    schema = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["key", "track_name", "artist_name", "album_name", "duration_ms", "sync", "lines"],
            "properties": {
                "_id": {"bsonType": "objectId"},
                "key": text,
                "track_name": text,
                "artist_name": text,
                "album_name": text,
                "duration_ms": number,
                "sync": number,
                "lines": {"bsonType": "array", "items": line},
            },
        }
    }
    try:
        collection.database.command(
            "collMod",
            collection.name,
            validator=schema,
            validationLevel="moderate",
            validationAction="error",
        )
    except Exception as exc:
        print(f"Could not update the songs validator: {exc}", flush=True)


def _valid_lines(lines: list) -> bool:
    """Return whether every line and word has the fields the catalog stores."""
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("text"), str):
            return False
        if not _optional_text(line):
            return False
        words = line.get("words")
        if words is None:
            continue
        if not isinstance(words, list):
            return False
        for word in words:
            if not isinstance(word, dict) or not isinstance(word.get("text"), str) or not _optional_text(word):
                return False
    return True


def _optional_text(node: dict) -> bool:
    """Return whether text_s and text_t are absent or strings."""
    for field in ("text_s", "text_t"):
        if field in node and not isinstance(node[field], str):
            return False
    return True


def _client_address(handler: BaseHTTPRequestHandler) -> str:
    """Return the caller address. A host proxy puts the real address first."""
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or handler.client_address[0]
    return handler.client_address[0]


def _allow_read(address: str) -> bool:
    """Allow 30 reads per minute from one address."""
    now = time.monotonic()
    recent = [stamp for stamp in _reads.get(address, []) if now - stamp < 60]
    if len(recent) >= _READS_PER_MINUTE:
        _reads[address] = recent
        return False
    recent.append(now)
    _reads[address] = recent
    return True


def _allow_write(address: str) -> bool:
    """Allow one write per minute from one address."""
    now = time.monotonic()
    previous = _writes.get(address, 0.0)
    if now - previous < _WRITE_INTERVAL_SECONDS:
        return False
    _writes[address] = now
    return True


if __name__ == "__main__":
    main()
