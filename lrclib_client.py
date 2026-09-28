"""Read synced lyrics from LRCLIB and pick the line for a playback position.

LRCLIB does not require an account or an API key. Docs: https://lrclib.net/docs
Every endpoint and its JSON shape is listed in lrclib_api.md.

Spotify gives the song identity and progress_ms. LRCLIB gives lines that
each start at a timestamp. The current line is the last line whose timestamp
is still at or before progress_ms.

Several LRCLIB records can share a title and artist. This client prefers
the same title, artist, and album, then the LRCLIB duration closest to
Spotify. The last lyric timestamp is used only when every stored duration
is outside the threshold. Otherwise it asks once for an LRCLIB id and
remembers that choice for the Spotify track id.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from chinese_script import lrclib_lines, stamp_lines
import lyric_catalog

# Public LRCLIB API. No key is sent.
LRCLIB_API = "https://lrclib.net"
# LRCLIB asks every app to identify itself. There is no account to create.
# https://lrclib.net/docs
USER_AGENT = "SpotifyOverlayLyrics/0.1 (personal local lyrics overlay)"
# One LRC timestamp. [mm:ss.xx] uses decimal seconds, so .12 means 120 milliseconds.
LINE_PATTERN = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
# Ask the user to choose an LRCLIB id when the last lyric timestamp is farther
# than this from Spotify's duration.
DURATION_CHOICE_THRESHOLD_MS = 4000
# connect_spotify.py sets this. False hides search dumps and raw lyric details.
VERBOSE = True
# True while the glass overlay is the place that asks the user to pick an LRCLIB id.
OVERLAY_CHOICES = False


def _debug(*args, **kwargs) -> None:
    """Print a diagnostic line only while VERBOSE is on."""
    if VERBOSE:
        print(*args, **kwargs)
# Spotify track id -> LRCLIB id. Spotify ids are strings, so this is not a
# number-to-number map. The .spotify directory is gitignored.
# JSON cannot store comments, so song name, artist, album, and duration live
# in the spreadsheet beside this file.


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
class LyricWindow:
    """The line being sung and the next line, each with its LRCLIB timestamp.

    Attributes:
        singing (LyricLine): The line to show as current. Before the first
            timestamp this is still the first lyric.
        next_line (LyricLine | None): The following line. None on the last lyric.
            Its timestamp is never equal to singing.time_ms.
    """

    singing: LyricLine
    next_line: LyricLine | None


@dataclass(frozen=True)
class LyricCandidate:
    """One synced LRCLIB record the user can preview and choose.

    Attributes:
        record_id (int): LRCLIB id.
        label (str): Title, artist, album, and duration, for the overlay list.
        url (str): Public track page, opened in the browser for a preview.
    """

    record_id: int
    label: str
    url: str


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
    """Compare a title, artist, or album across capitalization and punctuation.

    Purpose:
        Keep letters from every language. Chinese, Japanese, and other
        non-English names stay intact. English case is folded.

    Parameters:
        value (str): Raw name.

    Returns:
        str: Letters and digits separated by single spaces. Empty when the
        name had no letters or digits.

    Precondition:
        value is a string.

    Postcondition:
        Punctuation is removed. Unicode letters and digits are preserved.
    """
    kept = []
    for char in value.casefold():
        if char.isalnum():
            kept.append(char)
        else:
            kept.append(" ")
    return " ".join("".join(kept).split())


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


def lyric_window(lines: tuple[LyricLine, ...], progress_ms: int) -> LyricWindow | None:
    """Choose the singing line and the next line for this playback position.

    Purpose:
        Keep one pair of LRCLIB timestamps. From 0 ms until the second line's
        timestamp, the singing line is the first lyric and the next line is
        the second. When playback reaches the next line's timestamp, that line
        becomes the singing line and the line after it becomes the next line.
        The last lyric has no next line.

    Parameters:
        lines (tuple[LyricLine, ...]): Sorted synced lines from one record.
        progress_ms (int): Playback position in milliseconds.

    Returns:
        LyricWindow | None: The singing line and the next line. None when
        every line is blank.

    Precondition:
        lines are sorted by time_ms. progress_ms is >= 0.

    Postcondition:
        next_line is None or its timestamp is greater than singing.time_ms.
        Entries that share a timestamp collapse to one line, so the two slots
        never hold the same timestamp. Playback before the first timestamp
        still returns the first lyric as the singing line.
    """
    visible: list[LyricLine] = []
    for line in lines:
        if not line.text:
            continue
        if visible and visible[-1].time_ms == line.time_ms:
            visible[-1] = line
            continue
        visible.append(line)
    if not visible:
        return None
    index = 0
    for cursor in range(len(visible) - 1):
        if progress_ms < visible[cursor + 1].time_ms:
            break
        index = cursor + 1
    singing = visible[index]
    next_line = visible[index + 1] if index + 1 < len(visible) else None
    return LyricWindow(singing=singing, next_line=next_line)


def lyric_slots(lines: tuple[LyricLine, ...], progress_ms: int) -> tuple[str, str, str] | None:
    """Place the current line and the following line so the current line stays put.

    Purpose:
        Even lyric indexes stay on the left. Odd indexes stay on the right.
        The other side shows the line that comes next. When playback reaches
        lyric 11, line 2 stays lyric 11 and becomes the highlighted line, and
        line 1 changes from lyric 10 to lyric 12.

    Parameters:
        lines (tuple[LyricLine, ...]): Sorted synced lines from one record.
        progress_ms (int): Playback position in milliseconds.

    Returns:
        tuple[str, str, str] | None: Left text, right text, and "left" or
        "right" for the side that is being sung. None when every line is blank.

    Precondition:
        lines are sorted by time_ms. progress_ms is >= 0.

    Postcondition:
        The highlighted side is the line whose timestamp the playback has
        reached. The other side is the following line, or empty on the last line.
    """
    visible: list[LyricLine] = []
    for line in lines:
        if not line.text:
            continue
        if visible and visible[-1].time_ms == line.time_ms:
            visible[-1] = line
            continue
        visible.append(line)
    if not visible:
        return None
    index = 0
    for cursor in range(len(visible) - 1):
        if progress_ms < visible[cursor + 1].time_ms:
            break
        index = cursor + 1
    following = visible[index + 1].text if index + 1 < len(visible) else ""
    if index % 2 == 0:
        return visible[index].text, following, "left"
    return following, visible[index].text, "right"


def same_lyric_timestamps(
    singing: LyricLine | None,
    next_line: LyricLine | None,
    window: LyricWindow,
) -> bool:
    """Return whether this window is the pair already saved.

    Purpose:
        Treat a reprint as the same singing timestamp and the same next
        timestamp. The same words later in the song have a new timestamp, so
        they are a new pair. The last lyric matches only when both next lines
        are absent.

    Parameters:
        singing (LyricLine | None): The saved singing line.
        next_line (LyricLine | None): The saved next line. None at the last lyric.
        window (LyricWindow): The pair for the current playback position.

    Returns:
        bool: True when both timestamps match the saved pair.

    Precondition:
        window.next_line is None or its timestamp differs from window.singing.

    Postcondition:
        The saved lines and the window are unchanged.
    """
    if singing is None or singing.time_ms != window.singing.time_ms:
        return False
    if window.next_line is None:
        return next_line is None
    if next_line is None:
        return False
    return next_line.time_ms == window.next_line.time_ms


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


def lrclib_track_url(record_id: int) -> str:
    """Return the LRCLIB website page for one lyrics record."""
    return f"https://lrclib.net/tracks/{record_id}"


def format_ms(duration_ms: int) -> str:
    """Format milliseconds as m:ss.xxx."""
    minutes, remainder = divmod(abs(duration_ms), 60000)
    seconds = remainder / 1000
    return f"{minutes}:{seconds:06.3f}"


def format_clock(duration_ms: int) -> str:
    """Format milliseconds as m:ss, rounded to the nearest second."""
    total_seconds = max(0, int(round(duration_ms / 1000)))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _title_rank(record: dict, track_name: str) -> int | None:
    """Return 0 for the same title, 1 for the title without a ' - ' suffix, or None."""
    record_title = normalize_name(str(record.get("trackName") or record.get("name") or ""))
    wanted_title = normalize_name(track_name)
    wanted_base = normalize_name(base_title(track_name))
    if not record_title or not wanted_title:
        return None
    if record_title == wanted_title:
        return 0
    if wanted_base and record_title == wanted_base:
        return 1
    return None


def _artist_rank(record: dict, artist_name: str) -> int | None:
    """Return 0 for the same artist, 1 when one name contains the other, or None."""
    record_artist = normalize_name(str(record.get("artistName") or ""))
    wanted_artist = normalize_name(artist_name)
    first_artist = normalize_name(artist_name.split(",")[0])
    if not record_artist or not (wanted_artist or first_artist):
        return None
    if wanted_artist and record_artist == wanted_artist:
        return 0
    if first_artist and (first_artist in record_artist or record_artist in first_artist):
        return 1
    return None


def _album_rank(record: dict, album_name: str) -> int:
    """Return 0 for the same album, 1 when one name contains the other, otherwise 2."""
    record_album = normalize_name(str(record.get("albumName") or ""))
    wanted_album = normalize_name(album_name)
    if not record_album or not wanted_album:
        return 2
    if record_album == wanted_album:
        return 0
    if wanted_album in record_album or record_album in wanted_album:
        return 1
    return 2


def _stored_duration_delta_ms(record: dict, duration_ms: int) -> int | None:
    """Return the gap between LRCLIB's stored duration and Spotify, in milliseconds."""
    stored = record.get("duration")
    if not isinstance(stored, (int, float)):
        return None
    return abs(int(stored) * 1000 - duration_ms)


