# Spotify response formats

Shapes below are the general JSON Spotify returns. Values are types, not a real account. `string | null` means the field can be missing or JSON `null`. Spotify often adds extra fields; the ones listed here are the ones that identify the account, the song, and playback.

This file covers every call `connect_spotify.py` makes, then the other reads the current scopes allow but the script does not call yet.

Every Web API call sends this header:

```http
Authorization: Bearer <access_token>
```

## Shared objects

These objects are nested inside several responses.

### Image

```json
{
  "url": "string",
  "height": "integer | null",
  "width": "integer | null"
}
```

`url` is the picture. Profile photos and album covers use this. Several sizes are returned, widest first.

### Simplified artist

```json
{
  "name": "string",
  "id": "string",
  "uri": "string",
  "href": "string",
  "type": "artist",
  "external_urls": { "spotify": "string" }
}
```

`name` is the artist name shown on screen. `id` is the stable Spotify id.

### Simplified album

```json
{
  "name": "string",
  "id": "string",
  "album_type": "album | single | compilation",
  "total_tracks": "integer",
  "release_date": "string",
  "release_date_precision": "year | month | day",
  "images": ["Image"],
  "artists": ["Simplified artist"],
  "uri": "string",
  "href": "string",
  "type": "album",
  "external_urls": { "spotify": "string" }
}
```

`images[0].url` is the largest cover. `release_date` is `YYYY`, `YYYY-MM`, or `YYYY-MM-DD`, matching `release_date_precision`.

### Track

`item` is this object when `item.type` is `"track"`.

```json
{
  "name": "string",
  "id": "string",
  "uri": "string",
  "type": "track",
  "duration_ms": "integer",
  "explicit": "boolean",
  "track_number": "integer",
  "disc_number": "integer",
  "is_local": "boolean",
  "artists": ["Simplified artist"],
  "album": "Simplified album",
  "external_ids": {
    "isrc": "string",
    "ean": "string",
    "upc": "string"
  },
  "external_urls": { "spotify": "string" },
  "href": "string",
  "popularity": "integer",
  "preview_url": "string | null",
  "is_playable": "boolean",
  "available_markets": ["string"]
}
```

Useful fields for a lyrics overlay:

| Field | Meaning |
| --- | --- |
| `name` | Song title |
| `artists[].name` | Artist names, in credit order |
| `album.name` | Album or single title |
| `album.images` | Cover art |
| `duration_ms` | Length of the song in milliseconds |
| `id` | Spotify track id. Use this to know the song changed |
| `external_ids.isrc` | Recording id shared across music services. Useful when matching lyrics |
| `is_local` | `true` when the file is on the user's computer, not in Spotify's catalog. Local files often have no `id` |

### Episode

`item` is this object when a podcast is playing and the request asked for episodes. `item.type` is `"episode"`.

```json
{
  "name": "string",
  "id": "string",
  "type": "episode",
  "description": "string",
  "duration_ms": "integer",
  "explicit": "boolean",
  "images": ["Image"],
  "show": {
    "name": "string",
    "id": "string",
    "publisher": "string",
    "type": "show"
  },
  "uri": "string",
  "href": "string"
}
```

This script does not send `additional_types=episode`, so a podcast may not appear as an episode. The default item type is `track`.

### Device

```json
{
  "id": "string | null",
  "name": "string",
  "type": "computer | smartphone | speaker | string",
  "is_active": "boolean",
  "is_private_session": "boolean",
  "is_restricted": "boolean",
  "volume_percent": "integer | null",
  "supports_volume": "boolean"
}
```

`name` is the speaker or computer name, such as `"Kitchen speaker"`. `is_private_session` true means Spotify is hiding what is playing from other APIs.

### Context

Where playback was started. Can be `null`, for example when the user played a single track with no playlist or album.

```json
{
  "type": "artist | playlist | album | show | collection | string",
  "uri": "string",
  "href": "string",
  "external_urls": { "spotify": "string" }
}
```

### Playback actions

```json
{
  "interrupting_playback": "boolean",
  "pausing": "boolean",
  "resuming": "boolean",
  "seeking": "boolean",
  "skipping_next": "boolean",
  "skipping_prev": "boolean",
  "toggling_repeat_context": "boolean",
  "toggling_repeat_track": "boolean",
  "toggling_shuffle": "boolean",
  "transferring_playback": "boolean"
}
```

Each flag is optional. `true` means that action is currently disallowed in this context.

## Calls this script makes

### 1. Browser redirect, not JSON

`GET https://accounts.spotify.com/authorize` sends the browser to Spotify. After approval, Spotify redirects to `http://127.0.0.1:8888/callback` with query parameters, not a JSON body.

Success:

```text
/callback?code=string&state=string
```

Denied or failed:

```text
/callback?error=access_denied&state=string
```

`code` is a one-time authorization code. It is exchanged at the token endpoint and is not an access token. `state` must match the value this script sent.

### 2. POST `https://accounts.spotify.com/api/token`

Trades the authorization code, or later a refresh token, for an access token. Body is form fields, not JSON. Response is JSON.

```json
{
  "access_token": "string",
  "token_type": "Bearer",
  "scope": "string",
  "expires_in": 3600,
  "refresh_token": "string"
}
```

