"""Music catalogue with persistent pages and shared provider cooldowns."""

import ast
import json
import os
import re
import sqlite3
import time
from urllib.parse import urlencode


def _localized(string_id, default):
    try:
        import xbmcaddon
        return xbmcaddon.Addon(id="plugin.audio.resonance").getLocalizedString(string_id) or default
    except Exception:
        return default



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
        name=item.get("name") or item.get("title") or _localized(11576, "Untitled"),
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
                 bypass_response_cache=False, hide_unreadable_playlists=False,
                 spotify_user_id=""):
        self.directory = directory
        self.token = token
        # Most browse and play invocations use the persistent catalogue cache.
        # Keep those fast on Kodi's constrained ARM devices by loading the HTTP
        # stack only when a Spotify API request is actually needed.
        self.http = session
        self.clock = clock
        self.signed_in = bool(signed_in)
        self.bypass_response_cache = bypass_response_cache
        self.hide_unreadable_playlists = bool(hide_unreadable_playlists)
        self.spotify_user_id = str(spotify_user_id or "")
        self.notice = ""
        os.makedirs(directory, exist_ok=True)
        self.db = sqlite3.connect(database_path(directory), timeout=10)
        self.db.execute("CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, value TEXT, expires REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS gates (provider TEXT PRIMARY KEY, until REAL)")
        self.db.commit()
        if not self.spotify_user_id:
            profile = self.cached("spotify:/me?", stale=True) or {}
            self.spotify_user_id = str(profile.get("id") or "")

    def cached(self, key, stale=False):
        row = self.db.execute("SELECT value, expires FROM pages WHERE key=?", (key,)).fetchone()
        if row and (stale or row[1] > self.clock()):
            return json.loads(row[0])
        return None

    def save(self, key, value, ttl=600):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?)", (key, json.dumps(value), self.clock() + ttl))
        return value

    def _playlist_is_likely_readable(self, item):
        owner = (item or {}).get("owner") or {}
        owner_id = str(owner.get("id") or "") if isinstance(owner, dict) else ""
        if not owner_id:
            return True
        if (item or {}).get("collaborative"):
            return True
        if not self.spotify_user_id:
            return True
        return owner_id == self.spotify_user_id

    def _visible_playlists(self, items):
        if not self.hide_unreadable_playlists:
            return items
        return [item for item in items if self._playlist_is_likely_readable(item)]

    def _ensure_spotify_user_id(self):
        if self.spotify_user_id:
            return self.spotify_user_id
        cached = self.cached("spotify:/me?", stale=True) or {}
        self.spotify_user_id = str(cached.get("id") or "")
        if self.spotify_user_id:
            return self.spotify_user_id
        try:
            profile = self.request("spotify", "/me", refresh=True)
        except Unavailable:
            return ""
        self.spotify_user_id = str((profile or {}).get("id") or "")
        return self.spotify_user_id

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
        import requests

        if provider != "spotify":
            raise ValueError("Unknown catalogue provider")
        if self.http is None:
            self.http = requests.Session()
        # Account requests (and their cached responses) belong to the signed-in
        # user. After a logout/account switch they must never be replayed.
        if not self.signed_in:
            raise Unavailable(_localized(11592, "Connect your Spotify account first"))
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
                raise Unavailable(_localized(11602, "{}: retry in {} min").format(provider, max(1, (remaining + 59) // 60)))
            headers = {}
            token = self.token()
            if not token:
                raise Unavailable(_localized(11592, "Connect your Spotify account first"))
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
                    raise Unavailable(_localized(11603, "{}: temporarily rate limited").format(provider))
                if response.status_code == 403:
                    raise Forbidden("Spotify refused this request (HTTP 403)")
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict):
                    if data.get("success") is False:
                        raise ValueError("provider returned an unsuccessful response")
                elif not isinstance(data, list):
                    # Spotify answers some endpoints with a bare JSON array.
                    raise ValueError("provider returned an unsuccessful response")
            except (requests.RequestException, ValueError) as exc:
                self.db.execute("INSERT OR REPLACE INTO gates VALUES (?,?)", (error_gate, self.clock() + 30))
                self.db.commit()
                raise Unavailable(_localized(11604, "{}: service unavailable").format(provider)) from exc
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

    def write_request(self, method, path, params=None, body=None):
        """Send an authenticated Spotify write request without caching it."""
        import requests

        if not self.signed_in:
            raise Unavailable(_localized(11592, "Connect your Spotify account first"))
        if self.http is None:
            self.http = requests.Session()
        token = self.token()
        if not token:
            raise Unavailable(_localized(11592, "Connect your Spotify account first"))
        headers = {"Authorization": "Bearer " + token}
        try:
            response = self.http.request(
                method, "https://api.spotify.com/v1" + path,
                params=params, json=body, headers=headers, timeout=(3, 10),
            )
            if response.status_code == 429:
                try:
                    delay = max(1, int(response.headers.get("Retry-After", "300")))
                except (TypeError, ValueError):
                    delay = 300
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO gates VALUES (?,?)", ("spotify", self.clock() + delay))
                raise Unavailable(_localized(11593, "Spotify is temporarily rate limited"))
            if response.status_code == 403:
                raise Forbidden("Spotify refused this change")
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                return {}
        except (requests.RequestException, ValueError) as exc:
            raise Unavailable(_localized(11594, "Spotify could not complete this change")) from exc

    def save_library_item(self, kind, identity, save=True):
        if kind not in ("track", "album", "playlist", "artist") or not re.fullmatch(r"[A-Za-z0-9]{22}", identity):
            raise ValueError("Invalid Spotify library item")
        uri = f"spotify:{kind}:{identity}"
        self.write_request("PUT" if save else "DELETE", "/me/library", params={"uris": uri})

    def add_track_to_playlist(self, playlist_id, track_id):
        if not re.fullmatch(r"[A-Za-z0-9]{22}", playlist_id) or not re.fullmatch(r"[A-Za-z0-9]{22}", track_id):
            raise ValueError("Invalid playlist or track ID")
        return self.write_request(
            "POST", f"/playlists/{playlist_id}/items",
            body={"uris": [f"spotify:track:{track_id}"]},
        )

    def remove_track_from_playlist(self, playlist_id, track_id):
        """Remove one track from a playlist this account owns."""
        if not re.fullmatch(r"[A-Za-z0-9]{22}", playlist_id) or not re.fullmatch(r"[A-Za-z0-9]{22}", track_id):
            raise ValueError("Invalid playlist or track ID")
        return self.write_request(
            "DELETE", f"/playlists/{playlist_id}/items",
            body={"items": [{"uri": f"spotify:track:{track_id}"}]},
        )

    def playlist_edit_access(self, playlist_id, owner_id=""):
        """Return whether the account may change the items of a playlist.

        ``owner_id`` is the owner that came with the playlist row, so the
        common case needs no request at all. Without it the playlist itself
        is read once. Collaborative playlists count as editable, because the
        account can be a collaborator of them.
        """
        if not re.fullmatch(r"[A-Za-z0-9]{22}", playlist_id or ""):
            return False
        user_id = str(self._ensure_spotify_user_id() or "")
        if owner_id:
            return bool(user_id) and str(owner_id) == user_id
        try:
            data = self.request("spotify", f"/playlists/{playlist_id}")
        except Unavailable:
            return False
        owner = data.get("owner") or {}
        owner = str(owner.get("id") or "") if isinstance(owner, dict) else ""
        if owner and user_id and owner == user_id:
            return True
        return bool(data.get("collaborative"))

    def create_playlist(self, name, description="", public=False):
        """Create a playlist owned by the signed-in account.

        Spotify returns the new playlist, so the caller can add tracks to it
        right away.
        """
        title = str(name or "").strip()
        if not title:
            raise ValueError(_localized(11596, "A playlist name is required"))
        body = {"name": title, "public": bool(public)}
        if description:
            body["description"] = str(description)
        return self.write_request("POST", "/me/playlists", body=body)

    def library_contains(self, uris):
        """Return the subset of ``uris`` that is in the account library.

        Spotify answers with a JSON array of booleans, one per requested URI.
        ``None`` is returned when the library could not be read, so callers can
        tell "not saved" apart from "unknown" and offer both actions.
        """
        wanted = [str(uri) for uri in uris if str(uri or "")]
        if not wanted:
            return set()
        if not self.signed_in:
            # Without an account the library state is unknown, not empty.
            return None
        found = set()
        for start in range(0, len(wanted), 40):
            chunk = wanted[start:start + 40]
            try:
                data = self.request("spotify", "/me/library/contains", {"uris": ",".join(chunk)})
            except Unavailable:
                return None
            flags = data if isinstance(data, list) else []
            for uri, flag in zip(chunk, flags):
                if flag:
                    found.add(uri)
        return found

    def liked_track_ids(self, identities):
        """Return the ids from ``identities`` that are in Liked Songs."""
        ids = [str(identity) for identity in identities
               if re.fullmatch(r"[A-Za-z0-9]{22}", str(identity or ""))]
        found = self.library_contains("spotify:track:" + identity for identity in ids)
        if found is None:
            return None
        return {uri.rsplit(":", 1)[-1] for uri in found}

    def saved_album_ids(self, identities):
        """Return the ids from ``identities`` that are in the saved albums."""
        ids = [str(identity) for identity in identities
               if re.fullmatch(r"[A-Za-z0-9]{22}", str(identity or ""))]
        found = self.library_contains("spotify:album:" + identity for identity in ids)
        if found is None:
            return None
        return {uri.rsplit(":", 1)[-1] for uri in found}

    def saved_artist_ids(self, identities):
        """Return the ids from ``identities`` that the account follows."""
        ids = [str(identity) for identity in identities
               if re.fullmatch(r"[A-Za-z0-9]{22}", str(identity or ""))]
        found = self.library_contains("spotify:artist:" + identity for identity in ids)
        if found is None:
            return None
        return {uri.rsplit(":", 1)[-1] for uri in found}

    def search(self, query, kind, offset=0):
        if kind == "playlist":
            # Spotify only returns playlist items to the owner and collaborators.
            # A playlist search could therefore only list playlists that
            # Resonance cannot open, so the category is not offered.
            raise ValueError(
                "Playlist search is unavailable. Open playlists from Your Spotify Library."
            )
        if kind not in ("track", "album", "artist"):
            raise ValueError("Unknown search category")
        data = self.request("spotify", "/search", dict(q=query, type=kind, limit=10, offset=offset))
        page = data.get(kind + "s") or {}
        return [normalize(i, kind) for i in page.get("items", []) if i], offset + 10 if page.get("next") else None

    def playlists(self, offset=0):
        # Playlist rows are served from the imported cache. Load the account
        # ID once when needed so followed playlists can be clearly marked.
        if not self.signed_in:
            raise Unavailable(_localized(11592, "Connect your Spotify account first"))
        if not self.spotify_user_id:
            self._ensure_spotify_user_id()
        items = self.cached("imported-playlists", stale=True) or []
        if not items and self.refresh_playlists():
            items = self.cached("imported-playlists", stale=True) or []
        items = self._visible_playlists(items)
        if not items:
            raise Unavailable(_localized(11595, "No imported Spotify playlists are available yet"))
        return items[offset:offset + 40], offset + 40 if offset + 40 < len(items) else None

    def liked_songs(self, offset=0):
        """Return a page of the current user's saved tracks (Liked Songs)."""
        if not self.signed_in:
            raise Unavailable(_localized(11592, "Connect your Spotify account first"))
        params = dict(limit=50, offset=offset)
        try:
            data = self.request("spotify", "/me/tracks", params)
        except Forbidden as exc:
            raise Unavailable(_localized(11599, "Spotify has not granted Resonance permission to read Liked Songs.\n\nSign in again from Account and status to approve the library permission.")) from exc
        items = data.get("items") or []
        tracks = [normalize(item.get("track") or {}, "track") for item in items if item]
        more = offset + len(items) if data.get("next") else None
        return tracks, more

    def saved_albums(self, offset=0):
        """Return a page of albums saved in the current user's library."""
        if not self.signed_in:
            raise Unavailable(_localized(11592, "Connect your Spotify account first"))
        data = self.request("spotify", "/me/albums", {"limit": 50, "offset": offset})
        items = data.get("items") or []
        albums = [normalize(item.get("album") or {}, "album") for item in items if item]
        return albums, (offset + len(items) if data.get("next") else None)

    def followed_artists(self, after=""):
        """Return a page of followed artists using Spotify's cursor pagination."""
        if not self.signed_in:
            raise Unavailable(_localized(11592, "Connect your Spotify account first"))
        params = {"type": "artist", "limit": 50}
        if after:
            params["after"] = after
        try:
            data = self.request("spotify", "/me/following", params)
        except Forbidden as exc:
            raise Unavailable(
                "Spotify has not granted Resonance permission to read followed artists.\n\n"
                "Reauthorize Spotify from Account and status to approve that permission."
            ) from exc
        artists_data = data.get("artists") or {}
        items = artists_data.get("items") or []
        next_cursor = ((items[-1].get("id") if items else "")
                       if artists_data.get("next") else None)
        return [normalize(item, "artist") for item in items if item], next_cursor

    def tracks(self, kind, identity, offset=0):
        if not re.fullmatch(r"[A-Za-z0-9]{22}", identity):
            raise ValueError("Invalid Spotify ID")
        if kind not in ("playlist", "album", "artist"):
            raise ValueError("Unknown collection")
        suffix = "items" if kind == "playlist" else "tracks" if kind == "album" else "albums"
        # Spotify caps artist album pages at 10 results; larger limits are
        # rejected with 400 "Invalid limit".
        limit = 10 if kind == "artist" else 40
        params = dict(limit=limit, offset=offset)
        page_key = "collection-page:" + kind + ":" + identity + ":" + str(offset)
        try:
            # Playlist contents are stable enough to reuse briefly. This also
            # lets a return visit render instantly while Spotify is slow.
            cached_page = self.cached(page_key) if kind == "playlist" else None
            if cached_page is not None:
                data = cached_page
            else:
                data = self.request("spotify", f"/{kind}s/{identity}/{suffix}", params)
                self.save(page_key, data, ttl=3600 if kind == "playlist" else 600)
        except Forbidden:
            if kind == "playlist":
                raise Unavailable(_localized(11601, "Spotify will not let Resonance read this playlist.\n\nReason: you do not own or collaborate on it.\n\nMake a copy in Spotify, then refresh playlists.\n\nYou can hide these in Settings > Catalogue."))
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

    def artist_top_tracks(self, identity, artist_name=""):
        if not re.fullmatch(r"[A-Za-z0-9]{22}", identity):
            raise ValueError("Invalid Spotify ID")
        try:
            data = self.request("spotify", f"/artists/{identity}/top-tracks", {"market": "from_token"})
            return [normalize(i, "track") for i in data.get("tracks", []) if i], None
        except Forbidden:
            if not artist_name:
                artist = self.request("spotify", f"/artists/{identity}")
                artist_name = str((artist or {}).get("name") or "")
            if not artist_name:
                raise
            data = self.request(
                "spotify",
                "/search",
                {"q": 'artist:"' + artist_name.replace('"', "") + '"', "type": "track", "limit": 10, "offset": 0},
                refresh=True,
            )
            page = data.get("tracks") or {}
            return [normalize(i, "track") for i in page.get("items", []) if i], None

    def artist_albums(self, identity, group="album", offset=0, limit=10):
        if not re.fullmatch(r"[A-Za-z0-9]{22}", identity):
            raise ValueError("Invalid Spotify ID")
        if group not in ("album", "single", "appears_on"):
            raise ValueError("Unknown artist album group")
        # Spotify rejects artist album pages with a limit above 10 with
        # "400 Invalid limit", so the page size is capped here.
        limit = min(max(1, int(limit)), 10)
        data = self.request(
            "spotify",
            f"/artists/{identity}/albums",
            {"include_groups": group, "limit": limit, "offset": offset},
        )
        return [normalize(i, "album") for i in data.get("items", []) if i], offset + limit if data.get("next") else None

    def artist_everything(self, identity, artist_name=""):
        sections = [
            ("Top songs", "track", self.artist_top_tracks(identity, artist_name)[0][:10]),
            ("Albums", "album", self.artist_albums(identity, "album", limit=10)[0]),
            ("Singles", "album", self.artist_albums(identity, "single", limit=10)[0]),
            ("Appears on", "album", self.artist_albums(identity, "appears_on", limit=10)[0]),
        ]
        return [(label, kind, items) for label, kind, items in sections if items]

    def track(self, identity):
        if not re.fullmatch(r"[A-Za-z0-9]{22}", identity):
            raise ValueError("Invalid Spotify ID")
        known = self.cached("track:" + identity, stale=True)
        if known and known.get("duration_ms"):
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
        if self.hide_unreadable_playlists:
            self._ensure_spotify_user_id()
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
        if self.http is not None:
            self.http.close()