def _synced_lines(record: dict) -> tuple[LyricLine, ...] | None:
    """Return timed lyric lines when the record has synced text."""
    if record.get("instrumental"):
        return None
    lines = parse_synced_lyrics(record.get("syncedLyrics") or "")
    if not lines or not any(line.text for line in lines):
        return None
    return lines


def choose_synced_record(
    records: list[dict],
    track_name: str,
    artist_name: str,
    album_name: str,
    duration_ms: int,
) -> tuple[dict, int, str] | None:
    """Pick one synced record from the LRCLIB results.

    Purpose:
        Keep synced records with the same title and artist. Prefer the same
        album, then the LRCLIB stored duration closest to Spotify. When every
        stored duration is outside the threshold, use the last lyric timestamp
        the same way.

    Parameters:
        records (list[dict]): JSON objects from GET /api/search and /api/get.
        track_name (str): Spotify track name.
        artist_name (str): Spotify artists joined with ', '.
        album_name (str): Spotify album name.
        duration_ms (int): Spotify item.duration_ms.

    Returns:
        tuple[dict, int, str] | None: The winning record, how many title and
        artist matches were considered, and a short reason. None when none of
        those matches are within the duration threshold.

    Precondition:
        Each record is a dict. duration_ms is positive.

    Postcondition:
        The returned record, when present, matches the title and artist, and
        either its stored duration or its last lyric timestamp is within
        DURATION_CHOICE_THRESHOLD_MS of Spotify. records is not modified.
    """
    pool: list[tuple[int, int, int, dict, tuple[LyricLine, ...]]] = []
    for record in records:
        lines = _synced_lines(record)
        title_rank = _title_rank(record, track_name)
        artist_rank = _artist_rank(record, artist_name)
        if lines is None or title_rank is None or artist_rank is None:
            continue
        pool.append((_album_rank(record, album_name), title_rank, artist_rank, record, lines))
    if not pool:
        return None

    stored_hits: list[tuple[tuple[int, int, int, int, int], dict]] = []
    for album_rank, title_rank, artist_rank, record, _lines in pool:
        delta = _stored_duration_delta_ms(record, duration_ms)
        if delta is None or delta > DURATION_CHOICE_THRESHOLD_MS:
            continue
        stored_hits.append(((title_rank, artist_rank, album_rank, delta, int(record.get("id") or 0)), record))
    if stored_hits:
        stored_hits.sort(key=lambda item: item[0])
        winner = stored_hits[0][1]
        title_rank, artist_rank, album_rank, delta, _record_id = stored_hits[0][0]
        reason = (
            f"title rank {title_rank}, artist rank {artist_rank}, album rank {album_rank}, "
            f"stored duration is {format_ms(delta)} from Spotify"
        )
        return winner, len(pool), reason

    lyric_hits: list[tuple[tuple[int, int, int, int, int], dict]] = []
    for album_rank, title_rank, artist_rank, record, lines in pool:
        lyric_ms = lyric_duration_ms(lines)
        if lyric_ms is None:
            continue
        delta = abs(duration_ms - lyric_ms)
        if delta > DURATION_CHOICE_THRESHOLD_MS:
            continue
        lyric_hits.append(((title_rank, artist_rank, album_rank, delta, int(record.get("id") or 0)), record))
    if not lyric_hits:
        return None
    lyric_hits.sort(key=lambda item: item[0])
    winner = lyric_hits[0][1]
    title_rank, artist_rank, album_rank, delta, _record_id = lyric_hits[0][0]
    reason = (
        f"stored durations were outside {DURATION_CHOICE_THRESHOLD_MS} ms; "
        f"title rank {title_rank}, artist rank {artist_rank}, album rank {album_rank}, "
        f"last lyric is {format_ms(delta)} from Spotify"
    )
    return winner, len(pool), reason


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

    def _get_json(self, path: str, query: dict[str, str | int], *, announce: bool = True) -> tuple[int, object]:
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
        if announce:
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
                if exc.code in (429, 503) and attempt == 1:
                    wait = min(int(exc.headers.get("Retry-After") or "2"), 2)
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

    def get_by_id(self, record_id: int, *, announce: bool = False) -> dict | None:
        """Call GET /api/get/{id} and return that lyrics record.

        Purpose:
            Load the LRCLIB id already chosen for a Spotify track.

        Parameters:
            record_id (int): LRCLIB id.
            announce (bool): Print the request fields when True. Defaults to False.

        Returns:
            dict | None: The lyrics record, or None when LRCLIB returns 404.

        Precondition:
            record_id is a positive integer.

        Postcondition:
            On success, the dict includes id and syncedLyrics when the record has them.
        """
        status, body = self._get_json(f"/api/get/{record_id}", {}, announce=announce)
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
        found = self.candidate_records(track_name, artist_name, album_name, duration_ms)
        chosen = choose_synced_record(found, track_name, artist_name, album_name, duration_ms)
        if chosen is None:
            return None
        record, count, reason = chosen
        return _match_from_record(
            record,
            duration_ms,
            source="search",
            candidate_count=count,
            reason=reason,
        )

    def candidate_records(
        self,
        track_name: str,
        artist_name: str,
        album_name: str,
        duration_ms: int,
    ) -> list[dict]:
        """Search LRCLIB and return the records the user or ranker can choose from.

        Purpose:
            Load /api/get and /api/search once for a Spotify track. The caller
            ranks them or asks the user for an id.

        Parameters:
            track_name (str): Spotify track name.
            artist_name (str): Spotify artists joined with ', '.
            album_name (str): Spotify album name.
            duration_ms (int): Spotify item.duration_ms.

        Returns:
            list[dict]: Combined lyric records. May be empty.

        Precondition:
            track_name and artist_name are non-empty.

        Postcondition:
            Each /api/get record not already in the search list is included.
            The console lists each record's stored duration and last lyric time.
        """
        _debug()
        _debug("LRCLIB search from Spotify:")
        _debug(f"  track_name: {track_name}")
        _debug(f"  artist_name: {artist_name}")
        _debug(f"  album_name: {album_name or '(empty)'}")
        _debug(f"  Spotify duration: {format_ms(duration_ms)} ({duration_ms} ms)")
        duration_sec = max(1, min(3600, int(round(duration_ms / 1000))))
        signature = self.get_by_signature(track_name, artist_name, album_name, duration_sec)
        search_track = base_title(track_name)
        search_artist = artist_name.split(",")[0].strip()
        _debug()
        _debug("Ranking synced records by title, artist, album, then stored duration.")
        _debug(f"Search track_name: {search_track}")
        _debug(f"Search artist_name: {search_artist}")
        found = self.search(search_track, search_artist)
        if signature and all(record.get("id") != signature.get("id") for record in found):
            found = [signature, *found]
        _print_search_durations(found, track_name, artist_name, album_name, duration_ms)
        return found


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


