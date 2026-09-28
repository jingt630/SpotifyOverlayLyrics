"""Look up NetEase word-level lyrics and save them under .netease.

A saved song id is reused on the next play. When word times exist, the
overlay highlights the word that matches the playback position.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from chinese_script import shown_text, stamp_lines
import lyric_catalog
from lrclib_client import (
    DURATION_CHOICE_THRESHOLD_MS,
    LyricCandidate,
    LyricLine,
    lyric_slots,
    media_choice_key,
    parse_synced_lyrics,
    _album_rank,
    _artist_rank,
    _title_rank,
    format_clock,
    normalize_name,
    spotify_track_query,
)

_ROOT = Path(__file__).resolve().parent
_OUT_DIR = _ROOT / ".netease"
_CHOICES_PATH = _OUT_DIR / "netease_choices.json"
_CATALOG_PATH = _OUT_DIR / "netease_catalog.csv"
_CATALOG_FIELDS = ("track_name", "artist_name", "album_name", "duration", "netease_id")
_ALIAS_PATH = _OUT_DIR / "name_aliases.json"
_ALIAS_CSV_PATH = _OUT_DIR / "name_aliases.csv"
_ALIAS_FIELDS = ("artist_name", "track_name", "chinese_artists", "chinese_titles")
_MB_ARTIST = "https://musicbrainz.org/ws/2/artist/"
_MB_RECORDING = "https://musicbrainz.org/ws/2/recording/"
_MB_AGENT = "SpotifyOverlayLyrics/1.0 (local lyrics overlay)"
_SEARCH_URL = "https://music.163.com/api/cloudsearch/pc"
_LYRIC_URL = "https://interface3.music.163.com/eapi/song/lyric/v1"
_EAPI_KEY = b"e82ckenh8dichen8"
_USER_AGENT = "Mozilla/5.0"
_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_WORD_RE = re.compile(r"\((\d+),(\d+),\d+\)([^()\n]*)")


def search_songs(track_name: str, artist_name: str) -> list[dict]:
    """Return NetEase songs for a title and artist.

    Purpose:
        Ask the public cloud search for candidates the same way a person
        would type the song and the singer.

    Parameters:
        track_name (str): Spotify track name.
        artist_name (str): Spotify artist names.

    Returns:
        list[dict]: id, name, artist, album, and duration_ms for each hit.

    Precondition:
        track_name is not empty.

    Postcondition:
        The returned list is a copy of the search hits. No file is written.
    """
    body = urllib.parse.urlencode(
        {"s": f"{track_name} {artist_name}".strip(), "type": 1, "limit": 20, "offset": 0}
    ).encode("utf-8")
    request = urllib.request.Request(
        _SEARCH_URL,
        data=body,
        headers={"User-Agent": _USER_AGENT, "Referer": "https://music.163.com/"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    songs = ((payload.get("result") or {}).get("songs")) or []
    rows: list[dict] = []
    for song in songs:
        artists = song.get("ar") or song.get("artists") or []
        album = song.get("al") or song.get("album") or {}
        duration = song.get("dt") if song.get("dt") is not None else song.get("duration")
        if not isinstance(song.get("id"), int) or not isinstance(duration, int):
            continue
        rows.append(
            {
                "id": song["id"],
                "name": str(song.get("name") or ""),
                "artist": ", ".join(str(artist.get("name") or "") for artist in artists if artist.get("name")),
                "album": str(album.get("name") or ""),
                "duration_ms": duration,
            }
        )
    return rows


def _has_han(text: str) -> bool:
    """Return True when the text contains a Chinese or Japanese kanji character."""
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _han_names(values: list[object]) -> list[str]:
    """Return unique Chinese names, including the han part of a mixed name."""
    found: list[str] = []
    for value in values:
        name = str(value or "").strip()
        for span in re.findall(r"[\u4e00-\u9fff]{2,}", name):
            if span not in found:
                found.append(span)
    return found


def _mb_json(url: str) -> dict | None:
    """Return one MusicBrainz JSON object.

    Purpose:
        Read Chinese artist and title aliases. MusicBrainz asks for a
        descriptive User-Agent and about one request per second.

    Parameters:
        url (str): A MusicBrainz ws/2 URL.

    Returns:
        dict | None: The parsed body. None when the request fails, so a
        network miss is not saved as "this song has no Chinese name".

    Precondition:
        url is absolute.

    Postcondition:
        No file is written.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _MB_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError, TimeoutError):
        return None
    return payload if isinstance(payload, dict) else None


def _cached_aliases(track_name: str, artist_name: str) -> tuple[list[str], list[str]]:
    """Return saved extra titles and artist names for this song."""
    if not _ALIAS_PATH.exists():
        return [], []
    try:
        raw = json.loads(_ALIAS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], []
    row = raw.get(f"{artist_name.casefold()}\u001f{track_name.casefold()}") if isinstance(raw, dict) else None
    if not isinstance(row, dict):
        return [], []
    titles = row.get("titles") if isinstance(row.get("titles"), list) else []
    artists = row.get("artists") if isinstance(row.get("artists"), list) else []
    return [str(item) for item in titles], [str(item) for item in artists]


def _both_scripts(names: list[str]) -> list[str]:
    """Return each name in simplified and traditional Chinese."""
    try:
        import zhconv
    except ImportError:
        return names
    found: list[str] = []
    for name in names:
        for variant in (zhconv.convert(name, "zh-cn"), zhconv.convert(name, "zh-tw"), name):
            if variant not in found:
                found.append(variant)
    return found


