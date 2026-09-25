"""Read the signed-in profile and the track that is playing.

These calls use the access token from spotify_client.auth. They do not
return lyrics. progress_ms is only the playback position in milliseconds.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from spotify_client.auth import print_json, refresh_session

# Web API call that returns the signed-in user's profile.
API_ME = "https://api.spotify.com/v1/me"
# Web API call that returns the track currently playing for that user.
API_NOW_PLAYING = "https://api.spotify.com/v1/me/player/currently-playing"


def get_json(url: str, access_token: str) -> tuple[int, dict | None]:
    """GET a Web API URL with the access token.

    Purpose:
        Call one Spotify Web API endpoint on behalf of the signed-in user.

    Parameters:
        url (str): Full API URL, such as API_ME or API_NOW_PLAYING.
        access_token (str): Bearer token from the saved login.

    Returns:
        tuple[int, dict | None]: HTTP status, and the JSON object. The dict is
        None when the body is empty, including a 204 from the now-playing call.

    Precondition:
        access_token is a non-empty Spotify access token.

    Postcondition:
        On HTTP success, the status and body match Spotify's response.
        On HTTP error, RuntimeError is raised and includes the status and body.
    """
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Spotify API request failed ({exc.code}): {detail}") from exc


def spotify_get(session: dict, url: str) -> tuple[dict, int, dict | None]:
    """GET a Web API URL and refresh the access token once if it is expired.

    Purpose:
        Call Spotify with the saved login, and recover from one HTTP 401 by
        refreshing the access token before a second attempt.

    Parameters:
        session (dict): Saved login with "access_token". A refresh also needs
            "client_id" and "refresh_token".
        url (str): Full Web API URL.

    Returns:
        tuple[dict, int, dict | None]: The session to keep using, the HTTP
        status, and the JSON body. The session is a new dict only when a
        refresh happened. The body is None when Spotify sends an empty body.

    Precondition:
        session["access_token"] is a string. url is a Spotify Web API URL
        allowed by the token's scopes.

    Postcondition:
        On success, the status and body match Spotify's response.
        After a 401, the saved token file has been refreshed once and the call
        has been retried. Other HTTP errors still raise RuntimeError.
    """
    try:
        status, body = get_json(url, session["access_token"])
        return session, status, body
    except RuntimeError as exc:
        # 401 means the access token expired. One refresh, then try the call again.
        if "(401)" not in str(exc):
            raise
        session = refresh_session(session)
        status, body = get_json(url, session["access_token"])
        return session, status, body


def song_summary(now: dict | None) -> str:
    """Build one line describing the item in a currently-playing response.

    Purpose:
        Give a short title-and-artist line. The full JSON is printed separately.

    Parameters:
        now (dict | None): Body of GET /v1/me/player/currently-playing, or None
            when Spotify returned an empty body.

    Returns:
        str: "Nothing is currently playing." when there is no item. Otherwise
        "Now playing: <title> — <artists or show>".

    Precondition:
        now is None or a dict. When "item" is present, it is a dict.

    Postcondition:
        now is unchanged. The returned string is non-empty.
    """
    if not now or not now.get("item"):
        return "Nothing is currently playing."
    item = now["item"]
    artists = ", ".join(artist.get("name", "") for artist in item.get("artists", []))
    title = item.get("name") or item.get("type") or "Unknown item"
    if artists:
        return f"Now playing: {title} — {artists}"
    show = (item.get("show") or {}).get("name")
    if show:
        return f"Now playing: {title} — {show}"
    return f"Now playing: {title}"


def fetch_now_playing(session: dict) -> tuple[dict, dict | None]:
    """Read the currently playing item without printing it.

    Purpose:
        Poll Spotify for progress_ms while a lyric line is followed locally.

    Parameters:
        session (dict): Saved login passed to spotify_get.

    Returns:
        tuple[dict, dict | None]: The session to keep using, and the playback
        body. The body is None when Spotify returns 204 or an empty item.

    Precondition:
        session can call API_NOW_PLAYING.

    Postcondition:
        Nothing is written to stdout. The returned session can be polled again.
    """
    session, status, now = spotify_get(session, API_NOW_PLAYING)
    if status == 204 or not now:
        return session, None
    return session, now


def print_profile(session: dict, *, verbose: bool = True) -> dict:
    """Read and print the signed-in user's profile.

    Purpose:
        Call GET /v1/me and show both a one-line identity and the raw JSON.

    Parameters:
        session (dict): Saved login passed to spotify_get.

    Returns:
        dict: The session to keep using. It is a new dict if the access token
        was refreshed during the call.

    Precondition:
        session can call API_ME under the user-read-private scope. The email
        field is present only when user-read-email was granted.

    Postcondition:
        stdout shows the display name or id, plus the profile JSON.
        RuntimeError is raised when the profile body is empty.
        The returned session can be passed to the next API call.
    """
    session, _, me = spotify_get(session, API_ME)
    if not me:
        raise RuntimeError("Spotify returned an empty profile.")
    name = me.get("display_name") or me.get("id")
    print(f"Signed in as: {name} ({me.get('id')})")
    if verbose:
        print_json("GET /v1/me  (profile)", me)
    return session


def print_now_playing(session: dict) -> tuple[dict, dict | None]:
    """Read and print the user's currently playing item.

    Purpose:
        Call GET /v1/me/player/currently-playing and show a one-line summary
        plus the raw JSON. HTTP 204 means nothing is playing.

    Parameters:
        session (dict): Saved login passed to spotify_get.

    Returns:
        tuple[dict, dict | None]: The session to keep using, and the playback
        body. The body is None when Spotify returns 204 or an empty item.

    Precondition:
        session can call API_NOW_PLAYING under user-read-currently-playing
        or user-read-playback-state.

    Postcondition:
        stdout shows song_summary's line and the response JSON.
        A 204 response is printed as {"status": 204, "body": null}.
        The returned session can be passed to the next API call.
    """
    session, now = fetch_now_playing(session)
    status = 204 if now is None else 200
    print(song_summary(now))
    if status == 204 or not now:
        print_json("GET /v1/me/player/currently-playing", {"status": 204, "body": None})
        return session, None
    print_json("GET /v1/me/player/currently-playing", now)
    return session, now