def _match_from_rows(query: dict, rows: list[dict], reason: str) -> LyricMatch | None:
    """Build a line-sync match from catalog rows. record_id is a stable hash of the song key."""
    lines = tuple(
        LyricLine(int(row.get("start_ms") or 0), str(row.get("text") or ""))
        for row in rows
        if isinstance(row, dict) and str(row.get("text") or "")
    )
    lyric_ms = lyric_duration_ms(lines)
    if lyric_ms is None:
        return None
    digest = int(hashlib.sha256(lyric_catalog.song_key_of(query).encode("utf-8")).hexdigest()[:8], 16)
    duration_ms = int(query.get("duration_ms") or 0)
    return LyricMatch(
        record_id=digest,
        track_name=str(query.get("track_name") or ""),
        artist_name=str(query.get("artist_name") or ""),
        album_name=str(query.get("album_name") or ""),
        duration_sec=duration_ms // 1000,
        lyric_duration_ms=lyric_ms,
        duration_delta_ms=abs(duration_ms - lyric_ms),
        lines=lines,
        source="catalog",
        candidate_count=1,
        reason=reason,
    )


def _line_rows(match: LyricMatch) -> list[dict]:
    """Copy timed lines into catalog rows."""
    rows = [{"start_ms": line.time_ms, "text": line.text} for line in match.lines if line.text]
    if rows:
        stamp_lines(rows)
    return rows


