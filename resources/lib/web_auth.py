"""Regular Spotify authorization-code (PKCE) login for Web API tokens.

Spotify rate-limits tokens minted through the legacy Spotty Device-Connect
flow on the standard Web API (api.spotify.com), so catalogue/search requests
fail with HTTP 429 even for a signed-in account.  A token obtained through
the ordinary browser authorization-code flow is not rate-limited the same
way.  This module reuses the browser login the built-in terminal installer
performs: PKCE challenge, a short-lived loopback callback listener, token
exchange, and automatic refresh through the stored refresh token.

Playback is untouched: it keeps using Spotty Device Connect.  This module
only supplies the account Web API token the catalogue reads.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

ACCOUNTS_URL = "https://accounts.spotify.com/api/token"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
ZIP1_CREATE_URL = "https://zip1.io/api/create"
TOKEN_FILE = "web-auth.json"
REQUEST_FILE = "web-auth-request.json"
REQUEST_TTL = 600
DEFAULT_PORT = 53209
DEFAULT_REDIRECT_URI = "http://127.0.0.1:{}/callback".format(DEFAULT_PORT)
DEFAULT_WEB_AUTH_REDIRECT_URI = "https://resonance-spotify-callback.vercel.app/api/callback"
DEFAULT_SCOPE = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-read-private",
]
TOKEN_LIFETIME = 3600


class WebAuthError(Exception):
    pass


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def random_state():
    return secrets.token_urlsafe(24)


def is_local_redirect(redirect_uri):
    """True when the redirect targets the machine itself (loopback)."""
    try:
        host = (urllib.parse.urlparse(redirect_uri).hostname or "").lower()
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1")


def derive_poll_url(redirect_uri):
    """Map a hosted redirect to this add-on's poll endpoint.

    For loopback redirects there is no remote relay and the add-on waits on
    its local callback listener instead (returns "").
    """
    if is_local_redirect(redirect_uri):
        return ""
    try:
        parts = urllib.parse.urlparse(redirect_uri)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return urllib.parse.urlunparse((parts.scheme, parts.netloc, "/api/poll", "", "", ""))


def poll_for_code(poll_url, state, timeout=8):
    """Ask a hosted relay for the authorization code for this state."""
    separator = "&" if "?" in poll_url else "?"
    url = poll_url + separator + "state=" + urllib.parse.quote(state)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.load(response)
    except Exception:
        return ""
    if isinstance(data, dict):
        code = data.get("code")
        if isinstance(code, str) and code:
            return code
    return ""


def shorten_url(long_url, max_clicks=1, timeout=10, fetcher=None):
    """Shorten an authorize URL with zip1.io for users without a browser.

    Returns the short URL, or an empty string when the service is unavailable
    so callers can fall back to showing the full address.
    """
    try:
        if fetcher is not None:
            raw = fetcher(long_url, max_clicks)
        else:
            body = json.dumps({"url": long_url, "max-clicks": max_clicks}).encode("utf-8")
            request = urllib.request.Request(
                ZIP1_CREATE_URL,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = json.load(response)
    except Exception:
        return ""
    short = ""
    if isinstance(raw, dict):
        short = raw.get("short_url") or raw.get("url") or raw.get("short") or ""
    if isinstance(short, str):
        return short.strip()
    return ""


def authorize_url(client_id, redirect_uri, scopes, challenge, state):
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "show_dialog": "true",
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(client_id, redirect_uri, code, verifier, timeout=30):
    """Exchange an authorization code for access/refresh tokens."""
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        ACCOUNTS_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "ignore")
        except Exception:
            detail = ""
        raise WebAuthError(
            "token exchange failed (HTTP {}): {}".format(exc.code, detail[:200])
        ) from exc
    except urllib.error.URLError as exc:
        raise WebAuthError("token exchange network error: {}".format(exc.reason)) from exc


def refresh_access_token(client_id, refresh_token, timeout=30):
    """Refresh an access token through the stored refresh token."""
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        ACCOUNTS_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "ignore")
        except Exception:
            detail = ""
        raise WebAuthError(
            "token refresh failed (HTTP {}): {}".format(exc.code, detail[:200])
        ) from exc
    except urllib.error.URLError as exc:
        raise WebAuthError("token refresh network error: {}".format(exc.reason)) from exc


class CallbackServer:
    """Short-lived HTTP server that captures one /callback response."""

    def __init__(self, callback_path="/callback", host="0.0.0.0", port=DEFAULT_PORT):
        self.callback_path = callback_path
        self.result = {}
        self.condition = threading.Condition()

        handler = self._handler_class()
        try:
            self.httpd = HTTPServer((host, port), handler)
        except OSError as exc:
            raise WebAuthError(
                "could not open {}:{} for the login callback: {}".format(host, port, exc)
            ) from exc
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def _handler_class(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != outer.callback_path:
                    self.send_response(404)
                    self.end_headers()
                    return
                query = urllib.parse.parse_qs(parsed.query)
                if "error" in query:
                    outer.result["error"] = query["error"][0]
                elif "code" in query:
                    outer.result["code"] = query["code"][0]
                outer.result["state"] = query.get("state", [None])[0]
                with outer.condition:
                    outer.condition.notify_all()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body style='background:#121212;color:#1DB954;"
                    b"font-family:Arial;text-align:center;padding-top:80px'>"
                    b"<h2>Resonance: Spotify connected.</h2>"
                    b"<p>You can close this tab.</p></body></html>"
                )

            def log_message(self, *args):
                pass

        return Handler

    def start(self):
        self.thread.start()

    def wait(self, timeout):
        with self.condition:
            if not self.result:
                self.condition.wait(timeout)
        return self.result

    def stop(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()


def _normalize(data, client_id, now):
    expires_in = int(data.get("expires_in") or TOKEN_LIFETIME)
    token = (
        data.get("access_token")
        or data.get("accessToken")
        or ""
    )
    refresh = (
        data.get("refresh_token")
        or data.get("refreshToken")
        or ""
    )
    return {
        "access_token": token,
        "refresh_token": refresh,
        "expires_at": now + expires_in,
        "client_id": client_id,
    }


def save_login_request(directory, client_id, redirect_uri, state, verifier, clock=time.time):
    """Persist a pending PKCE request so the code can be pasted later."""
    data = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "verifier": verifier,
        "expires_at": clock() + REQUEST_TTL,
    }
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, REQUEST_FILE)
    try:
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        os.chmod(path, 0o600)
    except OSError:
        return None
    return path


def load_login_request(directory, client_id, clock=time.time):
    """Return a pending, unexpired PKCE request or None."""
    path = os.path.join(directory, REQUEST_FILE)
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("client_id") != client_id:
        return None
    if not data.get("verifier"):
        return None
    if data.get("expires_at", 0) <= clock():
        return None
    return data


def clear_login_request(directory):
    try:
        os.remove(os.path.join(directory, REQUEST_FILE))
    except OSError:
        pass


class WebAuth:
    """Token store for the regular authorization-code account login."""

    def __init__(self, directory, client_id, clock=time.time):
        self.directory = directory
        self.client_id = client_id
        self.clock = clock
        self.path = os.path.join(directory, TOKEN_FILE)

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(data, dict) or not data.get("access_token"):
            return None
        if data.get("client_id") != self.client_id:
            return None
        return data

    def save(self, data):
        os.makedirs(self.directory, exist_ok=True)
        try:
            with open(self.path, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise WebAuthError("could not persist the Spotify token: {}".format(exc)) from exc

    def clear(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def has_session(self):
        stored = self.load()
        return bool(stored and stored.get("refresh_token"))

    @staticmethod
    def _apply(data, new_values):
        return {key: new_values.get(key, data.get(key)) for key in data}

    def access_token(self, refresh_fetcher=None):
        """Return a usable access token, refreshing when expired."""
        stored = self.load()
        if not stored:
            return ""
        if stored.get("expires_at", 0) > self.clock() + 60:
            return stored["access_token"]
        refresh = stored.get("refresh_token")
        if not refresh:
            return ""
        now = self.clock()
        if refresh_fetcher is not None:
            refreshed = refresh_fetcher(self.client_id, refresh)
        else:
            refreshed = refresh_access_token(self.client_id, refresh)
        if isinstance(refreshed, dict):
            merged = dict(stored)
            merged.update(_normalize(refreshed, self.client_id, now))
            self.save(merged)
            return merged["access_token"]
        return ""