def _store_aliases(track_name: str, artist_name: str, titles: list[str], artists: list[str]) -> None:
    """Remember the Chinese names looked up for this song and refresh the spreadsheet."""
    raw: dict = {}
    if _ALIAS_PATH.exists():
        try:
            loaded = json.loads(_ALIAS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, json.JSONDecodeError):
            raw = {}
    raw[f"{artist_name.casefold()}\u001f{track_name.casefold()}"] = {"titles": titles, "artists": artists}
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    _ALIAS_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    with _ALIAS_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_ALIAS_FIELDS)
        writer.writeheader()
        for key, row in raw.items():
            artist, track = (key.split("\u001f", 1) + [""])[:2]
            if not isinstance(row, dict):
                continue
            writer.writerow(
                {
                    "artist_name": artist,
                    "track_name": track,
                    "chinese_artists": " | ".join(str(item) for item in row.get("artists") or []),
                    "chinese_titles": " | ".join(str(item) for item in row.get("titles") or []),
                }
            )


def chinese_search_names(track_name: str, artist_name: str) -> tuple[list[str], list[str]]:
    """Return extra Chinese titles and artist names to search on NetEase.

    Purpose:
        Spotify often shows an English artist, while NetEase files the same
        person under a Chinese name. A Chinese song can also be titled in
        English on Spotify. The extra names are pooled with the original
        search.

    Parameters:
        track_name (str): Playback title.
        artist_name (str): Playback artist.

    Returns:
        tuple[list[str], list[str]]: Extra titles, then extra artist names.
        Both lists are empty when the playback names are already Chinese.

    Precondition:
        Both arguments are strings.

    Postcondition:
        A successful lookup is cached under .netease/name_aliases.json.
    """
    if _has_han(track_name) and _has_han(artist_name):
        return [], []
    cached_titles, cached_artists = _cached_aliases(track_name, artist_name)
    if _alias_key_saved(track_name, artist_name):
        return cached_titles, cached_artists
    artist_names = _mb_artist_names(artist_name)
    if artist_names is None:
        return [], []
    titles = _mb_title_names(track_name, artist_name, artist_names) if not _has_han(track_name) else []
    if titles is None:
        return [], []
    titles = _both_scripts([name for name in titles if name.casefold() != track_name.casefold()])[:6]
    artist_names = _both_scripts(
        [name for name in artist_names if name.casefold() != artist_name.casefold()]
    )[:6]
    _store_aliases(track_name, artist_name, titles, artist_names)
    return titles, artist_names


def _alias_hit(wanted: str, detail: dict) -> bool:
    """Return True when this artist record is the playback artist."""
    labels = [detail.get("name"), detail.get("sort-name")]
    labels.extend(alias.get("name") for alias in (detail.get("aliases") or []))
    for label in labels:
        name = normalize_name(str(label or ""))
        if name == wanted:
            return True
        if _has_han(str(detail.get("name") or "")) and name.split()[:1] == [wanted]:
            return True
    return False


def _mb_artist_names(artist_name: str) -> list[str] | None:
    """Return Chinese names for an artist, or None when MusicBrainz cannot be reached."""
    query = urllib.parse.urlencode(
        {"query": f'artist:"{artist_name}" OR alias:"{artist_name}"', "fmt": "json", "limit": 8}
    )
    listing = _mb_json(f"{_MB_ARTIST}?{query}")
    if listing is None:
        return None
    wanted = normalize_name(artist_name)
    matched: list[dict] = []
    checked = 0
    for artist in listing.get("artists") or []:
        if int(artist.get("score") or 0) < 60 or not artist.get("id"):
            continue
        if checked >= 4:
            break
        checked += 1
        time.sleep(1.1)
        detail = _mb_json(f"{_MB_ARTIST}{artist['id']}?inc=aliases&fmt=json")
        if detail is None:
            return None
        if not _alias_hit(wanted, detail):
            continue
        names = _han_names([detail.get("name"), detail.get("sort-name")])
        if not names:
            aliases = [alias.get("name") for alias in (detail.get("aliases") or [])]
            names = _han_names(aliases)
        if names:
            return names
    return []


def _mb_title_names(track_name: str, artist_name: str, artist_aliases: list[str] | None = None) -> list[str] | None:
    """Return Chinese recording titles, or None when MusicBrainz cannot be reached."""
    queries = []
    for extra in (artist_aliases or [])[:2]:
        queries.append(f'alias:"{track_name}" AND artist:"{extra}"')
        queries.append(f'recording:"{track_name}" AND artist:"{extra}"')
    queries.append(f'recording:"{track_name}" AND artist:"{artist_name}"')
    titles: list[str] = []
    for text in queries[:3]:
        time.sleep(1.1)
        query = urllib.parse.urlencode({"query": text, "fmt": "json", "limit": 5})
        listing = _mb_json(f"{_MB_RECORDING}?{query}")
        if listing is None:
            return None
        for recording in (listing.get("recordings") or [])[:2]:
            if not recording.get("id"):
                continue
            time.sleep(1.1)
            detail = _mb_json(f"{_MB_RECORDING}{recording['id']}?inc=aliases&fmt=json")
            if detail is None:
                return None
            aliases = [alias.get("name") for alias in (detail.get("aliases") or [])]
            titles.extend(_han_names([detail.get("title"), *aliases]))
        if titles:
            break
    return titles


