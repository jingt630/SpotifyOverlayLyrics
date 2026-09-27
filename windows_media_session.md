# Windows media session

Windows keeps a media session for each app that is playing audio on this PC. The Spotify desktop app (Microsoft Store id `SpotifyAB.SpotifyMusic_…!Spotify`) publishes one. Reading it does not call the Spotify Web API and does not spend that quota.

It only sees Spotify running on this PC. Playback on a phone or another computer never appears here. Chrome and other apps have their own sessions. Keep the one whose app id starts with `Spotify`.

A session read on this PC while Spotify was playing returned a title, artist, album, track number, playing status, album art, a position that moved forward with the song, and a length. Pause, next, previous, and seek were reported as allowed.

## What you can read

### Which app

| Field | Call | Example from this PC |
| --- | --- | --- |
| App id | `session.source_app_user_model_id` | `SpotifyAB.SpotifyMusic_…!Spotify` |

### Song text

`await session.try_get_media_properties_async()`

| Field | Use |
| --- | --- |
| `title` | Song name. LRCLIB track name. |
| `artist` | One string. Several names are already joined. LRCLIB artist. |
| `album_title` | Album name. |
| `album_artist` | Album artist. Often the same as `artist`. |
| `track_number` | Track number on the album. `0` if the app did not set it. |
| `album_track_count` | Tracks on the album, when the app sets it. |
| `subtitle` | Extra line. Spotify left this empty. |
| `genres` | List of genre strings. Spotify left this empty. |
| `thumbnail` | Album art as an image stream, not a URL. |

There is no Spotify track id, no ISRC, and no Spotify URI.

### Play, pause, and which buttons work

`session.get_playback_info()`

| Field | Use |
| --- | --- |
| `playback_status` | `Playing`, `Paused`, `Stopped`, `Changing`, `Closed`, or `Opened`. |
| `playback_type` | `Music` for a song. |
| `playback_rate` | Speed. Spotify did not fill this while playing. |
| `is_shuffle_active` | Shuffle, when the app reports it. |
| `auto_repeat_mode` | Repeat, when the app reports it. |
| `controls` | Which transport buttons Spotify is allowing right now. |

`controls` flags seen for Spotify: play off, pause on, next on, previous on, seek on. The same object also has stop, fast-forward, rewind, shuffle, and repeat flags.

### Position and length

`session.get_timeline_properties()`

| Field | Use |
| --- | --- |
| `position` | Where playback was at `last_updated_time`. |
| `last_updated_time` | When `position` was sampled. |
| `start_time` | Usually 0. |
| `end_time` | Track length. This is the duration LRCLIB needs. |
| `min_seek_time` | Earliest seek Spotify allows. |
| `max_seek_time` | Latest seek Spotify allows. Usually the same as `end_time`. |

On this PC, `position` and `last_updated_time` both moved with the song (about 292.7 s, then about 294.7 s two seconds later, on a 296.5 s track).

Current position while audio is playing:

```text
position + (now - last_updated_time)
```

While paused, keep `position` and do not add elapsed time. Clamp the result to `end_time`.

### Events

These fire when Spotify changes the session. You do not poll Windows on a timer to notice them.

| Event | When it fires |
| --- | --- |
| `media_properties_changed` | Title, artist, or album changed. This is a new song. |
| `playback_info_changed` | Play, pause, or the enabled buttons changed. |
| `timeline_properties_changed` | Position or length changed. A seek shows up here as a position that does not match the local clock. |

### Commands this session accepts

These go to the Spotify window on this PC. They do not use a Web API scope.

| Call | Action |
| --- | --- |
| `try_toggle_play_pause_async` | Pause or resume |
| `try_play_async` / `try_pause_async` | Play or pause |
| `try_skip_next_async` / `try_skip_previous_async` | Next or previous |
| `try_change_playback_position_async` | Seek |

## How to call it

Install `winsdk` for the Python that runs the overlay. The types are Windows Runtime, projected into Python by that package.

```python
import asyncio
from datetime import datetime, timezone

from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager,
)


def spotify_session(manager):
    for session in manager.get_sessions():
        app_id = session.source_app_user_model_id or ""
        if app_id.startswith("Spotify"):
            return session
    return None


async def read_spotify():
    manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
    session = spotify_session(manager)
    if session is None:
        return None
    props = await session.try_get_media_properties_async()
    info = session.get_playback_info()
    timeline = session.get_timeline_properties()
    controls = info.controls
    return {
        "app_id": session.source_app_user_model_id,
        "title": props.title,
        "artist": props.artist,
        "album": props.album_title,
        "album_artist": props.album_artist,
        "track_number": props.track_number,
        "status": info.playback_status.name,
        "can_pause": controls.is_pause_enabled,
        "can_next": controls.is_next_enabled,
        "can_previous": controls.is_previous_enabled,
        "can_seek": controls.is_playback_position_enabled,
        "position": timeline.position,
        "duration": timeline.end_time,
        "updated_at": timeline.last_updated_time,
        "read_at": datetime.now(timezone.utc),
    }


print(asyncio.run(read_spotify()))
```

Subscribe after you have the session:

```python
def on_track(_session, _args):
    # title / artist / album changed
    ...

def on_playback(_session, _args):
    # play or pause
    ...

def on_timeline(_session, _args):
    # seek, or a fresh position sample
    ...

session.add_media_properties_changed(on_track)
session.add_playback_info_changed(on_playback)
session.add_timeline_properties_changed(on_timeline)
```

`RequestAsync` is the WinRT name behind `request_async`. `TryGetMediaPropertiesAsync` is `try_get_media_properties_async`. `GetPlaybackInfo` and `GetTimelineProperties` are the other two reads.

## Enough for LRCLIB and for jumps?

Yes, for Spotify playing on this PC.

LRCLIB search uses track name, artist, album, and duration. The session has `title`, `artist`, `album_title`, and `end_time`. That is the same set `spotify_track_query` copies off a Web API track, aside from the Spotify id.

Line timing uses a position in the song. Take `position` at `last_updated_time`, then add elapsed time only while `playback_status` is `Playing`. That is the same local clock the overlay already runs between Web API polls.

Jumps are events, not guesses:

- A new song fires `media_properties_changed`. Drop the old lyric lines and search LRCLIB with the new title, artist, album, and `end_time`.
- A seek inside the song fires `timeline_properties_changed` with a new `position`. If that position is far from the local clock, snap to it. A small gap is the same poll lag the overlay already ignores.
- Pause and resume fire `playback_info_changed`. Stop or start the local clock. Do not search LRCLIB again.

The saved lyric map in `.spotify/lyric_choices.json` is keyed by Spotify track id. This session has no track id. The same song can still be remembered by title, artist, and duration (the fallback `spotify_track_query` already uses when an id is missing). Entries already saved under a Spotify id will not match until that song is stored again under the text key.

This does not replace the Web API when the listener is on a phone. For the desktop app on this PC, it is enough to find synced lyrics and to follow skips and seeks.
