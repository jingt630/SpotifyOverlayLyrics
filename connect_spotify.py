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
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
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
    """Ask in the terminal and keep asking until the answer is not blank."""
    value = input(label).strip()
    while not value:
        value = input("That cannot be empty. Try again: ").strip()
    return value


def generate_pkce() -> tuple[str, str]:
    """Create the PKCE pair Spotify requires for a login without a Client Secret.

    The verifier stays on this machine. Only its SHA-256 hash, the challenge,
    is sent to Spotify in the browser. The token request later must include
    the original verifier, which proves this same script started the login.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


class CallbackResult:
    """Values Spotify sends back to the local redirect page."""

    def __init__(self) -> None:
        self.code: str | None = None  # One-time code to exchange for an access token.
        self.state: str | None = None  # Echo of the random value sent with the login.
        self.error: str | None = None  # Set when Spotify or the state check rejects login.
        self.ready = threading.Event()  # Set once the redirect page has been handled.


def make_handler(expected_state: str, result: CallbackResult):
    """Build the local page handler that receives Spotify's redirect.

    expected_state is the random value sent with the login request. It must
    match the value Spotify returns, which confirms the redirect belongs to
    this login attempt.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
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
            return

    return Handler


def post_form(url: str, fields: dict[str, str]) -> dict:
    """POST form fields and return the JSON body. Used for the token exchange."""
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
    """GET a Web API URL with the access token and return status plus JSON."""
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


def save_tokens(client_id: str, token: dict) -> None:
    """Write the Client ID and tokens to TOKEN_PATH. Tokens are not printed."""
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "client_id": client_id,
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "token_type": token.get("token_type", "Bearer"),
        "scope": token.get("scope", ""),
        "expires_in": token.get("expires_in"),
    }
    TOKEN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def print_setup() -> None:
    """Print the dashboard steps needed before the Client ID can be used."""
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
    """Open Spotify login, wait for the redirect, and return the token JSON."""
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


def show_account(access_token: str) -> None:
    """Call the profile and now-playing endpoints to confirm the token works."""
    _, me = get_json(API_ME, access_token)
    if not me:
        raise RuntimeError("Spotify returned an empty profile.")
    name = me.get("display_name") or me.get("id")
    print(f"Signed in as: {name} ({me.get('id')})")

    status, now = get_json(API_NOW_PLAYING, access_token)
    if status == 204 or not now or not now.get("item"):
        print("Nothing is currently playing.")
        return
    item = now["item"]
    artists = ", ".join(artist.get("name", "") for artist in item.get("artists", []))
    print(f"Now playing: {item.get('name')} — {artists}")


def main() -> None:
    """Collect the Client ID, log in, save tokens, and print a connection check."""
    print_setup()
    client_id = prompt("Paste your Spotify Client ID: ")
    token = authorize(client_id)
    if "access_token" not in token:
        raise RuntimeError(f"Spotify did not return an access token: {token}")
    save_tokens(client_id, token)
    print()
    print(f"Tokens saved to {TOKEN_PATH}")
    show_account(token["access_token"])
    print("Spotify Web API connection is ready.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(1)
    except Exception as exc:
        print(f"\nCould not connect: {exc}")
        raise SystemExit(1)