def synced_choice_rows(records: list[dict], duration_ms: int) -> list[tuple[LyricCandidate, dict]]:
    """Return synced LRCLIB records closest to the Spotify duration.

    Purpose:
        Build the list shown when automatic matching cannot pick one record.
        The overlay uses the first five. The full lyrics stay on the record
        dict in memory and are not written to disk.

    Parameters:
        records (list[dict]): Records from /api/search and /api/get.
        duration_ms (int): Spotify item.duration_ms.

    Returns:
        list[tuple[LyricCandidate, dict]]: Candidates and the raw records,
        closest duration first.

    Precondition:
        duration_ms is positive.

    Postcondition:
        Each candidate url is the public LRCLIB track page. records is unchanged.
    """
    ranked: list[tuple[int, dict, int]] = []
    for record in records:
        if record.get("instrumental"):
            continue
        lines = parse_synced_lyrics(record.get("syncedLyrics") or "")
        lyric_ms = lyric_duration_ms(lines)
        if lyric_ms is None or not any(line.text for line in lines):
            continue
        ranked.append((int(record["id"]), record, lyric_ms))
    ranked.sort(key=lambda item: _stored_duration_delta_ms(item[1], duration_ms) or abs(duration_ms - item[2]))
    rows: list[tuple[LyricCandidate, dict]] = []
    for record_id, record, lyric_ms in ranked:
        name = str(record.get("trackName") or record.get("name") or "")
        artist = str(record.get("artistName") or "")
        album = str(record.get("albumName") or "")
        stored = record.get("duration")
        clock_ms = int(stored) * 1000 if isinstance(stored, (int, float)) else lyric_ms
        label = f"{name} — {artist} · {album} · {format_clock(clock_ms)}"
        rows.append((LyricCandidate(record_id, label, lrclib_track_url(record_id)), record))
    return rows


