"""Menu and context menu checks for the Resonance plugin entry point.

``plugin.py`` needs Kodi's ``xbmc*`` modules, so they are stubbed here. The
stubs are installed only while the plugin module is imported; the plugin keeps
its own references to them afterwards.
"""
import contextlib
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).parents[1]
PROFILE = tempfile.mkdtemp(prefix="resonance-menu-test-")
ADDED = []
ITEMS = []
FOLDERS = []
DIALOGS = []
ADDON_SETTINGS = {}


def _addon_version():
    """Read the version from addon.xml, like Kodi does."""
    text = (ROOT / "addon.xml").read_text(encoding="utf-8")
    match = re.search(r'version="([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


VERSION = _addon_version()


def _stub(**attrs):
    module = types.ModuleType("stub")
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


class _MusicInfoTag:
    def setTitle(self, *args, **kwargs):
        pass

    def setArtist(self, *args, **kwargs):
        pass

    def setAlbum(self, *args, **kwargs):
        pass

    def setDuration(self, *args, **kwargs):
        pass

    def setYear(self, *args, **kwargs):
        pass


class _ListItem:
    def __init__(self, label=None, path=None, **kwargs):
        self.label = label
        self.art = {}
        self.context = []

    def setArt(self, art):
        self.art = art

    def setProperty(self, *args, **kwargs):
        pass

    def addContextMenuItems(self, items=(), *args, **kwargs):
        self.context.extend(list(items))

    def getMusicInfoTag(self):
        return _MusicInfoTag()


def _add_directory_item(handle, url, item, is_folder):
    ADDED.append((item.label, url, item.art))
    ITEMS.append(item)
    FOLDERS.append(is_folder)


def _translate(path):
    if path.startswith("special://"):
        return os.path.join(PROFILE, path.replace("special://", "").replace("/", "_"))
    return path


class _Dialog:
    """Stand-in for xbmcgui.Dialog that records the shown windows."""

    def select(self, *args, **kwargs):
        return -1

    def yesno(self, *args, **kwargs):
        return False

    def ok(self, *args, **kwargs):
        return None

    def textviewer(self, heading, text="", *args, **kwargs):
        DIALOGS.append((heading, text))
        return None

    def notification(self, *args, **kwargs):
        return None


def _english_strings():
    """Read the English source strings so the tests see the real labels."""
    texts, sid = {}, None
    path = ROOT / "resources" / "language" / "resource.language.en_gb" / "strings.po"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith('msgctxt "#'):
            sid = int(line[len('msgctxt "#'):-1])
        elif line.startswith("msgid ") and sid is not None:
            texts[sid] = json.loads(line[len("msgid "):])
    return texts


ENGLISH = _english_strings()


def _load_plugin():
    stubs = {
        "xbmc": _stub(
            LOGDEBUG=0, LOGINFO=0, LOGERROR=0, LOGWARNING=0,
            log=lambda *a, **k: None,
            executebuiltin=lambda *a, **k: None,
            getInfoLabel=lambda *a, **k: "",
            sleep=lambda *a, **k: None,
        ),
        "xbmcaddon": _stub(Addon=lambda *a, **k: types.SimpleNamespace(
            getAddonInfo=lambda key: PROFILE if key == "profile" else (VERSION if key == "version" else str(ROOT)),
            getSetting=lambda key: ADDON_SETTINGS.get(key, ""),
            getLocalizedString=lambda sid: ENGLISH.get(sid, "L%s" % sid),
            setSetting=lambda key, value: ADDON_SETTINGS.__setitem__(key, value),
        )),
        "xbmcgui": _stub(
            ListItem=_ListItem,
            Dialog=_Dialog,
            Window=lambda *a, **k: types.SimpleNamespace(
                getProperty=lambda *a, **k: "",
                setProperty=lambda *a, **k: None,
                clearProperty=lambda *a, **k: None,
            ),
        ),
        "xbmcplugin": _stub(
            addDirectoryItem=_add_directory_item,
            endOfDirectory=lambda *a, **k: None,
            setContent=lambda *a, **k: None,
            setProperty=lambda *a, **k: None,
            setResolvedUrl=lambda *a, **k: None,
            resolveUrl=lambda *a, **k: None,
        ),
        "xbmcvfs": _stub(translatePath=_translate),
    }
    saved = {name: sys.modules.get(name) for name in stubs}
    argv = sys.argv
    sys.modules.update(stubs)
    sys.argv = ["plugin.py", "1", "?action=home"]
    try:
        spec = importlib.util.spec_from_file_location("resonance_plugin", ROOT / "plugin.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.argv = argv
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@contextlib.contextmanager
def _stub_utils(signed_in=True):
    """Serve the plugin a predictable utils module for the menu actions."""
    module = types.ModuleType("utils")
    module.migrate_legacy_addon_data = lambda: None
    module.resonance_signed_in = lambda: signed_in
    module.spotty_has_credentials = lambda: True
    module.get_playback_settings = lambda: ("320", "1.0", "track")
    module.get_spotify_device_name = lambda: "Kodi"
    module.get_catalogue_settings = lambda: {}
    module.get_cached_auth_token = lambda: ""
    module.get_spotify_webapi_client_id = lambda: ""
    saved = sys.modules.get("utils")
    sys.modules["utils"] = module
    try:
        yield
    finally:
        if saved is None:
            sys.modules.pop("utils", None)
        else:
            sys.modules["utils"] = saved


class MenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT))
        cls.plugin = _load_plugin()

    def menu(self, query, signed_in=True):
        ADDED.clear()
        ITEMS.clear()
        FOLDERS.clear()
        with _stub_utils(signed_in):
            self.plugin.PARAMS = dict(self.plugin.parse_qsl(query.lstrip("?")))
            self.plugin.run()
        return list(ADDED)

    @staticmethod
    def labels(rows):
        return [label for label, _, _ in rows]

    @staticmethod
    def icons(rows):
        return [art.get("icon") for _, _, art in rows]

    def test_menu_icons_are_bundled_files(self):
        for action, name in self.plugin.MENU_ICONS.items():
            path = self.plugin.icon_path(name)
            self.assertTrue(os.path.isfile(path), action + " -> " + name)

    def test_main_menu_rows_use_their_own_icon(self):
        rows = self.menu("?action=home")
        self.assertTrue(rows)
        icons = self.icons(rows)
        for icon in icons:
            self.assertTrue(os.path.isfile(icon), icon)
            self.assertIn(os.path.join("resources", "media"), icon)
        self.assertEqual(len(set(icons)), len(icons))

    def test_main_menu_has_no_spotify_link_row(self):
        rows = self.menu("?action=home")
        self.assertNotIn("Open Spotify link", self.labels(rows))
        self.assertFalse([url for _, url, _ in rows if "action=open_link" in url])

    def test_search_menu_has_no_playlist_row(self):
        rows = self.menu("?action=search_menu")
        self.assertEqual(self.labels(rows), ["Songs", "Albums", "Artists"])
        icons = self.icons(rows)
        self.assertEqual(len(set(icons)), len(icons))

    def test_library_menu_rows_use_their_own_icon(self):
        rows = self.menu("?action=spotify_library_menu")
        self.assertEqual(
            self.labels(rows),
            ["Liked Songs", "Saved Albums", "Followed Artists",
             "Saved and Followed Playlists", "Create playlist..."],
        )
        icons = self.icons(rows)
        self.assertEqual(len(set(icons)), len(icons))

    def test_library_menu_can_create_a_playlist(self):
        rows = self.menu("?action=spotify_library_menu")
        create = [url for label, url, _ in rows if label == "Create playlist..."]
        self.assertTrue(create)
        self.assertIn("action=create_playlist", create[0])

    def test_account_page_is_grouped_into_sections(self):
        rows = self.menu("?action=status")
        labels = self.labels(rows)
        for heading in ("[ACCOUNT]", "[CATALOGUE]", "[PLAYBACK]", "[MAINTENANCE]"):
            self.assertTrue(any(heading in label for label in labels), heading)
        self.assertTrue(any("Sign out" in label for label in labels))
        self.assertTrue(any("Clear cache" in label for label in labels))

    def test_account_page_rows_use_their_own_icon(self):
        rows = self.menu("?action=status")
        icons = self.icons(rows)
        for icon in icons:
            self.assertTrue(os.path.isfile(icon), icon)
        self.assertGreater(len(set(icons)), 4)

    def test_account_page_can_show_the_release_notes(self):
        rows = self.menu("?action=status")
        labels = self.labels(rows)
        self.assertTrue(any("What's new" in label for label in labels))
        notes = [url for label, url, _ in rows if "What's new" in label]
        self.assertIn("action=whats_new", notes[0])

    def test_playback_status_only_reacts_on_quality_and_connect(self):
        rows = self.menu("?action=playback_status")
        clickable = [(label, url) for (label, url, _), folder in zip(rows, FOLDERS) if folder]
        self.assertEqual(
            [url for _, url in clickable],
            [self.plugin.url(action="playback_quality"),
             self.plugin.url(action="connect_playback")],
        )

    def test_information_rows_cannot_be_clicked(self):
        rows = self.menu("?action=playback_status")
        for (label, url, _), folder in zip(rows, FOLDERS):
            if folder:
                continue
            # A row without a path makes Kodi try to play it and answer with
            # a "playback failed" message, so it points at the action that
            # does nothing instead.
            self.assertIn("action=noop", url, label)

    def test_release_notes_are_shown_after_an_update(self):
        ADDON_SETTINGS.pop("last_notes_version", None)
        DIALOGS.clear()
        handle = self.plugin.HANDLE
        self.plugin.HANDLE = -1
        try:
            self.menu("?action=home")
        finally:
            self.plugin.HANDLE = handle
        self.assertTrue(DIALOGS)
        heading, notes = DIALOGS[-1]
        self.assertIn(VERSION, heading)
        self.assertIn("What's new", heading)
        self.assertTrue(notes.strip())
        self.assertEqual(ADDON_SETTINGS.get("last_notes_version"), VERSION)

    def test_release_notes_are_shown_only_once_per_version(self):
        ADDON_SETTINGS["last_notes_version"] = VERSION
        DIALOGS.clear()
        self.menu("?action=home")
        self.assertEqual(DIALOGS, [])

    def test_context_menu_entry_without_a_handle_still_runs_the_action(self):
        # Kodi runs a context menu entry as a script and hands over the
        # handle -1. The action still has to run, only the listing is
        # skipped, otherwise "Add to Spotify playlist" and "Create playlist
        # with this track" would do nothing at all.
        ADDED.clear()
        FOLDERS.clear()
        DIALOGS.clear()
        handle = self.plugin.HANDLE
        self.plugin.HANDLE = -1
        try:
            with _stub_utils():
                self.plugin.PARAMS = dict(self.plugin.parse_qsl("action=whats_new"))
                self.plugin.run()
        finally:
            self.plugin.HANDLE = handle
        self.assertTrue(DIALOGS)
        self.assertEqual(ADDED, [])


class ContextMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT))
        cls.plugin = _load_plugin()

    @staticmethod
    def labels(actions):
        return [label for label, _ in actions]

    def test_like_is_hidden_for_a_liked_track(self):
        entry = {"id": "a" * 22, "name": "Song"}
        labels = self.labels(self.plugin.spotify_write_context_menu(entry, "track", liked={"a" * 22}))
        self.assertNotIn("Like on Spotify", labels)
        self.assertIn("Unlike on Spotify", labels)

    def test_unlike_is_hidden_for_a_track_that_is_not_liked(self):
        entry = {"id": "b" * 22, "name": "Song"}
        labels = self.labels(self.plugin.spotify_write_context_menu(entry, "track", liked=set()))
        self.assertIn("Like on Spotify", labels)
        self.assertNotIn("Unlike on Spotify", labels)

    def test_both_like_options_are_offered_when_spotify_cannot_answer(self):
        entry = {"id": "c" * 22, "name": "Song"}
        labels = self.labels(self.plugin.spotify_write_context_menu(entry, "track", liked=None))
        self.assertIn("Like on Spotify", labels)
        self.assertIn("Unlike on Spotify", labels)

    def test_track_without_a_playlist_has_no_removal_action(self):
        entry = {"id": "d" * 22, "name": "Song"}
        actions = dict(self.plugin.spotify_write_context_menu(entry, "track", liked=set()))
        self.assertFalse([label for label in actions if label.startswith("Remove from")])

    def test_track_in_a_playlist_can_be_removed_from_it(self):
        entry = {"id": "d" * 22, "name": "Song"}
        playlist = ("p" * 22, "Road trip", True)
        actions = dict(self.plugin.playlist_remove_context_menu(entry, playlist))
        self.assertIn("Remove from Road trip", actions)
        action = actions["Remove from Road trip"]
        self.assertIn("action=remove_from_playlist", action)
        self.assertIn("playlist=" + "p" * 22, action)
        self.assertIn("id=" + "d" * 22, action)

    def test_track_in_a_read_only_playlist_has_no_removal_action(self):
        entry = {"id": "d" * 22, "name": "Song"}
        playlist = ("p" * 22, "Someone else", False)
        self.assertEqual(self.plugin.playlist_remove_context_menu(entry, playlist), [])
        self.assertEqual(self.plugin.playlist_remove_context_menu(entry, None), [])

    def test_track_can_start_a_new_playlist(self):
        entry = {"id": "d" * 22, "name": "Song"}
        actions = dict(self.plugin.spotify_write_context_menu(entry, "track", liked=set()))
        self.assertIn("Create playlist with this track...", actions)
        self.assertIn("action=create_playlist", actions["Create playlist with this track..."])

    def test_saved_album_hides_the_save_action(self):
        entry = {"id": "e" * 22, "name": "Album"}
        labels = self.labels(self.plugin.spotify_write_context_menu(entry, "album", saved={"e" * 22}))
        self.assertNotIn("Save album on Spotify", labels)
        self.assertIn("Remove album from Spotify library", labels)

    def test_followed_playlist_can_be_unfollowed(self):
        client = types.SimpleNamespace(spotify_user_id="u" * 22)
        entry = {"id": "p" * 22, "name": "Road trip", "owner": {"id": "o" * 22}}
        labels = self.labels(self.plugin.playlist_context_menu(entry, client))
        self.assertEqual(labels, ["Unfollow playlist"])

    def test_owned_playlist_has_no_unfollow_action(self):
        client = types.SimpleNamespace(spotify_user_id="o" * 22)
        entry = {"id": "p" * 22, "name": "Mine", "owner": {"id": "o" * 22}}
        self.assertEqual(self.plugin.playlist_context_menu(entry, client), [])


