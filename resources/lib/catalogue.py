"""Music catalogue with persistent pages and shared provider cooldowns."""

import ast
import json
import os
import re
import sqlite3
import time
from urllib.parse import urlencode

import requests


class Unavailable(Exception):
    pass


class Forbidden(Unavailable):
    """Spotify declined this request with HTTP 403.

    This can happen when reading a playlist that is visible in the account's
    playlist list but whose track items are not readable for the current app or
    account. Contrary to a network failure or a rate limit, a 403 is not a
    retry signal, so it must never arm the shared cooldown gate.
    """
    pass


def unplayable_reason(track):
    """Return a user-facing reason when a track can never stream, else empty.

    Spotify reports a definitive availability problem through is_playable
    together with a restrictions body (e.g. ``{"reason": "market"}``). Such
    tracks still carry valid metadata and duration, so without this check the
    local streamer would only discover the failure after repeated timeouts.
    """
    if track.get("is_playable") is False and track.get("restrictions"):
        return "This track is not available for playback in your region"
    return ""


def _image_url(item):
    raw = (item or {}).get("images") or (item or {}).get("thumbnail") or []
    if not isinstance(raw, list):
        return raw or ""
    for entry in raw:
        url = entry.get("url") if isinstance(entry, dict) else entry
        if url:
            return url
    return ""


def normalize(item, kind="track", album=None):
    item = item or {}
    artists = item.get("artists") or item.get("artist") or []
    if not isinstance(artists, list):
        artists = [artists]
    artists = [a if isinstance(a, dict) else {"name": str(a)} for a in artists]
    raw_album = item.get("album") or album or {}
    if not isinstance(raw_album, dict):
        raw_album = {"name": str(raw_album)}
    image = _image_url(item) or _image_url(raw_album)
    source_album = {key: raw_album[key] for key in ("id", "name", "release_date", "images", "artists") if key in raw_album}
    result = dict(item)
    result.update(
        name=item.get("name") or item.get("title") or "Untitled",
        type=kind, artists=artists, album=source_album,
        images=[{"url": image}] if image else [],
    )
    return result


def database_path(directory):
    """Return the catalogue DB path, upgrading the pre-rename file once."""
    path = os.path.join(directory, "catalogue.db")
    legacy = os.path.join(directory, "catalogue-v2.db")
    if not os.path.exists(path) and os.path.exists(legacy):
        try:
            os.rename(legacy, path)
        except OSError:
            pass
    return path


def remember_failed(directory, identity):
    """Persist a confirmed stream failure marker shared with plugin windows.

    The spotty binary cannot report an upfront availability verdict, so a
    discovery run (first_audio_failed) records the offending track here. Later
    plays of the same track reject immediately instead of stalling, which is
    exactly what should happen when Spotify removed a track and its API is
    temporarily rate-limited for the device.
    """
    if not re.fullmatch(r"[A-Za-z0-9]{22}", identity):
        return
    db = sqlite3.connect(database_path(directory), timeout=10)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, value TEXT, expires REAL)")
        db.execute(
            "INSERT OR REPLACE INTO pages VALUES (?,?,?)",
            (
                "tfail:" + identity,
                json.dumps({"reason": "This track could not be streamed on this device"}),
                time.time() + 604800,
            ),
        )
        db.commit()
    finally:
        db.close()