def _prompt_lrclib_id(records: list[dict], duration_ms: int) -> int | None:
    """Ask once for an LRCLIB id when automatic duration matching is too far.

    Purpose:
        Show the synced candidates and accept only an id from that list.

    Parameters:
        records (list[dict]): Records returned for this Spotify track.
        duration_ms (int): Spotify item.duration_ms.

    Returns:
        int | None: The id the user entered, or None when no synced candidate exists.

    Precondition:
        stdin can read a line.

    Postcondition:
        The returned id is one of the synced records. The choice file is not
        written here.
    """
    choices: list[tuple[int, dict, int]] = []
    for record in records:
        if record.get("instrumental"):
            continue
        lines = parse_synced_lyrics(record.get("syncedLyrics") or "")
        lyric_ms = lyric_duration_ms(lines)
        if lyric_ms is None or not any(line.text for line in lines):
            continue
        choices.append((int(record["id"]), record, lyric_ms))
    if not choices:
        return None
    if sys.stdin is None or not sys.stdin.isatty():
        return None
    choices.sort(key=lambda item: _stored_duration_delta_ms(item[1], duration_ms) or abs(duration_ms - item[2]))
    print()
    print(f"The closest stored duration and last lyric are more than {DURATION_CHOICE_THRESHOLD_MS} ms from Spotify.")
    print("Enter one LRCLIB id. The same Spotify track will reuse it.")
    for record_id, record, lyric_ms in choices:
        name = record.get("trackName") or record.get("name") or ""
        artist = record.get("artistName") or ""
        album = record.get("albumName") or ""
        print(
            f"  id {record_id}: last lyric {format_ms(lyric_ms)}, "
            f"{format_ms(abs(duration_ms - lyric_ms))} from Spotify, "
            f"{name} — {artist} — {album}"
        )
        print(f"    {lrclib_track_url(record_id)}")
    allowed = {record_id for record_id, _record, _lyric_ms in choices}
    while True:
        raw = input("LRCLIB id: ").strip()
        if raw.isdigit() and int(raw) in allowed:
            return int(raw)
        print("Enter one of the ids listed above.")


def media_choice_key(track_name: str, artist_name: str, album_name: str) -> str:
    """Return the map key for a song identified by title, artist, and album.

    Purpose:
        Windows media sessions have no Spotify track id. Those three strings
        are what an LRCLIB search uses, so the same triple should reuse one
        LRCLIB id.

    Parameters:
        track_name (str): Song title.
        artist_name (str): Artist string from the media session.
        album_name (str): Album title. An empty album is part of the key.

    Returns:
        str: A single key that stays stable for the same three strings.

    Precondition:
        The three arguments are strings.

    Postcondition:
        Equal names produce an equal key. No file is written.
    """
    return f"{track_name}\u001f{artist_name}\u001f{album_name}"


