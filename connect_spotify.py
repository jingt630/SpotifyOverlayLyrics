"""Connect this machine to the Spotify Web API.

Run this file in a terminal. It asks for your Spotify Client ID, opens a
browser so you can approve the app, then saves tokens and checks that the
API works by reading your profile and the track that is playing.

Login uses Spotify's Authorization Code flow with PKCE. PKCE proves this
script started the login without storing a Client Secret. The steps are:

1. Build a one-time secret (the code verifier) and a hash of it (the challenge).
2. Send you to Spotify with the challenge and the permissions in SCOPES.
3. Catch Spotify's redirect on a local page and read the one-time code.
4. Trade that code plus the original verifier for an access token.
5. Call the Web API with that token and save it under .spotify/tokens.json.
6. Print the profile JSON once, then print the current-song JSON again
   every time you type a line in the terminal.

The granted scopes decide which account endpoints Spotify will answer.
This file requests profile, email, the current track, and playback state.

Each function docstring records its purpose, parameter types, return value,
precondition, and postcondition.

The JSON shapes those Spotify calls return are written out in
spotify_api_formats.md.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Spotify account login page. The browser is sent here so you can approve the app.
AUTH_URL = "https://accounts.spotify.com/authorize"
# Spotify endpoint that trades the one-time login code for an access token.
TOKEN_URL = "https://accounts.spotify.com/api/token"
# Web API call that returns the signed-in user's profile. Used to confirm login.
API_ME = "https://api.spotify.com/v1/me"
# Web API call that returns the track currently playing for that user.
API_NOW_PLAYING = "https://api.spotify.com/v1/me/player/currently-playing"

# Local address Spotify sends the browser back to after you approve the app.
# This exact URI must also be saved in the Spotify app's Redirect URI settings.
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8888
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"

# Permissions requested from the Spotify account. Spotify shows these on the
# approval page, and the access token can only call endpoints these allow.
# user-read-private / user-read-email: read the account profile.
# user-read-currently-playing: read the track that is playing now.
# user-read-playback-state: read playback details such as progress and device.
SCOPES = " ".join(
    [
        "user-read-private",
        "user-read-email",
        "user-read-currently-playing",
        "user-read-playback-state",
    ]
)

# Where the Client ID and tokens are stored after a successful login.
# This path is gitignored so the tokens are not committed.
TOKEN_PATH = Path(__file__).resolve().parent / ".spotify" / "tokens.json"
# How long to wait for you to finish approving the app in the browser.
LOGIN_TIMEOUT_SECONDS = 180


def prompt(label: str) -> str:
    """Ask in the terminal and keep asking until the answer is not blank.

    Purpose:
        Collect one required text value from the person running the script.

    Parameters:
        label (str): Text printed before the input cursor.

    Returns:
        str: The typed line with surrounding whitespace removed.

    Precondition:
        stdin can read a line of text.

    Postcondition:
        The returned string is not empty.
    """
    value = input(label).strip()
    while not value:
        value = input("That cannot be empty. Try again: ").strip()
    return value


def generate_pkce() -> tuple[str, str]:
    """Create the PKCE pair Spotify requires for a login without a Client Secret.

    Purpose:
        Prove this script started the login. The verifier stays on this
        machine. Only its SHA-256 hash, the challenge, is sent in the browser.
        The later token request must send the original verifier.

    Parameters:
        None.

    Returns:
        tuple[str, str]: (code_verifier, code_challenge). The verifier is a
        URL-safe random string. The challenge is its unpadded base64url SHA-256.

    Precondition:
        The process can read from the operating system's random source.

    Postcondition:
        The challenge is the S256 hash of the verifier, and the two values differ.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


class CallbackResult:
    """Hold the values Spotify sends back to the local redirect page.

    Purpose:
        Share the redirect result between the HTTP server thread and the
        thread that is waiting for login to finish.

    Attributes:
        code (str | None): One-time authorization code, or None.
        state (str | None): State value Spotify echoed, or None.
        error (str | None): Failure reason, or None when login succeeded.
        ready (threading.Event): Set after the redirect has been handled.
    """

    def __init__(self) -> None:
        """Start a result that has not received a redirect yet.

        Purpose:
            Create empty fields and an unset ready event.

        Parameters:
            None.

        Returns:
            None.

        Precondition:
            None.

        Postcondition:
            code, state, and error are None, and ready is not set.
        """
        self.code: str | None = None  # One-time code to exchange for an access token.
        self.state: str | None = None  # Echo of the random value sent with the login.
        self.error: str | None = None  # Set when Spotify or the state check rejects login.
        self.ready = threading.Event()  # Set once the redirect page has been handled.