| Field | Meaning |
| --- | --- |
| `access_token` | Secret sent on later Web API calls. Expires after `expires_in` seconds |
| `token_type` | Always `"Bearer"` for these calls |
| `scope` | Space-separated permissions the user actually granted |
| `expires_in` | Lifetime of this access token, in seconds. Spotify uses 3600 |
| `refresh_token` | Long-lived secret used to get a new access token. A refresh response may omit it; keep the previous one |

A failed token request uses a different error object:

```json
{
  "error": "invalid_grant",
  "error_description": "string"
}
```

### 3. GET `https://api.spotify.com/v1/me`

Returns the signed-in account. Scope `user-read-private` is required for subscription fields. Scope `user-read-email` is required for `email`.

```json
{
  "account_id": "string",
  "id": "string",
  "display_name": "string | null",
  "email": "string",
  "country": "string",
  "product": "premium | free | open | string",
  "type": "user",
  "uri": "string",
  "href": "string",
  "explicit_content": {
    "filter_enabled": "boolean",
    "filter_locked": "boolean"
  },
  "followers": {
    "href": "string | null",
    "total": "integer"
  },
  "images": ["Image"],
  "external_urls": { "spotify": "string" }
}
```

| Field | Meaning |
| --- | --- |
| `display_name` | Name on the Spotify profile. Can be null |
| `id` | Username-style Spotify id. Spotify says not to use this for account linking |
| `account_id` | Stable account id Spotify recommends for linking |
| `email` | Address on the account. Present only with `user-read-email`. Unverified |
| `product` | `"premium"`, `"free"`, or `"open"` (treat `"open"` like free). Needs `user-read-private` |
| `country` | ISO country code from the account, such as `"US"` |
| `images` | Profile picture sizes |

### 4. GET `https://api.spotify.com/v1/me/player/currently-playing`

Returns what is playing right now. Scope `user-read-currently-playing` or `user-read-playback-state`.

This is the call repeated every time a line is typed. No query parameters are sent, so the item is a track unless Spotify includes another type.

When something is playing, HTTP 200:

```json
{
  "device": "Device",
  "repeat_state": "off | track | context",
  "shuffle_state": "boolean",
  "context": "Context | null",
  "timestamp": "integer",
  "progress_ms": "integer | null",
  "is_playing": "boolean",
  "currently_playing_type": "track | episode | ad | unknown",
  "item": "Track | Episode | null",
  "actions": "Playback actions"
}
```

| Field | Meaning |
| --- | --- |
| `item` | The song or episode. `null` for an ad or when the type is unknown |
| `item.name` | Title to display |
| `item.artists` | Artists, only on a track |
| `progress_ms` | How far into the item playback is, in milliseconds |
| `is_playing` | `false` while paused. The item can still be present |
| `timestamp` | Unix time in milliseconds when Spotify last reported this state |
| `currently_playing_type` | Which kind of `item` to expect |
| `device` | Which speaker or app is active |
| `repeat_state` | `"off"`, `"track"`, or `"context"` |
| `shuffle_state` | Whether shuffle is on |

When nothing is playing, Spotify returns HTTP 204 and an empty body. This script prints that as:

```json
{ "status": 204, "body": null }
```

That wrapper is printed by this script. It is not Spotify's JSON.

## Calls the current scopes allow, not used yet

These work with the token already saved. They are listed so the response shapes are in one place.

### GET `https://api.spotify.com/v1/me/player`

Scope: `user-read-playback-state`.

Same playback object as currently-playing, including device, shuffle, repeat, progress, and item. HTTP 204 and an empty body means no active device.

Optional query parameters, not sent by this script:

| Parameter | Meaning |
| --- | --- |
| `market` | ISO country code, such as `ES`. The account country wins when a user token is sent |
| `additional_types` | Comma-separated extra item types. Use `episode` to receive podcasts in `item` |

### GET `https://api.spotify.com/v1/me/player/devices`

Scope: `user-read-playback-state`.

Lists Spotify Connect devices. Some hardware never appears.

```json
{
  "devices": ["Device"]
}
```

`devices` is an array of the Device object above. The active one has `is_active: true`.

### GET `https://api.spotify.com/v1/me/player/queue`

Scope: `user-read-currently-playing` or `user-read-playback-state`.

```json
{
  "currently_playing": "Track | Episode | null",
  "queue": ["Track | Episode"]
}
```

`queue` is what will play next, in order. It uses the same Track and Episode objects as `item`.

## Error JSON from the Web API

A failed `GET` to `api.spotify.com` looks like this:

```json
{
  "error": {
    "status": 401,
    "message": "string"
  }
}
```

| Status | Meaning |
| --- | --- |
| 401 | Access token missing, expired, or rejected. This script refreshes once and retries |
| 403 | The token's scopes do not allow that endpoint, or Spotify blocked the call |
| 429 | Too many requests. Spotify may send a `Retry-After` header |
| 204 | Success with no body, not an error. Nothing is playing, or no active device |

Official field lists: [Get Current User's Profile](https://developer.spotify.com/documentation/web-api/reference/get-current-users-profile), [Get Currently Playing Track](https://developer.spotify.com/documentation/web-api/reference/get-the-users-currently-playing-track), [Get Playback State](https://developer.spotify.com/documentation/web-api/reference/get-information-about-the-users-current-playback), [Get Available Devices](https://developer.spotify.com/documentation/web-api/reference/get-a-users-available-devices), [Get Queue](https://developer.spotify.com/documentation/web-api/reference/get-queue).
