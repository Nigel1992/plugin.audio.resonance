import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

PATH = pathlib.Path(__file__).parents[1] / "resources/lib/catalogue.py"
spec = importlib.util.spec_from_file_location("catalogue", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise module.requests.HTTPError()

    def json(self):
        return self.data


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        pass


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def client(self, responses, signed_in=True, **kwargs):
        http = Session(responses)
        client = module.Catalogue(
            self.temp.name,
            lambda: "test",
            http,
            clock=lambda: 1000,
            signed_in=signed_in,
            **kwargs,
        )
        self.addCleanup(client.close)
        return client, http

    def test_retry_after_is_shared_between_instances(self):
        client, http = self.client([Response({}, 429, {"Retry-After": "600"})])
        with self.assertRaises(module.Unavailable):
            client.request("spotify", "/me/playlists")
        other, second_http = self.client([])
        with self.assertRaises(module.Unavailable):
            other.request("spotify", "/search")
        self.assertEqual(other.remaining("spotify"), 600)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(second_http.calls, [])

    def test_search_uses_spotify_web_api(self):
        client, http = self.client([Response({"tracks": {"items": []}})])
        self.assertEqual(client.search("query", "track"), ([], None))
        self.assertEqual(len(http.calls), 1)
        self.assertIn("api.spotify.com", http.calls[0][0])

    def test_search_failure_does_not_try_another_provider(self):
        client, http = self.client([Response({}, 500)])
        with self.assertRaises(module.Unavailable):
            client.search("query", "track")
        self.assertEqual(len(http.calls), 1)

    def test_cache_bypass_still_respects_rate_limit(self):
        client, http = self.client([Response({}, 429, {"Retry-After": "60"})])
        client.save("spotify:/search?", {"old": True})
        client.bypass_response_cache = True
        with self.assertRaises(module.Unavailable):
            client.request("spotify", "/search")
        with self.assertRaises(module.Unavailable):
            client.request("spotify", "/search")
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(client.cached("spotify:/search?"), {"old": True})

    def test_cache_avoids_network_and_survives_failure(self):
        client, http = self.client([Response({"items": [1]}), Response({}, 503)])
        self.assertEqual(client.request("spotify", "/test"), {"items": [1]})
        client.request("spotify", "/test")
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(client.request("spotify", "/test", refresh=True), {"items": [1]})
        self.assertIn("Cached", client.notice)

    def test_new_playlist_item_shape(self):
        client, _ = self.client([Response({"items": [{"item": {"id": "a" * 22, "name": "Song"}}, {"item": None}], "next": "next"})])
        items, more = client.tracks("playlist", "a" * 22)
        self.assertEqual(len(items), 1)
        self.assertEqual(more, 40)

    def test_legacy_playlist_migration_is_read_only(self):
        path = pathlib.Path(self.temp.name) / "simplecache.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE simplecache (id TEXT, data TEXT)")
            db.execute("INSERT INTO simplecache VALUES (?,?)", ("spotify.publicprofile.playlists.v2.user", repr([{"id": "a" * 22, "name": "My playlist"}])))
        before = path.read_bytes()
        client, _ = self.client([])
        client.import_playlists()
        self.assertEqual(len(client.cached("imported-playlists")), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_playlists_come_from_imported_cache_without_network(self):
        # A cached listing renders without touching the Spotify Web API.
        client, http = self.client([])
        client.save("imported-playlists", [{"id": "a" * 22, "name": "Mix"}], ttl=86400)
        items, more = client.playlists()
        self.assertEqual([item["name"] for item in items], ["Mix"])
        self.assertIsNone(more)
        self.assertEqual(http.calls, [])

    def test_playlists_without_cache_attempts_web_api_fetch(self):
        # With no imported list the Web API is consulted; failure surfaces as
        # the existing Unavailable message instead of a network error.
        client, http = self.client([Response({}, 503)])
        with self.assertRaises(module.Unavailable):
            client.playlists()
        self.assertEqual([call[0] for call in http.calls], ["https://api.spotify.com/v1/me/playlists"])

    def test_logged_out_hides_imported_playlists(self):
        # Imported playlists belong to the signed-in account and must not leak
        # into a logged-out session.
        client, http = self.client([], signed_in=False)
        client.save("imported-playlists", [{"id": "a" * 22, "name": "Mix"}], ttl=86400)
        with self.assertRaises(module.Unavailable):
            client.playlists()
        self.assertEqual(http.calls, [])

    def test_logged_out_never_replays_cached_spotify_pages(self):
        # A cached Spotify response from the previous account must not be served
        # once the credentials are gone.
        client, http = self.client([], signed_in=False)
        client.save("spotify:/search?", {"items": [{"id": "a" * 22}]}, ttl=86400)
        with self.assertRaises(module.Unavailable):
            client.request("spotify", "/search")
        self.assertEqual(http.calls, [])

    def test_logged_out_skips_playlist_import(self):
        path = pathlib.Path(self.temp.name) / "simplecache.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE simplecache (id TEXT, data TEXT)")
            db.execute("INSERT INTO simplecache VALUES (?,?)", ("spotify.publicprofile.playlists.v2.user", repr([{"id": "a" * 22, "name": "Old"}])))
        client, _ = self.client([], signed_in=False)
        client.import_playlists()
        self.assertIsNone(client.cached("imported-playlists", stale=True))

    def _credentials(self, username="user123"):
        path = pathlib.Path(self.temp.name) / "runtime"
        path.mkdir(exist_ok=True)
        (path / "credentials.json").write_text(json.dumps({"username": username}))

    def test_refresh_playlists_updates_cache_from_web_api(self):
        self._credentials()
        profile = {"items": [
            {"id": "a" * 22, "name": "Mix", "images": [{"url": "https://img/a"}], "owner": {"id": "user123"}},
            {"id": "b" * 22, "name": "B", "owner": {"id": "user123"}, "collaborative": True},
        ], "next": "more"}
        client, http = self.client([Response(profile)])
        self.assertTrue(client.refresh_playlists())
        items = client.cached("imported-playlists")
        self.assertEqual([item["id"] for item in items], ["a" * 22, "b" * 22])
        self.assertEqual(items[0]["images"], [{"url": "https://img/a"}])
        self.assertEqual(http.calls[0][0], "https://api.spotify.com/v1/me/playlists")
        self.assertEqual(http.calls[0][1]["headers"]["Authorization"], "Bearer test")
        self.assertEqual(http.calls[0][1]["params"], {"limit": 50, "offset": 0})
        self.assertEqual(client.cached("playlists-refreshed")["count"], 2)

    def test_refresh_keeps_followed_playlists_visible(self):
        # Spotify may list followed playlists that later 403 when opened. Keep
        # them visible and explain the API limitation only if the user opens one.
        self._credentials()
        profile = {"items": [
            {"id": "a" * 22, "name": "Mine", "owner": {"id": "user123"}},
            {"id": "b" * 22, "name": "Collaborative", "owner": {"id": "other"}, "collaborative": True},
            {"id": "c" * 22, "name": "Followed only", "owner": {"id": "other"}},
            {"id": "d" * 22, "name": "No owner info", "owner": {}},
        ], "next": None}
        client, _ = self.client([Response(profile)])
        self.assertTrue(client.refresh_playlists())
        labels = [item["name"] for item in client.cached("imported-playlists")]
        self.assertEqual(labels, ["Mine", "Collaborative", "Followed only", "No owner info"])

    def test_open_followed_playlist_gives_clear_message(self):
        # A 403 from /playlists/{id}/items must surface the playlist-specific
        # message, not a generic "service unavailable", and must not arm the
        # shared rate-limit gate that would block later requests.
        client, http = self.client([Response({}, 403)])
        with self.assertRaises(module.Unavailable) as raised:
            client.tracks("playlist", "a" * 22)
        self.assertIn("make your own copy", str(raised.exception))
        self.assertEqual(client.remaining("playlists"), 0)
        self.assertEqual(len(http.calls), 1)

    def test_cached_playlist_listing_keeps_followed_playlists_visible(self):
        client, http = self.client([])
        client.save("imported-playlists", [
            {"id": "a" * 22, "name": "Mine", "owner": {"id": "user123"}},
            {"id": "c" * 22, "name": "Followed only", "owner": {"id": "other"}},
        ], ttl=86400)
        items, more = client.playlists()
        self.assertEqual([item["name"] for item in items], ["Mine", "Followed only"])
        self.assertIsNone(more)
        self.assertEqual(http.calls, [])

    def test_refresh_playlists_keeps_cache_when_source_fails(self):
        self._credentials()
        client, _ = self.client([Response({}, 500)])
        client.save("imported-playlists", [{"id": "a" * 22, "name": "Old"}], ttl=86400)
        self.assertFalse(client.refresh_playlists())
        self.assertEqual([item["name"] for item in client.cached("imported-playlists")], ["Old"])

    def test_account_playlists_require_account_token(self):
        self._credentials()
        for token, responses in (("", []), ("expired", [Response({}, 401)])):
            with self.subTest(token=token):
                client, http = self.client(responses)
                client.token = lambda: token
                client.save("imported-playlists", [{"id": "a" * 22}])
                self.assertFalse(client.refresh_playlists())
                self.assertEqual(len(http.calls), 1 if token else 0)
                self.assertEqual(client.cached("imported-playlists"), [{"id": "a" * 22}])

    def test_empty_playlist_cache_is_refilled_when_opened(self):
        client, _ = self.client([])
        def refresh():
            client.save("imported-playlists", [{"id": "a" * 22}])
            return True
        with mock.patch.object(client, "refresh_playlists", side_effect=refresh):
            self.assertEqual(client.playlists(), ([{"id": "a" * 22}], None))

    def test_refresh_playlists_skips_when_signed_out(self):
        self._credentials()
        client, http = self.client([], signed_in=False)
        client.save("imported-playlists", [{"id": "a" * 22, "name": "Old"}], ttl=86400)
        self.assertFalse(client.refresh_playlists())
        self.assertEqual(http.calls, [])
        self.assertEqual([item["name"] for item in client.cached("imported-playlists")], ["Old"])

    def test_import_does_not_clobber_refreshed_playlists(self):
        path = pathlib.Path(self.temp.name) / "simplecache.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE simplecache (id TEXT, data TEXT)")
            db.execute("INSERT INTO simplecache VALUES (?,?)", ("spotify.publicprofile.playlists.v2.user", repr([{"id": "b" * 22, "name": "Legacy"}])))
        client, _ = self.client([])
        client.save("imported-playlists", [{"id": "a" * 22, "name": "Fresh"}], ttl=86400)
        client.import_playlists()
        self.assertEqual([item["name"] for item in client.cached("imported-playlists")], ["Fresh"])

    def test_existing_cooldown_is_preserved(self):
        (pathlib.Path(self.temp.name) / "spotify-api-gate.json").write_text(json.dumps({"retry_until": 1300}))
        client, http = self.client([])
        with self.assertRaises(module.Unavailable):
            client.request("spotify", "/search")
        self.assertEqual(http.calls, [])

    def test_album_metadata_does_not_duplicate_entire_tracklist(self):
        album = {"id": "b" * 22, "name": "Album", "tracks": [{"id": "a" * 22}], "release_date": "2008-06-12"}
        track = module.normalize({"id": "a" * 22, "artist": "Coldplay"}, album=album)
        self.assertNotIn("tracks", track["album"])
        self.assertEqual(track["album"]["release_date"], "2008-06-12")

    def test_unplayable_reason_requires_spotify_restrictions(self):
        self.assertNotEqual(
            module.unplayable_reason({"is_playable": False, "restrictions": {"reason": "market"}}),
            "",
        )
        for track in ({}, {"is_playable": False}, {"restrictions": {"reason": "market"}}, {"is_playable": True, "restrictions": {"reason": "market"}}):
            self.assertEqual(module.unplayable_reason(track), "")

    def test_cached_track_without_playability_is_reverified(self):
        cached_track = {"id": "a" * 22, "name": "Ghost", "duration_ms": 298344}
        spotify_track = {"id": "a" * 22, "name": "Ghost", "duration_ms": 298344, "is_playable": False, "restrictions": {"reason": "market"}}
        client, http = self.client([Response(spotify_track)])
        client.save("track:" + "a" * 22, cached_track, ttl=86400)
        result = client.track("a" * 22)
        self.assertEqual(http.calls[0][0], "https://api.spotify.com/v1/tracks/" + "a" * 22)
        self.assertEqual(result["is_playable"], False)
        self.assertEqual(client.cached("track:" + "a" * 22)["is_playable"], False)
        self.assertNotEqual(module.unplayable_reason(result), "")
        # A second play now short-circuits with the rejected entry.
        second = client.track("a" * 22)
        self.assertEqual(second["is_playable"], False)
        self.assertEqual(len(http.calls), 1)

    def test_remembered_play_failure_blocks_until_spotify_verdict(self):
        client, http = self.client([])
        module.remember_failed(self.temp.name, "a" * 22)
        # Entries without an availability flag are blocked if the streamer
        # already recorded a failure for the same track.
        ghost = {"id": "a" * 22, "name": "Ghost", "duration_ms": 298344}
        self.assertNotEqual(client.unplayable(ghost), "")
        # An authoritative playable verdict overrides the remembered failure.
        playable = dict(ghost, is_playable=True)
        self.assertEqual(client.unplayable(playable), "")

    def test_spotify_market_verdict_keeps_clear_message(self):
        client, http = self.client([])
        track = {"id": "a" * 22, "is_playable": False, "restrictions": {"reason": "market"}}
        self.assertEqual(client.unplayable(track), "This track is not available for playback in your region")

    def test_spotify_track_verification_short_circuits_playable_cache(self):
        cached = {"id": "a" * 22, "name": "Known", "duration_ms": 200000, "is_playable": True}
        client, http = self.client([Response({}, 400)])
        client.save("track:" + "a" * 22, cached, ttl=86400)
        result = client.track("a" * 22)
        self.assertEqual(result["is_playable"], True)
        self.assertEqual(http.calls, [])

    def test_artist_albums_use_reduced_spotify_limit(self):
        payload = {"items": [{"id": "b" * 22, "name": "Album"}], "next": "next"}
        client, http = self.client([Response(payload)])
        items, more = client.tracks("artist", "b" * 22)
        self.assertEqual(len(items), 1)
        self.assertEqual(more, 10)
        self.assertEqual(http.calls[0][1]["params"], {"limit": 10, "offset": 0})

    def test_album_tracks_keep_larger_spotify_limit(self):
        payload = {"items": [{"id": "c" * 22, "name": "Song"}], "next": "next"}
        client, http = self.client([Response(payload)])
        items, more = client.tracks("album", "e" * 22)
        self.assertEqual(len(items), 1)
        self.assertEqual(more, 40)
        self.assertEqual(http.calls[0][1]["params"], {"limit": 40, "offset": 0})

    def test_album_tracks_inherit_cached_album_art(self):
        album_id = "e" * 22
        client, http = self.client([Response({"items": [{"id": "c" * 22, "name": "Song"}]})])
        client.save(
            "collection:album:" + album_id,
            {"id": album_id, "name": "Album", "images": [{"url": "https://img/album"}]},
            ttl=86400,
        )
        items, more = client.tracks("album", album_id)
        self.assertIsNone(more)
        self.assertEqual(items[0]["images"], [{"url": "https://img/album"}])
        self.assertEqual(items[0]["album"]["images"], [{"url": "https://img/album"}])

if __name__ == "__main__":
    unittest.main()
