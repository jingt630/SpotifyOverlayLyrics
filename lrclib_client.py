"""Read synced lyrics from LRCLIB and pick the line for a playback position.

LRCLIB does not require an account or an API key. Docs: https://lrclib.net/docs
Every endpoint and its JSON shape is listed in lrclib_api.md.

Spotify gives the song identity and progress_ms. LRCLIB gives lines that
each start at a timestamp. The current line is the last line whose timestamp
is still at or before progress_ms.

Several LRCLIB records can share a title and artist and still differ by a
few milliseconds. This client locks one record for a Spotify track id so
later position checks reuse that same timing instead of swapping versions.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

# Public LRCLIB API. No key is sent.
LRCLIB_API = "https://lrclib.net"
# LRCLIB asks every app to identify itself. There is no account to create.
# https://lrclib.net/docs
USER_AGENT = "SpotifyOverlayLyrics/0.1 (personal local lyrics overlay)"
# One LRC timestamp. [mm:ss.xx] uses decimal seconds, so .12 means 120 milliseconds.
LINE_PATTERN = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


class LrcLibError(RuntimeError):
    """LRCLIB returned an error other than 'this track was not found'."""


@dataclass(frozen=True)
class LyricLine:
    """One synced lyric line.

    Attributes:
        time_ms (int): Start time of the line, in milliseconds from 0.
        text (str): The words. Empty when the timestamp is only a marker.
    """

    time_ms: int
    text: str


@dataclass(frozen=True)
class LyricMatch:
    """The one LRCLIB record chosen for a Spotify track.

    Attributes:
        record_id (int): LRCLIB id. Later reads of the same song reuse it.
        track_name (str): Title stored on that record.
        artist_name (str): Artist stored on that record.
        album_name (str): Album stored on that record.
        duration_sec (int): Duration field stored on the LRCLIB record, in seconds.
            This field is not used to choose the record.
        lyric_duration_ms (int): Timestamp of the last synced line, in milliseconds.
            This is the length of the lyric file.
        duration_delta_ms (int): Absolute gap between lyric_duration_ms and
            Spotify's duration_ms.
        lines (tuple[LyricLine, ...]): Synced lines, sorted by time_ms.
        source (str): "get" when /api/get chose it, otherwise "search".
        candidate_count (int): How many synced records were inside the duration window.
        reason (str): Why this record won over the other candidates.
    """

    record_id: int
    track_name: str
    artist_name: str
    album_name: str
    duration_sec: int
    lyric_duration_ms: int
    duration_delta_ms: int
    lines: tuple[LyricLine, ...]
    source: str
    candidate_count: int
    reason: str


def normalize_name(value: str) -> str:
    """Lowercase a title, artist, or album and drop punctuation.

    Purpose:
        Compare Spotify and LRCLIB names that differ only by case or symbols.

    Parameters:
        value (str): Raw name.

    Returns:
        str: Words separated by single spaces.

    Precondition:
        value is a string.

    Postcondition:
        The result contains only lowercase letters, digits, and single spaces.
    """
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return " ".join(cleaned.split())


def base_title(track_name: str) -> str:
    """Return the title without a trailing ' - Remastered' style suffix.

    Purpose:
        Search LRCLIB with the song name when the Spotify title has an extra label.

    Parameters:
        track_name (str): Spotify track name.

    Returns:
        str: Text before the first ' - ', or the whole name when there is none.

    Precondition:
        track_name is a string.

    Postcondition:
        The result is track_name, or a shorter prefix of it.
    """
    return track_name.split(" - ", 1)[0].strip()


def parse_synced_lyrics(synced: str) -> tuple[LyricLine, ...]:
    """Turn an LRCLIB syncedLyrics string into timed lines.

    Purpose:
        Read LRC lines such as '[00:17.12] words' into milliseconds and text.
        A line may contain more than one timestamp.

    Parameters:
        synced (str): The syncedLyrics field from an LRCLIB record.

    Returns:
        tuple[LyricLine, ...]: Lines sorted by time_ms. Empty input returns ().

    Precondition:
        synced is a string. Timestamps use [minutes:seconds.fraction].

    Postcondition:
        Every returned time_ms is >= 0 and the tuple is ordered by time_ms.
        Text after a timestamp is kept, including a blank marker line.
    """
    lines: list[LyricLine] = []
    for raw_line in synced.splitlines():
        stamps = list(LINE_PATTERN.finditer(raw_line))
        if not stamps:
            continue
        text = raw_line[stamps[-1].end() :].strip()
        for stamp in stamps:
            minutes = int(stamp.group(1))
            seconds = float(stamp.group(2))
            time_ms = int(round((minutes * 60 + seconds) * 1000))
            lines.append(LyricLine(time_ms=time_ms, text=text))
    lines.sort(key=lambda line: line.time_ms)
    return tuple(lines)


def line_at(lines: tuple[LyricLine, ...], progress_ms: int) -> tuple[LyricLine | None, LyricLine | None]:
    """Find the lyric line that should be showing at progress_ms.

    Purpose:
        Choose the last non-empty line whose start time is still <= progress_ms,
        and the next non-empty line after it.

    Parameters:
        lines (tuple[LyricLine, ...]): Sorted synced lines from one locked record.
        progress_ms (int): Spotify playback position in milliseconds.

    Returns:
        tuple[LyricLine | None, LyricLine | None]: (current, next). current is
        None when playback is still before the first non-empty line.

    Precondition:
        lines are sorted by time_ms. progress_ms is >= 0.

    Postcondition:
        The Spotify position is not modified. current.time_ms <= progress_ms
        when current is not None. next is the following non-empty line, or None.
    """
    current: LyricLine | None = None
    current_index = -1
    for index, line in enumerate(lines):
        if line.time_ms > progress_ms:
            break
        if line.text:
            current = line
            current_index = index
    next_line: LyricLine | None = None
    for line in lines[current_index + 1 :]:
        if line.text:
            next_line = line
            break
    return current, next_line


def lyric_duration_ms(lines: tuple[LyricLine, ...]) -> int | None:
    """Return the last synced timestamp, which is how long the lyric file runs.

    Purpose:
        Measure the lyrics themselves. LRCLIB's duration field can be shorter
        than the last line, so the stored field is not the lyric length.

    Parameters:
        lines (tuple[LyricLine, ...]): Synced lines sorted by time_ms.

    Returns:
        int | None: time_ms of the last line, or None when lines is empty.

    Precondition:
        lines is sorted by time_ms.

    Postcondition:
        The returned value is lines[-1].time_ms when lines is not empty.
    """
    if not lines:
        return None
    return lines[-1].time_ms


def format_ms(duration_ms: int) -> str:
    """Format milliseconds as m:ss.xxx."""
    minutes, remainder = divmod(abs(duration_ms), 60000)
    seconds = remainder / 1000
    return f"{minutes}:{seconds:06.3f}"


def _rank_key(
    record: dict,
    track_name: str,
    artist_name: str,
    album_name: str,
    duration_ms: int,
) -> tuple[int, int, int, int, int] | None:
    """Build the sort key for one search hit, or None when it cannot be used.

    Smaller keys win. Order: title, artist, gap between Spotify's duration and
    the last lyric timestamp, album, then LRCLIB id.
    """
    synced = (record.get("syncedLyrics") or "").strip()
    if record.get("instrumental") or not synced:
        return None
    lines = parse_synced_lyrics(synced)
    lyric_ms = lyric_duration_ms(lines)
    if lyric_ms is None or not any(line.text for line in lines):
        return None
    record_title = normalize_name(str(record.get("trackName") or record.get("name") or ""))
    wanted_title = normalize_name(track_name)
    wanted_base = normalize_name(base_title(track_name))
    if record_title == wanted_title:
        title_rank = 0
    elif record_title == wanted_base:
        title_rank = 1
    else:
        return None
    record_artist = normalize_name(str(record.get("artistName") or ""))
    wanted_artist = normalize_name(artist_name)
    first_artist = normalize_name(artist_name.split(",")[0])
    if record_artist == wanted_artist:
        artist_rank = 0
    elif first_artist and first_artist in record_artist:
        artist_rank = 1
    else:
        artist_rank = 2
    album_rank = 0 if normalize_name(str(record.get("albumName") or "")) == normalize_name(album_name) else 1
    record_id = int(record.get("id") or 0)
    return (title_rank, artist_rank, abs(duration_ms - lyric_ms), album_rank, record_id)


def choose_synced_record(
    records: list[dict],
    track_name: str,
    artist_name: str,
    album_name: str,
    duration_ms: int,
) -> tuple[dict, int, str] | None:
    """Pick one synced record from the LRCLIB results.

    Purpose:
        Keep records whose title matches, then prefer the closer artist, then
        the lyric file whose last timestamp is closest to Spotify's duration,
        then the same album. LRCLIB's stored duration field is not used.

    Parameters:
        records (list[dict]): JSON objects from GET /api/search and /api/get.
        track_name (str): Spotify track name.
        artist_name (str): Spotify artists joined with ', '.
        album_name (str): Spotify album name.
        duration_ms (int): Spotify item.duration_ms.

    Returns:
        tuple[dict, int, str] | None: The winning record, how many synced
        title matches were ranked, and a short reason. None when no synced
        record has a matching title.

    Precondition:
        Each record is a dict. duration_ms is positive.

    Postcondition:
        The returned record, when present, has synced lyrics and a matching
        title. records is not modified.
    """
    ranked: list[tuple[tuple[int, int, int, int, int], dict]] = []
    for record in records:
        key = _rank_key(record, track_name, artist_name, album_name, duration_ms)
        if key is not None:
            ranked.append((key, record))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    winner_key, winner = ranked[0]
    title_rank, artist_rank, delta_ms, album_rank, _record_id = winner_key
    reason = (
        f"title rank {title_rank}, artist rank {artist_rank}, "
        f"last lyric is {format_ms(delta_ms)} from Spotify's duration, "
        f"album rank {album_rank}"
    )
    return winner, len(ranked), reason


class LrcLibClient:
    """HTTP client for the read-only LRCLIB lyrics endpoints.

    No account is stored. The client sends User-Agent on every request and
    honors Retry-After once when LRCLIB answers 429.
    """

    def __init__(self, api_root: str = LRCLIB_API, user_agent: str = USER_AGENT) -> None:
        """Store the API root and the client name sent in User-Agent.

        Purpose:
            Create a client that has not called the network yet.

        Parameters:
            api_root (str): Origin such as https://lrclib.net. Defaults to LRCLIB_API.
            user_agent (str): Value required by LRCLIB's identification rule.

        Returns:
            None.

        Precondition:
            api_root has no trailing path. user_agent is non-empty.

        Postcondition:
            No request has been sent.
        """
        self.api_root = api_root.rstrip("/")
        self.user_agent = user_agent

    def _get_json(self, path: str, query: dict[str, str | int]) -> tuple[int, object]:
        """GET one LRCLIB path and return the status plus the parsed JSON.

        Purpose:
            Send an identified request. A 429 waits for Retry-After once, up to
            5 seconds, then tries that same request one more time.

        Parameters:
            path (str): Path beginning with /api/.
            query (dict[str, str | int]): Query fields. Empty values are omitted.

        Returns:
            tuple[int, object]: HTTP status and the JSON value. 404 returns
            the error object instead of raising.

        Precondition:
            path is an LRCLIB read endpoint. The network is reachable.

        Postcondition:
            On 200, the second item is the JSON body. On 404, the status is 404.
            Other HTTP errors raise LrcLibError. At most one retry happens.
        """
        params = {key: value for key, value in query.items() if value != ""}
        url = f"{self.api_root}{path}?{urllib.parse.urlencode(params)}" if params else f"{self.api_root}{path}"
        _print_lrcib_request(path, params)
        for attempt in (1, 2):
            request = urllib.request.Request(url, headers={"User-Agent": self.user_agent}, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read().decode("utf-8")
                    return response.status, json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code == 404:
                    try:
                        return 404, json.loads(detail) if detail else None
                    except json.JSONDecodeError:
                        return 404, None
                if exc.code == 429 and attempt == 1:
                    wait = min(int(exc.headers.get("Retry-After") or "1"), 5)
                    time.sleep(wait)
                    continue
                raise LrcLibError(f"LRCLIB request failed ({exc.code}): {detail}") from exc
        raise LrcLibError("LRCLIB request failed after the rate-limit wait.")

    def get_by_signature(
        self,
        track_name: str,
        artist_name: str,
        album_name: str,
        duration_sec: int,
    ) -> dict | None:
        """Call GET /api/get, LRCLIB's own best match for one recording.

        Purpose:
            Ask LRCLIB to choose the published record for this title, artist,
            album, and duration. Their server only matches when the duration
            is within ±2 seconds.

        Parameters:
            track_name (str): Track title.
            artist_name (str): Artist name.
            album_name (str): Album name. Included because it improves their match.
            duration_sec (int): Duration in whole seconds, from 1 to 3600.

        Returns:
            dict | None: The lyrics record, or None when LRCLIB returns 404.

        Precondition:
            track_name and artist_name are non-empty.

        Postcondition:
            On success, the dict includes id, trackName, artistName, albumName,
            duration, syncedLyrics, and plainLyrics. No lyrics are published.
        """
        status, body = self._get_json(
            "/api/get",
            {
                "track_name": track_name,
                "artist_name": artist_name,
                "album_name": album_name,
                "duration": duration_sec,
            },
        )
        if status == 404 or not isinstance(body, dict) or "id" not in body:
            return None
        return body

    def search(self, track_name: str, artist_name: str) -> list[dict]:
        """Call GET /api/search and return up to 20 lyric records.

        Purpose:
            List alternate recordings when /api/get has no synced lyrics.
            LRCLIB does not paginate this endpoint.

        Parameters:
            track_name (str): Title keyword.
            artist_name (str): Artist keyword.

        Returns:
            list[dict]: Matching records. Empty when nothing matches.

        Precondition:
            At least one of track_name or artist_name is non-empty.

        Postcondition:
            The list contains only dicts from the JSON array. Plain and synced
            records are both included; the caller filters them.
        """
        status, body = self._get_json(
            "/api/search",
            {"track_name": track_name, "artist_name": artist_name},
        )
        if status == 404 or not isinstance(body, list):
            return []
        return [record for record in body if isinstance(record, dict)]

    def find_synced(
        self,
        track_name: str,
        artist_name: str,
        album_name: str,
        duration_ms: int,
    ) -> LyricMatch | None:
        """Resolve one synced lyric record for a Spotify track.

        Purpose:
            Search by title and artist, include the /api/get record in that
            pool, then let choose_synced_record rank by title, artist, last
            lyric timestamp against Spotify's duration, and album.

        Parameters:
            track_name (str): Spotify item.name.
            artist_name (str): Spotify artists joined with ', '.
            album_name (str): Spotify album name. May be empty.
            duration_ms (int): Spotify item.duration_ms.

        Returns:
            LyricMatch | None: The locked record and its parsed lines, or None
            when no synced record has a matching title.

        Precondition:
            duration_ms is positive. track_name and artist_name are non-empty.

        Postcondition:
            A returned match uses a single LRCLIB id. The last lyric timestamp
            is that record's lyric length. None means the caller should not
            invent a line time.
        """
        print()
        print("LRCLIB search from Spotify:")
        print(f"  track_name: {track_name}")
        print(f"  artist_name: {artist_name}")
        print(f"  album_name: {album_name or '(empty)'}")
        print(f"  Spotify duration: {format_ms(duration_ms)} ({duration_ms} ms)")
        duration_sec = max(1, min(3600, int(round(duration_ms / 1000))))
        signature = self.get_by_signature(track_name, artist_name, album_name, duration_sec)
        search_track = base_title(track_name)
        search_artist = artist_name.split(",")[0].strip()
        print()
        print("Ranking synced records by title, then artist, then last lyric timestamp, then album.")
        print(f"Search track_name: {search_track}")
        print(f"Search artist_name: {search_artist}")
        found = self.search(search_track, search_artist)
        if signature and all(record.get("id") != signature.get("id") for record in found):
            found = [signature, *found]
        _print_search_durations(found, duration_ms)
        chosen = choose_synced_record(found, track_name, artist_name, album_name, duration_ms)
        if chosen is None:
            return None
        record, count, reason = chosen
        source = "get" if signature and record.get("id") == signature.get("id") else "search"
        return _match_from_record(
            record,
            duration_ms,
            source=source,
            candidate_count=count,
            reason=reason,
        )


def _match_from_record(
    record: dict,
    duration_ms: int,
    source: str,
    candidate_count: int,
    reason: str,
) -> LyricMatch | None:
    """Wrap a raw LRCLIB record into a LyricMatch when it has timed text."""
    lines = parse_synced_lyrics(record.get("syncedLyrics") or "")
    lyric_ms = lyric_duration_ms(lines)
    if lyric_ms is None or not any(line.text for line in lines):
        return None
    stored = record.get("duration")
    stored_sec = int(stored) if isinstance(stored, (int, float)) else 0
    return LyricMatch(
        record_id=int(record["id"]),
        track_name=str(record.get("trackName") or record.get("name") or ""),
        artist_name=str(record.get("artistName") or ""),
        album_name=str(record.get("albumName") or ""),
        duration_sec=stored_sec,
        lyric_duration_ms=lyric_ms,
        duration_delta_ms=abs(duration_ms - lyric_ms),
        lines=lines,
        source=source,
        candidate_count=candidate_count,
        reason=reason,
    )


class LyricLookup:
    """Cache one LyricMatch per Spotify track so each keypress does not call LRCLIB.

    The current line is recomputed locally from progress_ms. LRCLIB is called
    again only when the Spotify track id changes.
    """

    def __init__(self, client: LrcLibClient | None = None) -> None:
        """Start with an empty cache.

        Purpose:
            Hold the last match in memory for the running terminal session.

        Parameters:
            client (LrcLibClient | None): Client to use. A new one is created
                when this is omitted.

        Returns:
            None.

        Precondition:
            None.

        Postcondition:
            No network call has been made. The cache is empty.
        """
        self.client = client or LrcLibClient()
        self._key: str | None = None
        self._match: LyricMatch | None = None
        self._miss_key: str | None = None

    def show(self, now: dict | None) -> None:
        """Print the locked LRCLIB match and the line for this progress_ms.

        Purpose:
            Connect the Spotify playback body to one synced line. Reuse the
            cached record while the same track is still playing.

        Parameters:
            now (dict | None): Body of GET /v1/me/player/currently-playing,
                or None when nothing is playing.

        Returns:
            None.

        Precondition:
            now is None or a Spotify currently-playing object.

        Postcondition:
            stdout shows either the current and next lyric lines, or why no
            synced record was used. LRCLIB is not called again for the same
            Spotify track id during this process.
        """
        query = spotify_track_query(now)
        if query is None:
            print("No Spotify track to look up on LRCLIB.")
            return
        if query["key"] == self._key and self._match is not None:
            _print_lines(self._match, query["progress_ms"])
            return
        if query["key"] == self._miss_key:
            print("No synced LRCLIB lyrics for this track (not asking again).")
            return
        match = self.client.find_synced(
            query["track_name"],
            query["artist_name"],
            query["album_name"],
            query["duration_ms"],
        )
        if match is None:
            self._key = None
            self._match = None
            self._miss_key = query["key"]
            print("LRCLIB has no synced lyrics with a matching title.")
            return
        self._match = match
        self._key = query["key"]
        self._miss_key = None
        _print_match(match, query["duration_ms"])
        _print_lines(match, query["progress_ms"])


def spotify_track_query(now: dict | None) -> dict | None:
    """Copy the Spotify fields LRCLIB needs from a currently-playing body.

    Purpose:
        Keep the LRCLIB client from reading through the rest of the Spotify JSON.

    Parameters:
        now (dict | None): Playback body, or None.

    Returns:
        dict | None: track_name, artist_name, album_name, duration_ms,
        progress_ms, and key. None when there is no catalog track.

    Precondition:
        now is None or a dict.

    Postcondition:
        now is unchanged. key is stable for the same Spotify track id.
    """
    if not now:
        return None
    item = now.get("item") or {}
    if item.get("type") != "track":
        return None
    artists = [artist.get("name", "") for artist in item.get("artists", []) if artist.get("name")]
    track_name = item.get("name") or ""
    duration_ms = item.get("duration_ms")
    if not track_name or not artists or not isinstance(duration_ms, int):
        return None
    spotify_id = item.get("id") or f"{track_name}|{artists[0]}|{duration_ms}"
    progress = now.get("progress_ms")
    return {
        "key": str(spotify_id),
        "track_name": track_name,
        "artist_name": ", ".join(artists),
        "album_name": (item.get("album") or {}).get("name") or "",
        "duration_ms": duration_ms,
        "progress_ms": progress if isinstance(progress, int) else 0,
    }


def _print_lrcib_request(path: str, params: dict[str, str | int]) -> None:
    """Print the LRCLIB path and query fields before a GET. The URL is not printed."""
    print()
    print(f"LRCLIB GET {path}")
    if params:
        for key, value in params.items():
            print(f"  {key}: {value}")


def _print_search_durations(records: list[dict], spotify_duration_ms: int) -> None:
    """Print each record's stored duration and the last lyric timestamp."""
    print(f"LRCLIB returned {len(records)} record(s). Spotify duration is {format_ms(spotify_duration_ms)}:")
    if not records:
        return
    for record in records:
        stored = record.get("duration")
        stored_shown = f"{int(stored)}s" if isinstance(stored, (int, float)) else "(missing)"
        lines = parse_synced_lyrics(record.get("syncedLyrics") or "")
        lyric_ms = lyric_duration_ms(lines)
        lyric_shown = format_ms(lyric_ms) if lyric_ms is not None else "(no synced lines)"
        if record.get("instrumental"):
            kind = "instrumental"
        elif lyric_ms is None:
            kind = "no synced lyrics"
        else:
            kind = "synced"
        name = record.get("trackName") or record.get("name") or ""
        artist = record.get("artistName") or ""
        print(
            f"  id {record.get('id')}: stored duration {stored_shown}, "
            f"last lyric {lyric_shown}, {kind}, {name} — {artist}"
        )


def _print_match(match: LyricMatch, duration_ms: int) -> None:
    """Print which LRCLIB record was locked and why."""
    print()
    print(
        f"LRCLIB {match.source} id {match.record_id}: "
        f"{match.track_name} — {match.artist_name} — {match.album_name}"
    )
    print(f"LRCLIB stored duration: {match.duration_sec}s (not used to choose)")
    print(f"Last lyric timestamp: {format_ms(match.lyric_duration_ms)}")
    print(f"Spotify duration: {format_ms(duration_ms)}")
    print(
        f"Last lyric is {format_ms(match.duration_delta_ms)} from Spotify's duration. "
        f"{match.candidate_count} synced candidate(s). {match.reason}."
    )


def _print_lines(match: LyricMatch, progress_ms: int) -> None:
    """Print the current line and the next line for this playback position."""
    current, upcoming = line_at(match.lines, progress_ms)
    if current is None:
        print(f"At {progress_ms} ms: before the first lyric line.")
    else:
        print(f"At {progress_ms} ms: {current.text}")
    if upcoming is not None:
        print(f"Next at {upcoming.time_ms} ms: {upcoming.text}")