class LyricLookup:
    """Cache one LyricMatch per Spotify track so each keypress does not call LRCLIB.

    The current line is recomputed locally from progress_ms. LRCLIB is called
    again only when the Spotify track id changes. singing_line and next_line
    keep the pair now on screen, including each line's LRCLIB timestamp.
    Saved lyrics are read from lyric_catalog before LRCLIB is searched again.
    """

    def __init__(self, client: LrcLibClient | None = None) -> None:
        """Start with an empty session cache.

        Purpose:
            Hold the last match in memory.

        Parameters:
            client (LrcLibClient | None): Client to use. A new one is created
                when this is omitted.

        Returns:
            None.

        Precondition:
            None.

        Postcondition:
            No LRCLIB request has been made. The session cache is empty.
        """
        self.client = client or LrcLibClient()
        self._key: str | None = None
        self._match: LyricMatch | None = None
        self._miss_key: str | None = None
        self._pending_key: str | None = None
        self._pending: list[LyricCandidate] = []
        self._pending_records: dict[int, dict] = {}
        self._pending_query: dict | None = None
        self._busy_key: str | None = None
        self._busy_until = 0.0
        self.singing_line: LyricLine | None = None
        self.next_line: LyricLine | None = None
        self._shown_track: str | None = None
        self._pair_on_screen = False
        self._status: tuple[str | None, str] | None = None

    def needs_lookup(self, now: dict | None) -> bool:
        """Return whether this playback still needs an LRCLIB record chosen.

        Purpose:
            Let the terminal ask for an LRCLIB id before the live lyric line starts.

        Parameters:
            now (dict | None): Spotify currently-playing body, or None.

        Returns:
            bool: True when a new Spotify track has no locked record yet.

        Precondition:
            now is None or a currently-playing object.

        Postcondition:
            No network call is made and no record is locked.
        """
        query = spotify_track_query(now)
        if query is None:
            return False
        if query["key"] == self._key and self._match is not None:
            return False
        if query["key"] == self._miss_key:
            return False
        return True

    def current_line(self, now: dict | None, progress_ms: int) -> str:
        """Return the lyric line for a playback position without printing diagnostics.

        Purpose:
            Reuse the locked LRCLIB record and read the line at progress_ms.
            LRCLIB is contacted only when the Spotify track id changes.

        Parameters:
            now (dict | None): Spotify currently-playing body, or None.
            progress_ms (int): Playback position to display, in milliseconds.

        Returns:
            str: The current non-empty lyric line, or an empty string when
            playback is before the first line or no synced record is locked.

        Precondition:
            progress_ms is >= 0.

        Postcondition:
            The locked record is unchanged when the Spotify track id is unchanged.
        """
        self._ensure(now)
        if self._match is None:
            return ""
        current, _upcoming = line_at(self._match.lines, progress_ms)
        if current is None:
            return ""
        return current.text

    def window_at(self, now: dict | None, progress_ms: int) -> LyricWindow | None:
        """Return the singing line and the next line for this playback position.

        Purpose:
            Lock the LRCLIB record when the Spotify track changes, then return
            both lines with their timestamps. From 0 ms until the second line's
            timestamp, the singing line is the first lyric.

        Parameters:
            now (dict | None): Spotify currently-playing body, or None.
            progress_ms (int): Playback position in milliseconds.

        Returns:
            LyricWindow | None: The pair to show. None when no synced record
            is locked. The last lyric has next_line None.

        Precondition:
            progress_ms is >= 0.

        Postcondition:
            singing_line and next_line are unchanged. The locked record is
            unchanged when the Spotify track id is unchanged.
        """
        self._ensure(now)
        if self._match is None:
            return None
        return lyric_window(self._match.lines, progress_ms)

    def slots_at(self, now: dict | None, progress_ms: int, script: str = "original") -> tuple[str, str, str] | None:
        """Return the left line, the right line, and which side is current.

        Purpose:
            Feed the overlay's two-line layout. The current line keeps its
            side instead of both lines jumping up.

        Parameters:
            now (dict | None): Spotify currently-playing body, or None.
            progress_ms (int): Playback position in milliseconds.
            script (str): original, simplified, or traditional. Stored copies
                are used. Japanese and other non-Chinese characters stay.

        Returns:
            tuple[str, str, str] | None: Left text, right text, and "left" or
            "right". None when no synced record is locked.

        Precondition:
            progress_ms is >= 0.

        Postcondition:
            The locked record is unchanged when the Spotify track id is unchanged.
        """
        self._ensure(now)
        if self._match is None:
            return None
        lines = self._match.lines
        converted = lrclib_lines(self._match.record_id, tuple(line.text for line in lines), script)
        if converted is not None:
            lines = tuple(LyricLine(line.time_ms, text) for line, text in zip(lines, converted))
        return lyric_slots(lines, progress_ms)

    def pair_is_showing(self, track_id: str | None, window: LyricWindow) -> bool:
        """Return whether the terminal already printed this timestamp pair.

        Purpose:
            Skip a print when the singing timestamp and the next timestamp are
            the ones already saved. A later repeat of the same words has a
            different timestamp, so it is not skipped.

        Parameters:
            track_id (str | None): Spotify track id for this playback.
            window (LyricWindow): The pair for the current position.

        Returns:
            bool: True when this pair is already the saved one and was the
            last thing printed.

        Precondition:
            window is the pair for track_id.

        Postcondition:
            singing_line and next_line are unchanged.
        """
        if not self._pair_on_screen or track_id != self._shown_track:
            return False
        return same_lyric_timestamps(self.singing_line, self.next_line, window)

    def remember_pair(self, track_id: str | None, window: LyricWindow) -> None:
        """Save the singing line and the next line after they are printed.

        Purpose:
            Remember both LRCLIB timestamps for the pair now on screen. The
            last lyric is saved with next_line None.

        Parameters:
            track_id (str | None): Spotify track id for this playback.
            window (LyricWindow): The pair that was just printed.

        Returns:
            None.

        Precondition:
            window is the pair for track_id.

        Postcondition:
            singing_line is window.singing. next_line is window.next_line.
            pair_is_showing(track_id, window) is True.
        """
        self._shown_track = track_id
        self.singing_line = window.singing
        self.next_line = window.next_line
        self._pair_on_screen = True
        self._status = None

    def status_is_showing(self, message: str, track_id: str | None = None) -> bool:
        """Return whether this status line is already the last print."""
        return not self._pair_on_screen and self._status == (track_id, message)

    def note_status(self, message: str, track_id: str | None = None) -> None:
        """Remember a status print without clearing the saved lyric pair."""
        self._status = (track_id, message)
        self._pair_on_screen = False

    def _ensure(self, now: dict | None) -> None:
        """Lock an LRCLIB record for the Spotify track in now, if one can be chosen."""
        query = spotify_track_query(now)
        if query is None:
            self._key = None
            self._match = None
            self._clear_pending()
            return
        if query["key"] == self._key and self._match is not None:
            return
        if query["key"] == self._pending_key and self._pending:
            return
        if query["key"] == self._busy_key and time.monotonic() < self._busy_until:
            return
        if query["key"] == self._miss_key:
            return
        if self._pending_key and self._pending_key != query["key"]:
            self._clear_pending()
        try:
            match = self._resolve(query)
        except LrcLibError:
            self._clear_pending()
            self._key = None
            self._match = None
            self._busy_key = query["key"]
            self._busy_until = time.monotonic() + 20
            return
        if self._pending_key == query["key"]:
            self._key = None
            self._match = None
            self._miss_key = None
            return
        if match is None:
            self._key = None
            self._match = None
            if lyric_catalog.sync_of(query) == 1:
                self._miss_key = None
            else:
                self._miss_key = query["key"]
            return
        self._match = match
        self._key = query["key"]
        self._miss_key = None
        self._busy_key = None

    def pending_choices(self, track_id: str | None) -> list[LyricCandidate] | None:
        """Return the overlay choices for this Spotify track, if a pick is waiting."""
        if track_id != self._pending_key or not self._pending:
            return None
        return list(self._pending)

    def has_match(self, track_id: str | None) -> bool:
        """Return whether a synced line lyric is already locked for this song."""
        return track_id is not None and track_id == self._key and self._match is not None

    def lacks_synced_lyrics(self, track_id: str | None) -> bool:
        """Return whether this Spotify track was already found to have no synced lyrics.

        Purpose:
            Let the overlay ask Spotify less often when line timing cannot change.

        Parameters:
            track_id (str | None): Spotify track id, or None when nothing is playing.

        Returns:
            bool: True when this id was looked up and no synced record was locked.

        Precondition:
            track_id is None or a Spotify track id.

        Postcondition:
            No network call is made.
        """
        return track_id is not None and track_id == self._miss_key

    def is_busy(self, track_id: str | None) -> bool:
        """Return whether LRCLIB asked us to wait before looking up this track again."""
        return track_id == self._busy_key and time.monotonic() < self._busy_until

    def accept_candidate(self, record_id: int) -> None:
        """Lock the LRCLIB record the user chose in the overlay.

        Purpose:
            Save the Spotify track id to that LRCLIB id and keep the synced
            lines in memory for the rest of this song. The lyric text is not
            written to disk.

        Parameters:
            record_id (int): One of the ids currently offered.

        Returns:
            None.

        Precondition:
            record_id is one of pending_choices.

        Postcondition:
            The lyric is stored in lyric_catalog. pending_choices is empty.
            Later line changes read the stored timestamps and do not call LRCLIB.
        """
        query = self._pending_query
        record = self._pending_records.get(record_id)
        if query is None or record is None:
            return
        match = _match_from_record(
            record,
            query["duration_ms"],
            "chosen",
            1,
            "LRCLIB id chosen in the overlay",
        )
        if match is None:
            return
        lyric_catalog.drop_hold(query)
        lyric_catalog.put_preference(query, 0, _line_rows(match))
        lyric_catalog.announce(query, "LRCLIB", "line sync, your choice")
        self._match = match
        self._key = query["key"]
        self._miss_key = None
        self._busy_key = None
        self._clear_pending()
        self._pair_on_screen = False
        self._status = None

    def _clear_pending(self) -> None:
        """Drop an in-progress candidate list."""
        self._pending_key = None
        self._pending = []
        self._pending_records = {}
        self._pending_query = None

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
            _debug("No Spotify track to look up on LRCLIB.")
            return
        if query["key"] == self._key and self._match is not None:
            _print_lines(self._match, query["progress_ms"])
            return
        if query["key"] == self._miss_key:
            _debug("No synced LRCLIB lyrics for this track (not asking again).")
            return
        match = self._resolve(query)
        if self._pending_key == query["key"]:
            return
        if match is None:
            self._key = None
            self._match = None
            self._miss_key = query["key"]
            _debug("LRCLIB has no synced lyrics with a matching title.")
            return
        self._match = match
        self._key = query["key"]
        self._miss_key = None
        _print_match(match, query["duration_ms"])
        _print_lines(match, query["progress_ms"])

    def _resolve(self, query: dict) -> LyricMatch | None:
        """Use a saved LRCLIB id, auto-pick within 2 seconds, or ask once.

        Purpose:
            Bind this Spotify track id to one lyric record.

        Parameters:
            query (dict): Fields from spotify_track_query, including key,
                track_name, artist_name, album_name, and duration_ms.

        Returns:
            LyricMatch | None: The record to lock, or None when nothing synced matches.

        Precondition:
            query["key"] is the Spotify track id string.

        Postcondition:
            An auto-picked lyric is stored in lyric_catalog. A later call for
            the same song reads that file without searching again.
        """
        cached = lyric_catalog.get(query)
        if cached is not None:
            place = "MongoDB" if cached.pop("source_hit", None) == "mongo" else "local"
            try:
                sync = int(cached.get("sync"))
            except (TypeError, ValueError):
                sync = -1
            if sync < 0 or sync == 1:
                if sync < 0:
                    lyric_catalog.announce(query, place, "no synced lyric")
                return None
            if sync == 0 and isinstance(cached.get("lines"), list):
                choice = ", your choice" if cached.get("preference") else ""
                lyric_catalog.announce(query, place, f"line sync{choice}")
                return _match_from_rows(query, cached["lines"], "catalog line lyric")
        found = self.client.candidate_records(
            query["track_name"],
            query["artist_name"],
            query["album_name"],
            query["duration_ms"],
        )
        chosen = choose_synced_record(
            found,
            query["track_name"],
            query["artist_name"],
            query["album_name"],
            query["duration_ms"],
        )
        if chosen is not None:
            record, count, reason = chosen
            match = _match_from_record(record, query["duration_ms"], "search", count, reason)
            if match is not None:
                _debug(f"Saved LRCLIB id {match.record_id} for this Spotify track.")
                lyric_catalog.drop_hold(query)
                lyric_catalog.put_shared(query, 0, _line_rows(match))
                lyric_catalog.announce(query, "LRCLIB", "line sync")
                return match
        rows = synced_choice_rows(found, query["duration_ms"])
        if not rows:
            return None
        if not OVERLAY_CHOICES and sys.stdin is not None and sys.stdin.isatty():
            picked = _prompt_lrclib_id(found, query["duration_ms"])
            if picked is None:
                return None
        else:
            self._pending_key = query["key"]
            self._pending_query = query
            self._pending = [candidate for candidate, _record in rows[:5]]
            self._pending_records = {candidate.record_id: record for candidate, record in rows[:5]}
            lyric_catalog.announce(query, "LRCLIB", "choose a version")
            return None
        record = next(item for item in found if int(item.get("id") or 0) == picked)
        match = _match_from_record(
            record,
            query["duration_ms"],
            "chosen",
            1,
            "LRCLIB id entered for this Spotify track",
        )
        if match is not None:
            lyric_catalog.drop_hold(query)
            lyric_catalog.put_preference(query, 0, _line_rows(match))
            lyric_catalog.announce(query, "LRCLIB", "line sync, your choice")
        return match


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