def _alias_key_saved(track_name: str, artist_name: str) -> bool:
    """Return True when this song was already looked up, even with no Chinese name."""
    if not _ALIAS_PATH.exists():
        return False
    try:
        raw = json.loads(_ALIAS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(raw, dict) and f"{artist_name.casefold()}\u001f{track_name.casefold()}" in raw


def collect_songs(track_name: str, artist_name: str, titles: list[str], artists: list[str]) -> list[dict]:
    """Search NetEase once per title and artist pair and pool the hits.

    Purpose:
        A Chinese catalog name and the English playback name should both
        be able to surface the same song before filtering.

    Parameters:
        track_name (str): Playback title.
        artist_name (str): Playback artist.
        titles (list[str]): Extra titles, such as a Chinese song name.
        artists (list[str]): Extra artist names, such as a Chinese stage name.

    Returns:
        list[dict]: Unique songs from every search that succeeded.

    Precondition:
        track_name is not empty.

    Postcondition:
        One failed search does not discard hits from the others. ValueError
        is raised only when every search fails.
    """
    pairs: list[tuple[str, str]] = [(track_name, artist_name)]
    for title in titles:
        pairs.append((title, artist_name))
    for artist in artists:
        pairs.append((track_name, artist))
        for title in titles:
            pairs.append((title, artist))
    seen: dict[int, dict] = {}
    failures = 0
    attempted = 0
    for title, artist in dict.fromkeys(pairs):
        attempted += 1
        try:
            rows = search_songs(title, artist)
        except (OSError, ValueError, json.JSONDecodeError, TimeoutError):
            failures += 1
            continue
        for row in rows:
            seen[int(row["id"])] = row
    if attempted and failures == attempted:
        raise OSError("NetEase search failed")
    return list(seen.values())


def _eapi_body(song_id: int) -> bytes:
    """Return the encrypted lyric request body for one NetEase song id.

    Purpose:
        Build the eapi form NetEase expects for /api/song/lyric/v1.

    Parameters:
        song_id (int): NetEase song id.

    Returns:
        bytes: application/x-www-form-urlencoded params.

    Precondition:
        song_id is positive.

    Postcondition:
        The body contains only the hex params field.
    """
    text = json.dumps({"id": song_id, "cp": "false", "lv": 0, "rv": 0, "yv": 1, "tv": 0}, separators=(",", ":"))
    path = "/api/song/lyric/v1"
    digest = hashlib.md5(f"nobody{path}use{text}md5forencrypt".encode("utf-8")).hexdigest()
    message = f"{path}-36cd479b6b5-{text}-36cd479b6b5-{digest}".encode("utf-8")
    pad = 16 - (len(message) % 16)
    message += bytes([pad]) * pad
    cipher = Cipher(algorithms.AES(_EAPI_KEY), modes.ECB())
    encryptor = cipher.encryptor()
    params = (encryptor.update(message) + encryptor.finalize()).hex()
    return urllib.parse.urlencode({"params": params}).encode("ascii")


def _decrypt_eapi(raw: bytes) -> dict:
    """Return a lyric JSON object from a plain or encrypted eapi body.

    Purpose:
        Accept either a JSON response or the hex AES body NetEase sometimes
        returns.

    Parameters:
        raw (bytes): HTTP response body.

    Returns:
        dict: Lyric payload.

    Precondition:
        raw is the full response.

    Postcondition:
        A dict is returned, or ValueError is raised.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    if text.startswith("{"):
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    cipher = Cipher(algorithms.AES(_EAPI_KEY), modes.ECB())
    decryptor = cipher.decryptor()
    plain = decryptor.update(bytes.fromhex(text)) + decryptor.finalize()
    pad = plain[-1]
    if 1 <= pad <= 16:
        plain = plain[:-pad]
    payload = json.loads(plain.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("NetEase lyric payload was not an object")
    return payload


def _lyric_field(node: object) -> str:
    """Return the lyric text from a NetEase lyric field."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        lyric = node.get("lyric")
        if isinstance(lyric, str):
            return lyric
    return ""


def word_lyric_result(song_id: int) -> tuple[list[dict], str, str]:
    """Return word lines plus why NetEase did not provide them.

    Purpose:
        A search hit can still have no word sync. The status key and detail
        are what the overlay shows instead of writing a JSON file.

    Parameters:
        song_id (int): NetEase song id.

    Returns:
        tuple[list[dict], str, str]: Lines, an overlay status key, and extra
        response text. The key and detail are empty when lines exist.

    Precondition:
        song_id is a NetEase id from search.

    Postcondition:
        No file is written. A network failure uses netease_error.
    """
    words, _lines, key, detail = fetch_lyrics(song_id)
    return words, key, detail


def fetch_lyrics(song_id: int) -> tuple[list[dict], list[dict], str, str]:
    """Return word lines and, when those are missing, synced line lyrics.

    Purpose:
        One NetEase response can carry word times or only an LRC. The line
        text is kept so it can be stored when LRCLIB has no synced lyric.

    Parameters:
        song_id (int): NetEase song id.

    Returns:
        tuple: Word lines, line lyrics, status key, and detail. The status
        key is empty when word lines exist.

    Precondition:
        song_id is a NetEase id from search.

    Postcondition:
        No file is written.
    """
    payload, fail = _lyric_payload(song_id)
    if payload is None:
        return [], [], fail[0], fail[1]
    words, key, detail = _word_lines_from_payload(payload)
    if words:
        return words, [], "", ""
    return [], _lrc_rows(_lyric_field(payload.get("lrc"))), key, detail


def _lrc_rows(text: str) -> list[dict]:
    """Turn a NetEase LRC string into timed lines with stored Chinese copies."""
    rows = [{"start_ms": line.time_ms, "text": line.text} for line in parse_synced_lyrics(text) if line.text]
    if rows:
        stamp_lines(rows)
    return rows


def _lyric_payload(song_id: int) -> tuple[dict | None, tuple[str, str]]:
    """Download one lyric response, or return a status when the request fails."""
    request = urllib.request.Request(
        _LYRIC_URL,
        data=_eapi_body(song_id),
        headers={
            "User-Agent": _USER_AGENT,
            "Referer": "https://music.163.com/",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = _decrypt_eapi(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            payload = _decrypt_eapi(body)
        except (ValueError, json.JSONDecodeError):
            return None, ("netease_refused", f"HTTP {exc.code}")
        return payload, ("", "")
    except (OSError, ValueError, json.JSONDecodeError, TimeoutError):
        return None, ("netease_error", "")
    return payload, ("", "")


def _word_lines_from_payload(payload: dict) -> tuple[list[dict], str, str]:
    """Turn one lyric response into lines or a reason the file was not written."""
    code = payload.get("code")
    message = payload.get("message") or payload.get("msg") or ""
    detail = str(message).strip()
    if isinstance(code, int) and code != 200:
        if detail:
            detail = f"code {code}. {detail}"
        else:
            detail = f"code {code}"
        return [], "netease_refused", detail
    if payload.get("nolyric") is True:
        return [], "netease_nolyric", detail
    if payload.get("uncollected") is True:
        return [], "netease_uncollected", detail
    for field, missing_key in (("yrc", "netease_unparsed"), ("klyric", "netease_unparsed")):
        text = _lyric_field(payload.get(field))
        if not text.strip():
            continue
        lines = parse_yrc(text)
        if lines:
            return lines, "", ""
        return [], missing_key, detail
    if _lyric_field(payload.get("lrc")).strip():
        return [], "netease_line_only", detail
    return [], "netease_none", detail


def parse_yrc(text: str) -> list[dict]:
    """Turn a yrc lyric string into lines of timed words.

    Purpose:
        Keep the word start and duration NetEase stored on each syllable.

    Parameters:
        text (str): yrc or klyric text.

    Returns:
        list[dict]: Lines that contain at least one word.

    Precondition:
        text is a string.

    Postcondition:
        The input string is unchanged.
    """
    lines: list[dict] = []
    for raw in text.splitlines():
        match = _LINE_RE.match(raw.strip())
        if match is None:
            continue
        words = []
        for word in _WORD_RE.finditer(match.group(3)):
            token = word.group(3)
            if not token:
                continue
            words.append(
                {
                    "text": token,
                    "start_ms": int(word.group(1)),
                    "duration_ms": int(word.group(2)),
                }
            )
        if not words:
            continue
        lines.append(
            {
                "start_ms": int(match.group(1)),
                "duration_ms": int(match.group(2)),
                "text": "".join(word["text"] for word in words),
                "words": words,
            }
        )
    return lines


def _lrclib_record(song: dict) -> dict:
    """Shape a NetEase song like an LRCLIB record so the same ranks apply."""
    return {
        "trackName": song["name"],
        "name": song["name"],
        "artistName": song["artist"],
        "albumName": song["album"],
        "duration": int(song["duration_ms"]) / 1000,
        "id": song["id"],
    }


def _best_name_rank(rank, record: dict, names: list[str]) -> int | None:
    """Return the best rank against any of the names, or None when none match."""
    found = [value for name in names if (value := rank(record, name)) is not None]
    if not found:
        return None
    return min(found)


def rank_songs(
    songs: list[dict],
    query: dict,
    titles: list[str] | None = None,
    artists: list[str] | None = None,
) -> list[dict]:
    """Return NetEase songs that match the title and artist, closest first.

    Purpose:
        Use the same title, artist, album, and duration ordering as the
        LRCLIB auto-pick. Extra Chinese titles and artist names count as
        the same song.

    Parameters:
        songs (list[dict]): Rows from search_songs.
        query (dict): spotify_track_query result.
        titles (list[str] | None): Extra titles that should also match.
        artists (list[str] | None): Extra artist names that should also match.

    Returns:
        list[dict]: Matching songs, best first. Each row gains delta_ms.

    Precondition:
        query contains track_name, artist_name, album_name, and duration_ms.

    Postcondition:
        songs is unchanged. A row is included only when one title and one
        artist name match.
    """
    title_names = [query["track_name"], *(titles or [])]
    artist_names = [query["artist_name"], *(artists or [])]
    ranked: list[tuple[int, int, int, int, dict]] = []
    for song in songs:
        record = _lrclib_record(song)
        title = _best_name_rank(_title_rank, record, title_names)
        artist = _best_name_rank(_artist_rank, record, artist_names)
        if title is None or artist is None:
            continue
        album = _album_rank(record, query["album_name"])
        delta = abs(int(song["duration_ms"]) - int(query["duration_ms"]))
        ranked.append((title, artist, album, delta, song))
    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]["id"]))
    rows: list[dict] = []
    for _title, _artist, _album, delta, song in ranked:
        row = dict(song)
        row["delta_ms"] = delta
        rows.append(row)
    return rows


def candidate_rows(songs: list[dict]) -> list[LyricCandidate]:
    """Return the overlay rows for an undecided NetEase match.

    Purpose:
        Give the same top-five preview the LRCLIB picker uses. The link
        opens the NetEase song page.

    Parameters:
        songs (list[dict]): Ranked songs from rank_songs.

    Returns:
        list[LyricCandidate]: At most five rows.

    Precondition:
        Each song has id, name, artist, album, and duration_ms.

    Postcondition:
        songs is unchanged.
    """
    rows: list[LyricCandidate] = []
    for song in songs[:5]:
        label = (
            f"{song['name']} — {song['artist']} · {song['album']} · "
            f"{format_clock(int(song['duration_ms']))}"
        )
        rows.append(LyricCandidate(int(song["id"]), label, f"https://music.163.com/song?id={song['id']}"))
    return rows


def save_word_lyrics(query: dict, song: dict, lines: list[dict]) -> Path:
    """Write one JSON file for a NetEase word-synced lyric.

    Purpose:
        Keep the word times on disk without drawing them.

    Parameters:
        query (dict): Spotify title, artist, album, and duration.
        song (dict): Chosen NetEase song.
        lines (list[dict]): Timed lines from lyric_lines.

    Returns:
        Path: The file that was written.

    Precondition:
        lines is non-empty.

    Postcondition:
        .netease/<song id>.json exists and is valid JSON. Each line and word
        stores a simplified copy and a traditional copy beside the original.
    """
    stamp_lines(lines)
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _OUT_DIR / f"{song['id']}.json"
    payload = {
        "track_name": query.get("track_name"),
        "artist_name": query.get("artist_name"),
        "album_name": query.get("album_name"),
        "duration_ms": query.get("duration_ms"),
        "netease_id": song["id"],
        "netease_name": song["name"],
        "netease_artist": song["artist"],
        "netease_album": song["album"],
        "netease_duration_ms": song["duration_ms"],
        "lines": lines,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class NeteaseChoiceStore:
    """Remember which NetEase song id belongs to each track.

    The JSON map is the song key to the NetEase id. The spreadsheet beside
    it lists the title, artist, album, duration, and that same id.
    """

    def __init__(self) -> None:
        """Load the saved map and index word-lyric files already on disk.

        Postcondition:
            get(key) returns a NetEase id when this song was saved before.
        """
        self.choices: dict[str, int] = {}
        self.catalog: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()
        if _CHOICES_PATH.exists():
            raw = json.loads(_CHOICES_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.choices = {str(key): int(value) for key, value in raw.items()}
        if _CATALOG_PATH.exists():
            with _CATALOG_PATH.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    key = media_choice_key(
                        str(row.get("track_name") or ""),
                        str(row.get("artist_name") or ""),
                        str(row.get("album_name") or ""),
                    )
                    if key:
                        self.catalog[key] = {field: str(row.get(field) or "") for field in _CATALOG_FIELDS}
        self._backfill()

    def get(self, key: str) -> int | None:
        """Return the saved NetEase id for this song, or None."""
        with self._lock:
            return self.choices.get(key)

    def put(self, key: str, query: dict, netease_id: int) -> None:
        """Save the playback key and the title-artist-album key for one NetEase id.

        Purpose:
            The next play of this Spotify song loads .netease/<id>.json from
            the local map. A Spotify track id and the title, artist, and
            album both point at that same file.

        Parameters:
            key (str): Song key from the current playback source.
            query (dict): Title, artist, album, and duration.
            netease_id (int): NetEase song id whose word file was written.

        Returns:
            None.

        Precondition:
            key is non-empty and the word file for netease_id exists.

        Postcondition:
            lines_for(query) reads that word file. The JSON map and the CSV
            each contain this song.
        """
        named = media_choice_key(
            str(query.get("track_name") or ""),
            str(query.get("artist_name") or ""),
            str(query.get("album_name") or ""),
        )
        keys = [named] if named else []
        if key and key not in keys:
            keys.append(key)
        row = {
            "track_name": str(query.get("track_name") or ""),
            "artist_name": str(query.get("artist_name") or ""),
            "album_name": str(query.get("album_name") or ""),
            "duration": format_clock(int(query.get("duration_ms") or 0)),
            "netease_id": str(netease_id),
        }
        with self._lock:
            unchanged = self.catalog.get(named) == row and all(self.choices.get(item) == netease_id for item in keys)
            if unchanged:
                return
            for item in keys:
                self.choices[item] = netease_id
            if named:
                self.catalog[named] = row
            self._write()

    def lines_for(self, query: dict) -> list[dict] | None:
        """Return the saved word lines for this Spotify song, or None.

        Purpose:
            Resolve the NetEase id from the local map, then read the word
            file. No NetEase request is made.

        Parameters:
            query (dict): Playback title, artist, album, duration, and key.

        Returns:
            list[dict] | None: The lines array from .netease/<id>.json.

        Precondition:
            query contains track_name, artist_name, album_name, and key.

        Postcondition:
            A missing word file drops that id from the map. A hit also
            stores the current playback key so the next play is direct.
        """
        song_id = self._resolve_id(query)
        if song_id is None:
            return None
        path = _OUT_DIR / f"{song_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._forget_id(song_id)
            return None
        lines = payload.get("lines") if isinstance(payload, dict) else None
        if not isinstance(lines, list) or not lines:
            return None
        if stamp_lines(lines):
            payload["lines"] = lines
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.put(str(query.get("key") or ""), query, song_id)
        return lines

    def _resolve_id(self, query: dict) -> int | None:
        """Return the NetEase id saved for this title and artist, if the file is known."""
        playback = str(query.get("key") or "")
        named = media_choice_key(
            str(query.get("track_name") or ""),
            str(query.get("artist_name") or ""),
            str(query.get("album_name") or ""),
        )
        with self._lock:
            for key in (playback, named):
                if key and key in self.choices:
                    return self.choices[key]
            found: list[int] = []
            for row in self.catalog.values():
                record = {
                    "trackName": row.get("track_name") or "",
                    "artistName": row.get("artist_name") or "",
                    "albumName": row.get("album_name") or "",
                }
                if _title_rank(record, str(query.get("track_name") or "")) is None:
                    continue
                if _artist_rank(record, str(query.get("artist_name") or "")) is None:
                    continue
                try:
                    song_id = int(row.get("netease_id") or 0)
                except ValueError:
                    continue
                if song_id and song_id not in found:
                    found.append(song_id)
            if len(found) == 1:
                return found[0]
        return None

    def _forget_id(self, song_id: int) -> None:
        """Drop every map entry that points at a word file we can no longer read."""
        with self._lock:
            keys = [key for key, value in self.choices.items() if value == song_id]
            named = [key for key, row in self.catalog.items() if str(row.get("netease_id")) == str(song_id)]
            if not keys and not named:
                return
            for key in keys:
                self.choices.pop(key, None)
            for key in named:
                self.catalog.pop(key, None)
            self._write()

    def _backfill(self) -> None:
        """Index word-lyric JSON files that were saved before the map existed."""
        if not _OUT_DIR.exists():
            return
        for path in _OUT_DIR.glob("*.json"):
            if path.name == _CHOICES_PATH.name:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or not payload.get("lines"):
                continue
            song_id = payload.get("netease_id")
            track = str(payload.get("track_name") or "")
            artist = str(payload.get("artist_name") or "")
            if not isinstance(song_id, int) or not track or not artist:
                continue
            key = media_choice_key(track, artist, str(payload.get("album_name") or ""))
            self.put(
                key,
                {
                    "track_name": track,
                    "artist_name": artist,
                    "album_name": str(payload.get("album_name") or ""),
                    "duration_ms": payload.get("duration_ms") or 0,
                },
                song_id,
            )

    def _write(self) -> None:
        """Write the id map and the spreadsheet. The caller holds self._lock."""
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        _CHOICES_PATH.write_text(
            json.dumps(self.choices, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        with _CATALOG_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=_CATALOG_FIELDS)
            writer.writeheader()
            seen: set[str] = set()
            for row in self.catalog.values():
                song_id = row.get("netease_id") or ""
                if not song_id or song_id in seen:
                    continue
                seen.add(song_id)
                writer.writerow(row)


def _word_fill(word: dict, progress_ms: int) -> float:
    """Return how much of one word has been sung, from 0 to 1.

    Purpose:
        A word that lasts 3000 ms is one third filled after 1000 ms. The
        fill moves from the left of the glyphs to the right.

    Parameters:
        word (dict): A NetEase word with start_ms and duration_ms.
        progress_ms (int): Playback position in milliseconds.

    Returns:
        float: 0 before the word, 1 after it, and the elapsed fraction during it.

    Precondition:
        progress_ms is >= 0.

    Postcondition:
        The returned value is between 0 and 1. word is unchanged.
    """
    start = word.get("start_ms")
    duration = word.get("duration_ms")
    if not isinstance(start, int) or progress_ms < start:
        return 0.0
    if not isinstance(duration, int) or duration <= 0:
        return 1.0
    return max(0.0, min(1.0, (progress_ms - start) / duration))


def _catalog_kind(document: dict) -> str:
    """Return word sync, line sync, or no synced lyric for a catalog document."""
    try:
        sync = int(document.get("sync"))
    except (TypeError, ValueError):
        sync = -1
    if sync == 1:
        kind = "word sync"
    elif sync == 0:
        kind = "line sync"
    else:
        kind = "no synced lyric"
    if document.get("preference"):
        kind += ", your choice"
    return kind


class NeteaseLookup:
    """Search NetEase when the song changes or the user skips inside it.

    Purpose:
        Try NetEase before the overlay settles on LRCLIB. A clear duration
        match is saved immediately. Several distant matches become overlay
        candidates. A miss leaves LRCLIB to draw the lines.
    """

    def __init__(self) -> None:
        """Start with no song checked.

        Postcondition:
            phase() is "ready" and no network call has been made.
        """
        self._lock = threading.Lock()
        self._phase = "ready"
        self._song_key: str | None = None
        self._busy = False
        self._choices: list[LyricCandidate] = []
        self._choice_songs: dict[int, dict] = {}
        self._query: dict | None = None
        self._pending: dict | None = None
        self._detail = ""
        self._reason_key = ""
        self._lines: list[dict] = []
        self._finished: dict[str, str] = {}
        self._store = NeteaseChoiceStore()

    def consider(self, now: dict | None, seeked: bool = False) -> None:
        """Start a NetEase check after a song change or a skip.

        Purpose:
            A new song is checked at once. A skip inside the current song
            checks it again only when the last try failed. A song that
            already has a result is not requested again.

        Parameters:
            now (dict | None): Spotify currently-playing body.
            seeked (bool): True when playback jumped inside the same song.

        Returns:
            None.

        Precondition:
            consider may be called from the Qt timer thread.

        Postcondition:
            A new song starts a search. A failed song starts a search only
            when seeked is True. A finished song keeps its result.
        """
        query = spotify_track_query(now)
        if query is None:
            return
        key = str(query["key"])
        with self._lock:
            if self._busy:
                if key != self._song_key:
                    self._pending = query
                return
            if key == self._song_key and self._phase in ("search", "choose", "saved", "none", "words", "lines", "miss"):
                return
            if key == self._song_key and self._phase == "error":
                if seeked:
                    self._start_locked(query)
                return
            cached = lyric_catalog.local_get(query)
            if cached is not None:
                lyric_catalog.announce(query, "local", _catalog_kind(cached))
                self._assign_catalog(key, query, cached)
                return
            if key in self._finished:
                self._song_key = key
                self._phase = self._finished[key]
                self._choices = []
                return
            self._start_locked(query)

    def _start_locked(self, query: dict) -> None:
        """Queue the search. The caller holds self._lock."""
        self._busy = True
        self._phase = "search"
        self._song_key = str(query["key"])
        self._query = query
        self._choices = []
        self._choice_songs = {}
        self._lines = []
        threading.Thread(target=self._search, args=(query,), name="netease-lyrics", daemon=True).start()

    def _search(self, query: dict) -> None:
        """Search, auto-pick or store candidates, then fetch word lines."""
        key = str(query["key"])
        cached = lyric_catalog.get(query)
        if cached is not None:
            place = "MongoDB" if cached.pop("source_hit", None) == "mongo" else "local"
            lyric_catalog.announce(query, place, _catalog_kind(cached))
            with self._lock:
                if self._song_key != key:
                    self._busy = False
                    return
                self._assign_catalog(key, query, cached)
                self._busy = False
            return
        try:
            extra_titles, extra_artists = chinese_search_names(query["track_name"], query["artist_name"])
            pooled = collect_songs(query["track_name"], query["artist_name"], extra_titles, extra_artists)
            ranked = rank_songs(pooled, query, extra_titles, extra_artists)
        except (OSError, ValueError, json.JSONDecodeError, TimeoutError):
            lyric_catalog.announce(query, "NetEase", "no response")
            self._finish(key, "error", query)
            return
        if not ranked:
            lyric_catalog.announce(query, "NetEase", "no synced lyric")
            self._finish(key, "none", query)
            return
        close = [song for song in ranked if song["delta_ms"] <= DURATION_CHOICE_THRESHOLD_MS]
        if close:
            self._fetch_first_synced(query, close)
            return
        choices = candidate_rows(ranked)
        songs = {int(song["id"]): song for song in ranked[:5]}
        with self._lock:
            if self._song_key != key:
                self._busy = False
                return
            self._choices = choices
            self._choice_songs = songs
            self._phase = "choose"
            self._busy = False
        lyric_catalog.announce(query, "NetEase", "choose a version")

    def _fetch_first_synced(self, query: dict, songs: list[dict]) -> None:
        """Save the first close match that has word lines, or record a miss.

        Purpose:
            A title and artist match whose length is within the LRCLIB
            threshold is not shown as a candidate. If that song has no word
            sync, the next close match is tried before LRCLIB takes over.
        """
        key = str(query["key"])
        reason_key = "netease_none"
        reason_detail = ""
        line_fallback: list[dict] = []
        for song in songs:
            words, line_rows, status_key, detail = fetch_lyrics(int(song["id"]))
            if not words:
                if line_rows and not line_fallback:
                    line_fallback = line_rows
                reason_key = status_key or "netease_none"
                reason_detail = detail
                continue
            lyric_catalog.drop_hold(query)
            lyric_catalog.put_shared(query, 1, words)
            lyric_catalog.announce(query, "NetEase", "word sync")
            self._finish(key, "saved", query, "", lines=words)
            return
        if line_fallback:
            lyric_catalog.hold_lines(query, line_fallback)
            lyric_catalog.announce(query, "NetEase", "line sync, checking LRCLIB")
            self._finish(key, "none", query, reason_detail, "netease_line_only")
            return
        phase = "error" if reason_key == "netease_error" else "none"
        if phase == "error":
            lyric_catalog.announce(query, "NetEase", "no response")
        else:
            lyric_catalog.announce(query, "NetEase", "no synced lyric")
        self._finish(key, phase, query, reason_detail, reason_key)

    def _fetch(self, query: dict, song: dict) -> None:
        """Download word lines for the song the overlay selected."""
        key = str(query["key"])
        words, line_rows, status_key, detail = fetch_lyrics(int(song["id"]))
        if words:
            lyric_catalog.drop_hold(query)
            lyric_catalog.put_preference(query, 1, words)
            lyric_catalog.announce(query, "NetEase", "word sync, your choice")
            self._finish(key, "saved", query, "", lines=words)
            return
        if line_rows:
            lyric_catalog.drop_hold(query)
            lyric_catalog.put_preference(query, 0, line_rows)
            lyric_catalog.announce(query, "NetEase", "line sync, your choice")
            self._finish(key, "lines", query, "", lines=line_rows)
            return
        phase = "error" if status_key == "netease_error" else "none"
        self._finish(key, phase, query, detail, status_key or "netease_none")

    def _finish(
        self,
        key: str,
        phase: str,
        query: dict,
        detail: str = "",
        status_key: str = "",
        lines: list[dict] | None = None,
    ) -> None:
        """Store the outcome for this song and clear the in-flight flag."""
        with self._lock:
            self._busy = False
            if phase != "error":
                self._finished[key] = phase
            pending = self._pending
            self._pending = None
            if pending is not None and str(pending["key"]) != key:
                pending_key = str(pending["key"])
                if pending_key in self._finished:
                    self._song_key = pending_key
                    self._phase = self._finished[pending_key]
                    self._query = pending
                    self._choices = []
                    return
                self._start_locked(pending)
                return
            if self._song_key == key:
                self._phase = phase
                self._query = query
                self._choices = []
                self._reason_key = status_key
                self._detail = detail
                if lines is not None:
                    self._lines = lines

    def _assign_catalog(self, key: str, query: dict, document: dict) -> None:
        """Apply a catalog document. The caller holds self._lock."""
        lines = document.get("lines") if isinstance(document.get("lines"), list) else []
        try:
            sync = int(document.get("sync"))
        except (TypeError, ValueError):
            sync = -1
        self._song_key = key
        self._query = query
        self._choices = []
        self._reason_key = ""
        self._detail = ""
        if sync == 1 and lines:
            self._phase = "words"
            self._lines = lines
            self._finished[key] = "saved"
            return
        if sync == 0 and lines:
            self._phase = "lines"
            self._lines = lines
            self._finished[key] = "lines"
            return
        self._phase = "miss"
        self._lines = []
        self._finished[key] = "miss"

    def adopt_lines(self, query: dict, lines: list[dict]) -> None:
        """Show NetEase line lyrics after LRCLIB had no synced record."""
        key = str(query["key"])
        with self._lock:
            self._assign_catalog(key, query, {"sync": 0, "lines": lines})

    def mark_miss(self, query: dict) -> None:
        """Stop searching this song. Both sources had no synced lyric."""
        key = str(query["key"])
        with self._lock:
            self._assign_catalog(key, query, {"sync": -1, "lines": []})

    def line_slots(self, progress_ms: int, script: str = "original") -> tuple[str, str, str] | None:
        """Return the left line, the right line, and which side is current."""
        with self._lock:
            if self._phase != "lines" or not self._lines:
                return None
            rows = self._lines
        timed = tuple(
            LyricLine(int(row.get("start_ms") or 0), shown_text(row, script))
            for row in rows
            if isinstance(row, dict) and str(row.get("text") or "")
        )
        return lyric_slots(timed, progress_ms)

    def phase(self) -> str:
        """Return search, choose, saved, words, lines, miss, none, error, or ready."""
        with self._lock:
            return self._phase

    def frame_at(self, progress_ms: int, script: str = "original") -> tuple[str, tuple[tuple[str, float], ...], str] | None:
        """Return the current NetEase line, each word's fill, and the next line.

        Purpose:
            Fill is how much of that word's duration has elapsed, from 0 to
            1. Earlier words are 1. Later words are 0. The current word is
            the fraction of its own duration. Even line indexes stay on the
            left and odd indexes stay on the right.

        Parameters:
            progress_ms (int): Playback position in milliseconds.
            script (str): original, simplified, or traditional. Stored copies
                are used. Characters that are not Chinese stay as written.

        Returns:
            tuple | None: Side ("left" or "right"), words as (text, fill),
            and the following line. None when this song has no word lines.

        Precondition:
            progress_ms is >= 0.

        Postcondition:
            The stored lines are unchanged.
        """
        with self._lock:
            if self._phase not in ("saved", "words") or not self._lines:
                return None
            lines = self._lines
        visible = [line for line in lines if str(line.get("text") or "")]
        if not visible:
            return None
        index = 0
        for cursor in range(len(visible) - 1):
            nxt = visible[cursor + 1].get("start_ms")
            if isinstance(nxt, int) and progress_ms < nxt:
                break
            index = cursor + 1
        words = visible[index].get("words") or []
        flagged = tuple(
            (shown_text(word, script), _word_fill(word, progress_ms))
            for word in words
            if isinstance(word, dict) and str(word.get("text") or "")
        )
        if not flagged:
            flagged = ((shown_text(visible[index], script), 0.0),)
        following = shown_text(visible[index + 1], script) if index + 1 < len(visible) else ""
        side = "left" if index % 2 == 0 else "right"
        return side, flagged, following

    def song_key(self) -> str:
        """Return the Spotify key this lookup is handling, or an empty string."""
        with self._lock:
            return self._song_key or ""

    def reason_key(self) -> str:
        """Return the overlay text key for the latest NetEase response."""
        with self._lock:
            return self._reason_key

    def detail(self) -> str:
        """Return extra words from the latest NetEase response, such as a code."""
        with self._lock:
            return self._detail

    def candidates(self) -> list[LyricCandidate]:
        """Return the current NetEase choices, or an empty list."""
        with self._lock:
            return list(self._choices)

    def accept(self, song_id: int) -> None:
        """Fetch word lines for the candidate the overlay selected.

        Purpose:
            Same role as accepting an LRCLIB id. The click returns at once.

        Parameters:
            song_id (int): NetEase id from a LyricCandidate.

        Returns:
            None.

        Precondition:
            song_id was offered in candidates().

        Postcondition:
            A background fetch starts when that id is still pending.
        """
        with self._lock:
            song = self._choice_songs.get(int(song_id))
            query = self._query
            if song is None or query is None or self._busy:
                return
            self._busy = True
            self._phase = "search"
            self._choices = []
        threading.Thread(target=self._fetch, args=(query, song), name="netease-pick", daemon=True).start()