class Catalogue:
    def __init__(self, directory, token, session=None, clock=time.time, signed_in=False,
                 bypass_response_cache=False):
        self.directory = directory
        self.token = token
        self.http = session or requests.Session()
        self.clock = clock
        self.signed_in = bool(signed_in)
        self.bypass_response_cache = bypass_response_cache
        self.notice = ""
        os.makedirs(directory, exist_ok=True)
        self.db = sqlite3.connect(database_path(directory), timeout=10)
        self.db.execute("CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, value TEXT, expires REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS gates (provider TEXT PRIMARY KEY, until REAL)")
        self.db.commit()

    def cached(self, key, stale=False):
        row = self.db.execute("SELECT value, expires FROM pages WHERE key=?", (key,)).fetchone()
        if row and (stale or row[1] > self.clock()):
            return json.loads(row[0])
        return None

    def save(self, key, value, ttl=600):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?)", (key, json.dumps(value), self.clock() + ttl))
        return value

    def remaining(self, provider):
        row = self.db.execute("SELECT until FROM gates WHERE provider=?", (provider,)).fetchone()
        until = row[0] if row else 0
        if provider == "spotify":
            try:
                with open(os.path.join(self.directory, "spotify-api-gate.json"), encoding="utf-8") as stream:
                    until = max(until, float(json.load(stream).get("retry_until") or 0))
            except (OSError, ValueError, TypeError):
                pass
        return max(0, int(until - self.clock()))

    def request(self, provider, path, params=None, refresh=False):
        if provider != "spotify":
            raise ValueError("Unknown catalogue provider")
        # Account requests (and their cached responses) belong to the signed-in
        # user. After a logout/account switch they must never be replayed.
        if not self.signed_in:
            raise Unavailable("Connect your Spotify account first")
        key = provider + ":" + path + "?" + urlencode(sorted((params or {}).items()))
        cached = None if self.bypass_response_cache else self.cached(key)
        if cached is not None and not refresh:
            return cached
        try:
            # Serialize provider requests across Kodi invocations. A second
            # window must observe a 429 before it starts another request.
            self.db.execute("BEGIN IMMEDIATE")
            error_gate = provider + ":" + path.strip("/").split("/")[0]
            remaining = max(self.remaining(provider), self.remaining(error_gate))
            if remaining:
                raise Unavailable(f"{provider}: retry in {max(1, (remaining + 59) // 60)} min")
            headers = {}
            token = self.token()
            if not token:
                raise Unavailable("Connect your Spotify account first")
            headers["Authorization"] = "Bearer " + token
            base = "https://api.spotify.com/v1"
            try:
                response = self.http.get(base + path, params=params, headers=headers, timeout=(3, 10))
                if response.status_code == 429:
                    try:
                        delay = max(1, int(response.headers.get("Retry-After", "300")))
                    except ValueError:
                        delay = 300
                    self.db.execute("INSERT OR REPLACE INTO gates VALUES (?,?)", (provider, self.clock() + delay))
                    self.db.commit()
                    raise Unavailable(f"{provider}: temporarily rate limited")
                if response.status_code == 403:
                    raise Forbidden("Spotify refused this request (HTTP 403)")
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or data.get("success") is False:
                    raise ValueError("provider returned an unsuccessful response")
            except (requests.RequestException, ValueError) as exc:
                self.db.execute("INSERT OR REPLACE INTO gates VALUES (?,?)", (error_gate, self.clock() + 30))
                self.db.commit()
                raise Unavailable(f"{provider}: service unavailable") from exc
            self.db.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?)", (key, json.dumps(data), self.clock() + 600))
            self.db.commit()
            return data
        except Unavailable as exc:
            self.db.rollback()
            cached = None if self.bypass_response_cache else self.cached(key, stale=True)
            if cached is not None:
                self.notice = "Cached results - " + str(exc)
                return cached
            raise
        finally:
            if self.db.in_transaction:
                self.db.rollback()

    def search(self, query, kind, offset=0):
        if kind not in ("track", "album", "artist", "playlist"):
            raise ValueError("Unknown search category")
        data = self.request("spotify", "/search", dict(q=query, type=kind, limit=10, offset=offset))
        page = data.get(kind + "s") or {}
        return [normalize(i, kind) for i in page.get("items", []) if i], offset + 10 if page.get("next") else None

    def playlists(self, offset=0):
        # Playlists are served from the previously imported list; the Spotify
        # Web API is not used for this listing. The imported list is account
        # data, so it stays hidden until the user is signed in again.
        if not self.signed_in:
            raise Unavailable("Connect your Spotify account first")
        items = self.cached("imported-playlists", stale=True) or []
        if not items and self.refresh_playlists():
            items = self.cached("imported-playlists", stale=True) or []
        if not items:
            raise Unavailable("No imported Spotify playlists are available yet")
        return items[offset:offset + 40], offset + 40 if offset + 40 < len(items) else None

    def tracks(self, kind, identity, offset=0):
        if not re.fullmatch(r"[A-Za-z0-9]{22}", identity):
            raise ValueError("Invalid Spotify ID")
        if kind not in ("playlist", "album", "artist"):
            raise ValueError("Unknown collection")
        suffix = "items" if kind == "playlist" else "tracks" if kind == "album" else "albums"
        # Spotify caps artist album pages at 10 results; larger limits are
        # rejected with 400 "Invalid limit".
        limit = 10 if kind == "artist" else 40
        try:
            data = self.request("spotify", f"/{kind}s/{identity}/{suffix}", dict(limit=limit, offset=offset))
        except Forbidden:
            if kind == "playlist":
                raise Unavailable(
                    "Spotify does not allow this app to read this playlist because it is "
                    "not owned by your account and you are not a collaborator. Open it in "
                    "Spotify and make your own copy of the playlist, then refresh playlists "
                    "in Resonance."
                )
            raise
        items = data.get("items") or []
        if kind == "playlist":
            items = [i.get("track") or i.get("item") for i in items if i]
        album = self.cached("collection:album:" + identity, stale=True) if kind == "album" else None
        return [
            normalize(i, "album" if kind == "artist" else "track", album=album)
            for i in items
            if i
        ], offset + limit if data.get("next") else None

    def track(self, identity):
        if not re.fullmatch(r"[A-Za-z0-9]{22}", identity):
            raise ValueError("Invalid Spotify ID")
        known = self.cached("track:" + identity, stale=True)
        if known and known.get("duration_ms") and known.get("is_playable") is not None:
            return known
        result = self.request("spotify", "/tracks/" + identity)
        normalized = normalize(result)
        if normalized.get("is_playable") is not None:
            self.save("track:" + identity, normalized, ttl=86400)
        return normalized

    def unplayable(self, track):
        """Return a playback-blocking reason, or empty when safe to stream."""
        if track.get("is_playable") is False:
            if track.get("restrictions"):
                return "This track is not available for playback in your region"
            return "This track is not available on this account"
        if track.get("is_playable") is True:
            return ""
        row = self.db.execute(
            "SELECT value, expires FROM pages WHERE key=?",
            ("tfail:" + str(track.get("id") or ""),),
        ).fetchone()
        if row and row[1] > self.clock():
            try:
                return (json.loads(row[0]) or {}).get("reason") or "This track previously failed to stream on this device"
            except (ValueError, TypeError):
                return "This track previously failed to stream on this device"
        return ""

    def metadata(self, track):
        track = dict(track)
        known = self.cached("metadata:" + str(track.get("id")), stale=True) or {}
        album = dict(track.get("album") or {})
        for key, old_key in (("release_date", "release_date"), ("name", "album_name"), ("id", "album_id")):
            if not album.get(key) and known.get(old_key):
                album[key] = known[old_key]
        track["album"] = album
        return track

    def import_playlists(self):
        """One-time read-only migration of known playlist IDs, not old runtime state."""
        if not self.signed_in:
            return
        if self.cached("migration-complete", stale=True):
            return
        try:
            with open(os.path.join(self.directory, "spotify-data-cache.json"), encoding="utf-8") as stream:
                legacy = json.load(stream)
            for key, value in legacy.items():
                if key.startswith("spotify.publictrack.v1.") and isinstance(value, dict):
                    self.save("metadata:" + key.rsplit(".", 1)[-1], value)
        except (OSError, ValueError, AttributeError):
            pass
        path = os.path.join(self.directory, "simplecache.db")
        if os.path.exists(path):
            with sqlite3.connect("file:" + path + "?mode=ro", uri=True) as old:
                rows = old.execute("SELECT data FROM simplecache WHERE id LIKE 'spotify.publicprofile.playlists.v2.%'").fetchall()
            items = {}
            for row in rows:
                try:
                    data = json.loads(row[0])
                except ValueError:
                    data = ast.literal_eval(row[0])
                if isinstance(data, dict):
                    data = data.get("items") or []
                for item in data:
                    if isinstance(item, dict) and item.get("id"):
                        items[item["id"]] = normalize(item, "playlist")
            # Never clobber a fresher list written by the background refresh.
            if items and not self.cached("imported-playlists", stale=True):
                self.save("imported-playlists", list(items.values()))
        self.save("migration-complete", True)

    def __web_api_playlists(self, pages=5):
        """Page the account's playlists through the official Spotify Web API.

        Uses the same browser-authorization token as the rest of the
        catalogue. The legacy api-partner GraphQL route rejects that token
        (403), so the supported ``/me/playlists`` endpoint is used instead;
        it also includes private playlists via playlist-read-private.
        """
        rows, seen, offset = [], set(), 0
        for _ in range(max(1, int(pages))):
            try:
                data = self.request(
                    "spotify", "/me/playlists", dict(limit=50, offset=offset), refresh=True
                )
            except Unavailable:
                return rows
            items = data.get("items") or []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                identity = str((item or {}).get("id") or "")
                if not re.fullmatch(r"[A-Za-z0-9]{22}", identity) or identity in seen:
                    continue
                seen.add(identity)
                rows.append(normalize(item, "playlist"))
            offset += len(items)
            if not data.get("next") or len(items) < 50:
                break
        return rows

    def refresh_playlists(self, pages=5):
        """Replace the cached playlist listing from the official Web API.

        Called from the service in the background. Any failure (signed out,
        no token, rate limit) leaves the existing cache untouched so browsing
        keeps working, and nothing is ever raised at the caller.
        """
        if not self.signed_in:
            return False
        try:
            rows = self.__web_api_playlists(pages)
            if not rows:
                return False
            self.save("imported-playlists", rows, ttl=86400)
            self.save("playlists-refreshed", {"at": self.clock(), "count": len(rows)}, ttl=86400)
        except Exception:
            return False
        return True

    def close(self):
        self.db.close()
        self.http.close()