def _display_rank(
    record: dict,
    track_name: str,
    artist_name: str,
    album_name: str,
    spotify_duration_ms: int,
) -> tuple[int, int, int, int, int]:
    """Sort the printed list by title, artist, album, then stored-duration gap."""
    title_rank = _title_rank(record, track_name)
    artist_rank = _artist_rank(record, artist_name)
    return (
        title_rank if title_rank is not None else 9,
        artist_rank if artist_rank is not None else 9,
        _album_rank(record, album_name),
        _stored_duration_delta_ms(record, spotify_duration_ms) or 10**12,
        int(record.get("id") or 0),
    )


def _lyric_delta_ms(record: dict, spotify_duration_ms: int) -> int:
    """Gap between Spotify's duration and this record's last lyric timestamp.

    Records with no synced timestamp sort after every timed record.
    """
    lyric_ms = lyric_duration_ms(parse_synced_lyrics(record.get("syncedLyrics") or ""))
    if lyric_ms is None:
        return 10**12
    return abs(spotify_duration_ms - lyric_ms)


def _print_lrcib_request(path: str, params: dict[str, str | int]) -> None:
    """Print the LRCLIB path and query fields before a GET. The URL is not printed."""
    _debug()
    _debug(f"LRCLIB GET {path}")
    if params:
        for key, value in params.items():
            _debug(f"  {key}: {value}")