def make_handler(expected_state: str, result: CallbackResult):
    """Build the local page handler that receives Spotify's redirect.

    Purpose:
        Turn Spotify's browser redirect into fields on result, then let the
        waiting login thread continue.

    Parameters:
        expected_state (str): Random state sent with this login request.
        result (CallbackResult): Object the handler fills when the redirect arrives.

    Returns:
        type[BaseHTTPRequestHandler]: A request-handler class bound to
        expected_state and result. HTTPServer instantiates it per request.

    Precondition:
        result.ready is not set, and expected_state is the state in the
        authorization URL for this attempt.

    Postcondition:
        The returned class reads GET /callback into result and sets result.ready.
        It does not start the server.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            """Read Spotify's redirect and store the code, state, or error.

            Purpose:
                Accept only /callback, check that state matches this login,
                and show a short browser page.

            Parameters:
                None beyond the request stored on self by BaseHTTPRequestHandler.
                self.path (str) is the request path plus query string.

            Returns:
                None. The response is written to the browser socket.

            Precondition:
                result and expected_state are the objects closed over by make_handler.

            Postcondition:
                result.code, result.state, and result.error match the redirect.
                A state mismatch clears result.code and sets result.error.
                result.ready is set, and the browser received a 200 or 400 page.
                Any path other than /callback gets 404 and does not set ready.
            """
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            query = urllib.parse.parse_qs(parsed.query)
            result.state = (query.get("state") or [None])[0]
            result.error = (query.get("error") or [None])[0]
            result.code = (query.get("code") or [None])[0]
            if result.state != expected_state:
                result.error = result.error or "state_mismatch"
                result.code = None

            if result.code and not result.error:
                body = (
                    "<html><body><h1>Spotify connected</h1>"
                    "<p>You can close this tab and return to the terminal.</p>"
                    "</body></html>"
                )
                status = 200
            else:
                body = (
                    "<html><body><h1>Spotify did not authorize this app</h1>"
                    f"<p>Error: {result.error or 'unknown'}</p>"
                    "<p>Return to the terminal for details.</p>"
                    "</body></html>"
                )
                status = 400

            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            result.ready.set()

        def log_message(self, format: str, *args) -> None:
            """Drop the default HTTP server log line.

            Purpose:
                Keep the terminal free of one-line request logs.

            Parameters:
                format (str): The log format string from BaseHTTPRequestHandler.
                *args: Values that would have been interpolated into format.

            Returns:
                None.

            Precondition:
                None.

            Postcondition:
                Nothing is written to stderr for this request.
            """
            return

    return Handler


def post_form(url: str, fields: dict[str, str]) -> dict:
    """POST form fields and return the JSON body.

    Purpose:
        Send the authorization-code or refresh-token request to Spotify.

    Parameters:
        url (str): Full endpoint URL, normally TOKEN_URL.
        fields (dict[str, str]): Form body. Keys and values are strings.

    Returns:
        dict: The parsed JSON object from a successful response.

    Precondition:
        url is an HTTP or HTTPS URL, and fields contains the grant Spotify expects.

    Postcondition:
        On HTTP success, the returned dict is Spotify's JSON body.
        On HTTP error, RuntimeError is raised and includes the status and body.
        No tokens are printed.
    """
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Spotify token request failed ({exc.code}): {detail}") from exc


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


def save_tokens(client_id: str, token: dict, previous_refresh: str | None = None) -> dict:
    """Write the Client ID and tokens to TOKEN_PATH.

    Purpose:
        Persist the login so the next run can skip the browser. A refresh
        response sometimes omits a new refresh token, so previous_refresh
        keeps the one already saved.

    Parameters:
        client_id (str): Spotify app Client ID.
        token (dict): Spotify token JSON. Must contain "access_token".
            Optional keys: "refresh_token", "token_type", "scope", "expires_in".
        previous_refresh (str | None): Refresh token to keep when token has none.
            Defaults to None.

    Returns:
        dict: The session written to disk. Keys are client_id, access_token,
        refresh_token, token_type, scope, and expires_in.

    Precondition:
        token["access_token"] exists. TOKEN_PATH's parent directory can be created.

    Postcondition:
        TOKEN_PATH exists and contains the returned session as JSON.
        The token strings are not printed. refresh_token is token's value when
        present, otherwise previous_refresh.
    """
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "client_id": client_id,
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token") or previous_refresh,
        "token_type": token.get("token_type", "Bearer"),
        "scope": token.get("scope", ""),
        "expires_in": token.get("expires_in"),
    }
    TOKEN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_saved_session() -> dict | None:
    """Read the login saved by an earlier run.

    Purpose:
        Reuse a completed login instead of opening the browser again.

    Parameters:
        None.

    Returns:
        dict | None: The saved session when client_id and access_token are
        present. None when the file is missing or those fields are empty.

    Precondition:
        If TOKEN_PATH exists, it contains a JSON object.

    Postcondition:
        The file is unchanged. The returned dict is None or has non-empty
        client_id and access_token values.
    """
    if not TOKEN_PATH.exists():
        return None
    saved = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    if not saved.get("client_id") or not saved.get("access_token"):
        return None
    return saved


def refresh_session(session: dict) -> dict:
    """Trade the saved refresh token for a new access token.

    Purpose:
        Replace an expired access token without sending the user through the browser.

    Parameters:
        session (dict): Saved login. Required keys: "client_id", "refresh_token".

    Returns:
        dict: The new session returned by save_tokens, including the new access token.

    Precondition:
        session["refresh_token"] and session["client_id"] are non-empty strings,
        and Spotify still accepts that refresh token.

    Postcondition:
        TOKEN_PATH holds the new access token. The returned dict is that session.
        RuntimeError is raised when the refresh token is missing or Spotify
        does not return an access token. The input session object is not mutated.
    """
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Saved login has no refresh token. Connect again.")
    token = post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": session["client_id"],
        },
    )
    if "access_token" not in token:
        raise RuntimeError(f"Spotify did not refresh the access token: {token}")
    return save_tokens(session["client_id"], token, previous_refresh=refresh_token)


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


def print_setup() -> None:
    """Print the dashboard steps needed before the Client ID can be used.

    Purpose:
        Tell the user which Spotify dashboard settings this script expects.

    Parameters:
        None.

    Returns:
        None.

    Precondition:
        stdout can be written.

    Postcondition:
        The terminal shows the dashboard URL and the exact REDIRECT_URI.
        No network call is made and no file is changed.
    """
    print()
    print("Spotify for Developers connection")
    print("---------------------------------")
    print("1. Open https://developer.spotify.com/dashboard and log in.")
    print("2. Create an app, or open the app you already created.")
    print("3. Open the app settings and add this Redirect URI exactly:")
    print(f"   {REDIRECT_URI}")
    print("4. Copy the Client ID from that app. A Client Secret is not needed.")
    print()


def authorize(client_id: str) -> dict:
    """Open Spotify login, wait for the redirect, and exchange the code.

    Purpose:
        Complete the Authorization Code with PKCE flow for one Client ID.

    Parameters:
        client_id (str): Client ID from the Spotify developer dashboard.

    Returns:
        dict: Spotify's token JSON, including "access_token", "expires_in",
        "scope", and usually "refresh_token".

    Precondition:
        client_id belongs to an app whose redirect allowlist contains REDIRECT_URI.
        REDIRECT_PORT is free on this machine. The user can approve the app
        in a browser within LOGIN_TIMEOUT_SECONDS.

    Postcondition:
        The local callback server is stopped. On success, the returned dict
        contains "access_token" and nothing has been written to TOKEN_PATH yet.
        TimeoutError or RuntimeError is raised when the redirect does not
        produce a usable code. The authorization code is not printed.
    """
    verifier, challenge = generate_pkce()
    # Random value Spotify must return unchanged, so a stray redirect is ignored.
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "scope": SCOPES,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    auth_url = f"{AUTH_URL}?{query}"
    result = CallbackResult()
    server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), make_handler(state, result))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("Opening Spotify in your browser so you can approve the app.")
    print("If the browser does not open, paste this URL yourself:")
    print(auth_url)
    print()
    webbrowser.open(auth_url)

    if not result.ready.wait(LOGIN_TIMEOUT_SECONDS):
        server.shutdown()
        raise TimeoutError(
            "Timed out waiting for Spotify to redirect back. "
            f"Check that {REDIRECT_URI} is saved in the app settings, then run this again."
        )
    server.shutdown()

    if result.error or not result.code:
        raise RuntimeError(f"Authorization was not granted ({result.error or 'no code'}).")

    return post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": result.code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )


def print_json(title: str, payload) -> None:
    """Pretty-print one Spotify response.

    Purpose:
        Show every field Spotify returned, under a heading.

    Parameters:
        title (str): Heading printed above the body.
        payload (dict | list | str | int | float | bool | None): Value to print.
            None prints a fixed empty-body line instead of JSON.

    Returns:
        None.

    Precondition:
        stdout accepts text. payload is JSON-serializable when it is not None.

    Postcondition:
        The heading and the payload are written to stdout. payload is unchanged.
    """
    print()
    print(f"----- {title} -----")
    if payload is None:
        print("(Spotify returned an empty body)")
        return
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def print_token_summary(token: dict) -> None:
    """Show Spotify's token response without the secret token strings.

    Purpose:
        Confirm which scopes and lifetime Spotify granted while leaving the
        access token and refresh token only in the token file.

    Parameters:
        token (dict): Token JSON from authorize or a refresh. Read keys are
            "token_type", "expires_in", and "scope".

    Returns:
        None.

    Precondition:
        token is a dict. Missing keys are printed as null.

    Postcondition:
        stdout shows token_type, expires_in, and scope. The access token and
        refresh token strings are not written.
    """
    print_json(
        "Spotify token response",
        {
            "token_type": token.get("token_type"),
            "expires_in_seconds": token.get("expires_in"),
            "scope": token.get("scope"),
            "access_token": "(saved in .spotify/tokens.json, not printed)",
            "refresh_token": "(saved in .spotify/tokens.json, not printed)",
        },
    )


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


def print_profile(session: dict) -> dict:
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
    print_json("GET /v1/me  (profile)", me)
    return session


def print_now_playing(session: dict) -> dict:
    """Read and print the user's currently playing item.

    Purpose:
        Call GET /v1/me/player/currently-playing and show a one-line summary
        plus the raw JSON. HTTP 204 means nothing is playing.

    Parameters:
        session (dict): Saved login passed to spotify_get.

    Returns:
        dict: The session to keep using. It is a new dict if the access token
        was refreshed during the call.

    Precondition:
        session can call API_NOW_PLAYING under user-read-currently-playing
        or user-read-playback-state.

    Postcondition:
        stdout shows song_summary's line and the response JSON.
        A 204 response is printed as {"status": 204, "body": null}.
        The returned session can be passed to the next API call.
    """
    session, status, now = spotify_get(session, API_NOW_PLAYING)
    print(song_summary(now))
    if status == 204:
        print_json("GET /v1/me/player/currently-playing", {"status": 204, "body": None})
        return session
    print_json("GET /v1/me/player/currently-playing", now)
    return session


def watch_keyboard(session: dict) -> None:
    """Fetch the current song again after each line typed in the terminal.

    Purpose:
        Keep reading playback until the user asks to stop. Enter alone counts
        as a request. The lines q, quit, and exit end the loop.

    Parameters:
        session (dict): Saved login passed to print_now_playing.

    Returns:
        None.

    Precondition:
        stdin can read lines, and session can call the currently-playing endpoint.

    Postcondition:
        Each non-quit line has printed one currently-playing response.
        The function returns after a quit line, or raises KeyboardInterrupt
        if the user cancels with Ctrl+C. The latest session stays local to
        this loop.
    """
    print()
    print("Type anything and press Enter to read the current song again.")
    print("Type q and press Enter to stop.")
    while True:
        typed = input("> ")
        if typed.strip().lower() in {"q", "quit", "exit"}:
            print("Stopped.")
            return
        session = print_now_playing(session)


def main() -> None:
    """Log in if needed, print the profile, then reread the song on each line.

    Purpose:
        Run the whole connection flow from the command line.

    Parameters:
        None. A missing token file causes a Client ID prompt via stdin.

    Returns:
        None.

    Precondition:
        stdout and stdin are available. Either TOKEN_PATH holds a usable
        session, or the user can complete browser login.

    Postcondition:
        The profile and at least one currently-playing response have been
        printed, unless an exception stopped the run. A new login is saved
        to TOKEN_PATH before those prints. watch_keyboard then owns the
        terminal until the user quits.
    """
    # Song titles can contain characters the default Windows console cannot print.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    session = load_saved_session()
    if session:
        print("Using the Spotify login already saved in .spotify/tokens.json")
    else:
        print_setup()
        client_id = prompt("Paste your Spotify Client ID: ")
        token = authorize(client_id)
        if "access_token" not in token:
            raise RuntimeError(f"Spotify did not return an access token: {token}")
        session = save_tokens(client_id, token)
        print()
        print(f"Tokens saved to {TOKEN_PATH}")
        print_token_summary(token)

    session = print_profile(session)
    session = print_now_playing(session)
    watch_keyboard(session)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(1)
    except Exception as exc:
        print(f"\nCould not connect: {exc}")
        raise SystemExit(1)