class _FakeClient:
    """Minimal catalogue stand-in for the rendering tests."""

    spotify_user_id = "u" * 22
    notice = ""

    def __init__(self, liked=None, saved=None, followed=None, editable=True):
        self.liked = liked
        self.saved = saved
        self.followed = followed
        self.editable = editable
        self.lookups = []
        self.edit_checks = []

    def playlist_edit_access(self, playlist_id, owner_id=""):
        self.edit_checks.append((playlist_id, owner_id))
        return self.editable

    def metadata(self, entry):
        return entry

    def save(self, *args, **kwargs):
        pass

    def cached(self, *args, **kwargs):
        return None

    def liked_track_ids(self, identities):
        self.lookups.append(list(identities))
        return self.liked

    def saved_album_ids(self, identities):
        self.lookups.append(list(identities))
        return self.saved

    def saved_artist_ids(self, identities):
        self.lookups.append(list(identities))
        return self.followed


class RenderTests(unittest.TestCase):
    """The rendered pages must show the right library actions."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT))
        cls.plugin = _load_plugin()

    def render(self, client, entries, kind, params):
        ADDED.clear()
        ITEMS.clear()
        self.plugin.render(client, entries, kind, None, params)
        return list(ITEMS)

    @staticmethod
    def context_labels(item):
        return [label for label, _ in item.context]

    def test_liked_track_only_offers_the_unlike_action(self):
        tracks = [{"id": "a" * 22, "name": "Liked"}, {"id": "b" * 22, "name": "Fresh"}]
        items = self.render(_FakeClient(liked={"a" * 22}), tracks, "track", dict(action="search"))
        self.assertNotIn("Like on Spotify", self.context_labels(items[0]))
        self.assertIn("Unlike on Spotify", self.context_labels(items[0]))
        self.assertIn("Like on Spotify", self.context_labels(items[1]))
        self.assertNotIn("Unlike on Spotify", self.context_labels(items[1]))

    def test_liked_songs_page_does_not_ask_spotify(self):
        tracks = [{"id": "a" * 22, "name": "Liked"}]
        client = _FakeClient(liked=None)
        items = self.render(client, tracks, "track", dict(action="liked_songs"))
        self.assertEqual(client.lookups, [])
        self.assertNotIn("Like on Spotify", self.context_labels(items[0]))
        self.assertIn("Unlike on Spotify", self.context_labels(items[0]))

    def test_saved_album_only_offers_the_remove_action(self):
        albums = [{"id": "c" * 22, "name": "Album"}]
        items = self.render(_FakeClient(saved={"c" * 22}), albums, "album", dict(action="search"))
        self.assertNotIn("Save album on Spotify", self.context_labels(items[0]))
        self.assertIn("Remove album from Spotify library", self.context_labels(items[0]))

    def test_followed_playlist_offers_the_unfollow_action(self):
        playlists = [{"id": "p" * 22, "name": "Road trip", "owner": {"id": "o" * 22}}]
        items = self.render(_FakeClient(), playlists, "playlist", dict(action="saved_playlists"))
        self.assertIn("Unfollow playlist", self.context_labels(items[0]))

    def test_followed_artist_offers_the_unfollow_action(self):
        artists = [{"id": "a" * 22, "name": "Coldplay"}]
        items = self.render(_FakeClient(followed={"a" * 22}), artists, "artist", dict(action="followed_artists"))
        self.assertIn("Unfollow artist - Coldplay", self.context_labels(items[0]))

    def test_artist_that_is_not_followed_has_no_unfollow_action(self):
        artists = [{"id": "a" * 22, "name": "Coldplay"}]
        items = self.render(_FakeClient(followed=set()), artists, "artist", dict(action="search"))
        self.assertFalse([label for label in self.context_labels(items[0]) if "Unfollow" in label])

    def test_track_in_an_owned_playlist_can_be_removed_from_it(self):
        tracks = [{"id": "t" * 22, "name": "Song"}]
        client = _FakeClient(editable=True)
        params = dict(action="collection", kind="playlist", id="p" * 22,
                      name="Road trip", owner="u" * 22)
        items = self.render(client, tracks, "track", params)
        labels = self.context_labels(items[0])
        self.assertIn("Remove from Road trip", labels)
        self.assertEqual(client.edit_checks, [("p" * 22, "u" * 22)])

    def test_track_in_a_read_only_playlist_cannot_be_removed(self):
        tracks = [{"id": "t" * 22, "name": "Song"}]
        client = _FakeClient(editable=False)
        params = dict(action="collection", kind="playlist", id="p" * 22,
                      name="Someone else", owner="o" * 22)
        items = self.render(client, tracks, "track", params)
        self.assertFalse([label for label in self.context_labels(items[0])
                          if label.startswith("Remove from")])

    def test_track_outside_a_playlist_cannot_be_removed(self):
        tracks = [{"id": "t" * 22, "name": "Song"}]
        items = self.render(_FakeClient(), tracks, "track", dict(action="liked_songs"))
        self.assertFalse([label for label in self.context_labels(items[0])
                          if label.startswith("Remove from")])


if __name__ == "__main__":
    unittest.main()