def _print_search_durations(
    records: list[dict],
    track_name: str,
    artist_name: str,
    album_name: str,
    spotify_duration_ms: int,
) -> None:
    """Print each record's stored duration and the last lyric timestamp."""
    _debug(f"LRCLIB returned {len(records)} record(s). Spotify duration is {format_ms(spotify_duration_ms)}:")
    if not records:
        return
    ordered = sorted(
        records,
        key=lambda record: _display_rank(record, track_name, artist_name, album_name, spotify_duration_ms),
    )
    for record in ordered:
        stored = record.get("duration")
        stored_shown = f"{int(stored)}s" if isinstance(stored, (int, float)) else "(missing)"
        stored_delta = _stored_duration_delta_ms(record, spotify_duration_ms)
        stored_delta_shown = format_ms(stored_delta) if stored_delta is not None else "n/a"
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
        album = record.get("albumName") or ""
        _debug(
            f"  id {record.get('id')}: stored delta {stored_delta_shown}, stored duration {stored_shown}, "
            f"last lyric {lyric_shown}, {kind}, {name} — {artist} — {album}"
        )
        if isinstance(record.get("id"), int):
            _debug(f"    {lrclib_track_url(record['id'])}")


def _print_match(match: LyricMatch, duration_ms: int) -> None:
    """Print which LRCLIB record was locked and why."""
    _debug()
    _debug(
        f"LRCLIB {match.source} id {match.record_id}: "
        f"{match.track_name} — {match.artist_name} — {match.album_name}"
    )
    _debug(lrclib_track_url(match.record_id))
    _debug(f"LRCLIB stored duration: {match.duration_sec}s")
    _debug(f"Last lyric timestamp: {format_ms(match.lyric_duration_ms)}")
    _debug(f"Spotify duration: {format_ms(duration_ms)}")
    _debug(
        f"Last lyric is {format_ms(match.duration_delta_ms)} from Spotify's duration. "
        f"{match.candidate_count} synced candidate(s). {match.reason}."
    )


def _print_lines(match: LyricMatch, progress_ms: int) -> None:
    """Print the current line and the next line for this playback position."""
    current, upcoming = line_at(match.lines, progress_ms)
    if current is None:
        _debug(f"At {progress_ms} ms: before the first lyric line.")
    else:
        _debug(f"At {progress_ms} ms: {current.text}")
    if upcoming is not None:
        _debug(f"Next at {upcoming.time_ms} ms: {upcoming.text}")
