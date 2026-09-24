# LRCLIB API

Source: [LRCLIB API documentation](https://lrclib.net/docs). The base URL is `https://lrclib.net`. No account and no API key are required.

This app only calls the two read endpoints marked below. Publish and flag are listed so the full API is in one place. They are not used.

## How every request must identify itself

LRCLIB asks each app to send one of these headers:

| Header | When |
| --- | --- |
| `User-Agent` | Normal clients. This app sends `SpotifyOverlayLyrics/0.1 (personal local lyrics overlay)`. |
| `X-User-Agent` or `Lrclib-Client` | When the platform blocks `User-Agent`, such as browser JavaScript. |

Example from their docs: `LRCGET v0.2.0 (https://github.com/tranxuanthang/lrcget)`.

Send requests one at a time. For a batch of songs, wait 200–500 ms between calls. If you go too fast, the response is HTTP 429:

```json
{
  "code": 429,
  "name": "TooManyRequests",
  "message": "Rate limit exceeded"
}
```

The `Retry-After` header says how many seconds to wait. This app waits once, up to 5 seconds, then retries that same request.

## Shared lyrics record

`GET /api/get`, `GET /api/get/{id}`, and each item in `GET /api/search` use this object.

```json
{
  "id": 3396226,
  "name": "string",
  "trackName": "string",
  "artistName": "string",
  "albumName": "string",
  "duration": 233,
  "instrumental": false,
  "plainLyrics": "string",
  "syncedLyrics": "[00:17.12] line text\n[03:20.31] next line\n",
  "lyricsfile": "version: '1.0'\nmetadata:\n  title: ...\n"
}
```

| Field | Meaning |
| --- | --- |
| `id` | Stable LRCLIB id for this record. |
| `name` | Alias of `trackName`. |
| `trackName` | Title stored with the lyrics. |
| `artistName` | Artist stored with the lyrics. |
| `albumName` | Album stored with the lyrics. |
| `duration` | Length of that recording in whole seconds. `/api/get` only matches a request within ±2 seconds of this value. |
| `instrumental` | `true` when the track was published with no lyric text. |
| `plainLyrics` | Lyrics with no timestamps. Can be empty. |
| `syncedLyrics` | LRC text. Each line is `[mm:ss.xx] words`. `.12` means 120 milliseconds. This is the field this app parses into line times. |
| `lyricsfile` | Raw Lyricsfile YAML. Always present. If you publish with this field set, LRCLIB ignores `plainLyrics` and `syncedLyrics` in that same request. |

A failed lookup uses a small error object instead:

```json
{
  "code": 404,
  "name": "TrackNotFound",
  "message": "Failed to find specified track"
}
```

## Endpoints

### GET `/api/get` — best match for one recording

**Used by this app** (`LrcLibClient.get_by_signature`).

Find the published lyrics for a track signature. Title and artist are required. Album and duration are optional, and both make the match more precise. Duration must be from 1 to 3600 seconds. LRCLIB returns a record only when its stored duration is within ±2 seconds of the duration you send.

| Query | Required | Type | Meaning |
| --- | --- | --- | --- |
| `track_name` | yes | string | Title |
| `artist_name` | yes | string | Artist |
| `album_name` | no | string | Album |
| `duration` | no | number | Seconds |

Example from the docs:

```text
GET /api/get?artist_name=Borislav+Slavov&track_name=I+Want+to+Live&album_name=Baldur%27s+Gate+3+(Original+Game+Soundtrack)&duration=233
```

- **200:** one lyrics record.
- **404:** `TrackNotFound`. A later request can succeed if their background fetch fills the track in.

### GET `/api/get/{id}` — one record by id

Not called yet. Use this when you already have an `id` from `/api/get` or `/api/search` and want that exact record again.

| URL field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `id` | yes | number | LRCLIB id, for example `3396226` |

```text
GET /api/get/3396226
```

The body is the same lyrics record as `/api/get`.

### GET `/api/search` — list of possible records

**Used by this app** only when `/api/get` has no synced lyrics (`LrcLibClient.search`).

Returns an array of lyrics records. At least one of `q` or `track_name` is required. If neither is sent, the array is empty. The list is capped at 20 items and has no page parameter.

| Query | Required | Type | Meaning |
| --- | --- | --- | --- |
| `q` | one of `q` or `track_name` | string | Keyword matched against title, artist, or album. If set, it overrides the structured fields. |
| `track_name` | one of `q` or `track_name` | string | Keyword in the title |
| `artist_name` | no | string | Keyword in the artist |
| `album_name` | no | string | Keyword in the album |

```text
GET /api/search?q=still+alive+portal
GET /api/search?track_name=22&artist_name=taylor+swift
```

Response: a JSON array of the shared lyrics record. This app keeps synced records with a matching title, then ranks them by artist, then by how close the last lyric timestamp is to Spotify's duration, then by album. LRCLIB's stored `duration` field is printed and is not used to choose the record.

### POST `/api/publish` — add lyrics

Not used. Experimental. Anonymous. Needs a one-time publish token.

Body:

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `trackName` | yes | string | Title |
| `artistName` | yes | string | Artist |
| `albumName` | yes | string | Album |
| `duration` | yes | number | Seconds |
| `plainLyrics` | no | string | Untimed lyrics |
| `syncedLyrics` | no | string | LRC text |
| `lyricsfile` | no | string | Lyricsfile YAML. When set, the other two lyric fields are ignored. |

If all three lyric fields are empty, the track is stored as instrumental. Publishing again keeps older revisions.

Header: `X-Publish-Token: {prefix}:{nonce}`.

- **201:** created.
- **400:** `IncorrectPublishTokenError`.

### POST `/api/request-challenge` — get a publish-token puzzle

Not used. Experimental. Returns a `prefix` and a `target`. You solve a proof-of-work puzzle for a `nonce`. The token is `{prefix}:{nonce}`, and each token works once. A challenge expires after 5 minutes.

```json
{
  "prefix": "string",
  "target": "000000FF00000000000000000000000000000000000000000000000000000000"
}
```

LRCLIB points implementers at the LRCGET source for the puzzle. This app does not request a challenge.

### POST `/api/flag` — report the current lyrics

Not used. Records a report against the lyrics currently published for a track (wrong words, wrong metadata, or a copyright problem). Uses the same one-time `X-Publish-Token` header as publish.

```json
{
  "trackId": 3396226,
  "content": "The lyrics don't match the audio"
}
```

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `trackId` | yes | number | LRCLIB id |
| `content` | no | string | Why you are flagging it |

- **201:** created.
- **400:** `IncorrectPublishTokenError`.

## What this app calls

| Need | Endpoint | Function |
| --- | --- | --- |
| One best record for the Spotify title, artist, album, and duration | `GET /api/get` | `get_by_signature` |
| Other synced copies when that record has no `syncedLyrics` | `GET /api/search` | `search`, then `choose_synced_record` |
| The line at the current playback position | none | `line_at` on the locked `syncedLyrics` and Spotify `progress_ms` |
