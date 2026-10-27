"""Small Kodi music browser. No background catalogue requests."""

import contextlib
import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlparse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from resources.lib import catalogue

ADDON = xbmcaddon.Addon()


def _t(string_id, *args):
    text = ADDON.getLocalizedString(string_id)
    return text.format(*args) if args else text
BASE = "plugin://plugin.audio.resonance/"
DATA = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))


def _handle():
    """Return the directory handle Kodi handed to this plugin run.

    A run without a handle cannot render a listing. That happens when Kodi
    starts the add-on as a script, for example when a row that only carries
    information is clicked.
    """
    if len(sys.argv) > 1 and sys.argv[1].lstrip("-").isdigit():
        return int(sys.argv[1])
    return -1


def _params():
    """Return the query parameters Kodi handed to this plugin run."""
    if len(sys.argv) > 2:
        return dict(parse_qsl(sys.argv[2].lstrip("?")))
    return {}


HANDLE = _handle()
PARAMS = _params()


def url(**values):
    return BASE + "?" + urlencode(values)


def token():
    from utils import get_cached_auth_token
    # Token renewal belongs to the service so simultaneous plugin windows
    # cannot launch competing authentication processes.
    return get_cached_auth_token()


ICON_DIR = os.path.join(ADDON.getAddonInfo("path"), "resources", "media")

# Every menu row carries its own bundled icon so the menu looks the same in
# every skin. Skin-provided "Default*.png" artwork is not used because the
# names differ per skin and many skins do not ship all of them.
MENU_ICONS = {
    "spotify_library_menu": "library.png",
    "search_menu": "search.png",
    "search": "search.png",
    "setup": "setup.png",
    "playback_status": "playback.png",
    "status": "account.png",
    "liked_songs": "songs.png",
    "saved_albums": "albums.png",
    "followed_artists": "artists.png",
    "saved_playlists": "playlists.png",
    "playlists": "playlists.png",
    "refresh_playlists": "refresh.png",
    "web_login": "key.png",
    "web_login_paste": "key.png",
    "connect_playback": "cast.png",
    "playback_quality": "quality.png",
    "sign_out": "sign_out.png",
    "clear_cache": "cache.png",
    "open_link": "link.png",
}

# Actions that open a page for the user to browse. The release notes are
# shown once after an update on the first of these pages.
PAGE_ACTIONS = {
    "home",
    "spotify_library_menu",
    "search_menu",
    "playback_status",
    "status",
    "playlists",
    "saved_playlists",
    "liked_songs",
    "saved_albums",
    "followed_artists",
    "search",
    "collection",
    "artist_top_tracks",
    "artist_albums",
    "artist_singles",
    "artist_appears_on",
    "artist_everything",
}


def icon_path(name):
    """Resolve a bundled menu icon name to an absolute path."""
    if not name:
        return ""
    if os.path.isabs(name):
        return name
    return os.path.join(ICON_DIR, name)


def row(label, action=None, icon=None, **params):
    item = xbmcgui.ListItem(label)
    # Explicit art stops Kodi probing action paths for a thumbnail. Rows that
    # only carry information get the neutral info glyph.
    art = icon_path(icon or MENU_ICONS.get(action) or "info.png")
    item.setArt({"icon": art, "thumb": art})
    item.setProperty("IsPlayable", "false")
    if action:
        # Folder items so a click runs the action; the handler then reloads status.
        add_item(url(action=action, **params), item, True)
    else:
        # A row without an action only carries information. Kodi tries to play
        # a row that has no path and answers with a "playback failed" message,
        # so the row points at an action that does nothing instead.
        add_item(url(action="noop"), item, False)


def release_notes():
    """Return the version and the release notes of the running add-on."""
    from resources.lib import whatsnew

    version = str(ADDON.getAddonInfo("version") or "")
    return version, whatsnew.notes_for(version)


def show_release_notes():
    """Show the release notes of the running version in a big window."""
    version, notes = release_notes()
    heading = "Resonance " + version if version else "Resonance"
    if not notes:
        xbmcgui.Dialog().ok(heading, _t(11486))
        return
    dialog = xbmcgui.Dialog()
    # The text viewer is the big scrollable window. Older Kodi versions only
    # know the small message box.
    viewer = getattr(dialog, "textviewer", None)
    if viewer is not None:
        viewer(heading + " - " + _t(11598), notes)
    else:
        dialog.ok(heading + " - " + _t(11598), notes)


def maybe_show_release_notes():
    """Show the release notes once after the add-on was updated.

    The version that was shown last is kept in the add-on settings, so the
    window appears after every update and never twice for the same version.
    """
    version = str(ADDON.getAddonInfo("version") or "")
    if not version:
        return
    try:
        if ADDON.getSetting("last_notes_version") == version:
            return
        ADDON.setSetting("last_notes_version", version)
    except Exception:
        pass
    try:
        # Opening a modal window here keeps the directory handle alive while
        # settings notifications can replace the same listing underneath it.
        if HANDLE >= 0:
            command = "RunPlugin(" + url(action="whats_new") + ")"
            xbmc.executebuiltin("AlarmClock(ResonanceNotes," + command + ",00:00:01,silent)")
        else:
            show_release_notes()
    except Exception as exc:
        xbmc.log("Resonance release notes failed: " + str(exc), xbmc.LOGERROR)


def add_item(path, item, is_folder):
    """Add a row to the current listing.

    Kodi runs the add-on without a directory handle when a context menu
    entry starts it as a script. There is no listing in that case, so the
    row is dropped instead of asking Kodi for an invalid handle.
    """
    if HANDLE < 0:
        return
    xbmcplugin.addDirectoryItem(HANDLE, path, item, is_folder)


def end_directory(succeeded=True, cache_to_disc=False):
    """Finish the current listing.

    Nothing has to be finished when Kodi ran the add-on as a script.
    """
    if HANDLE < 0:
        return
    xbmcplugin.endOfDirectory(HANDLE, succeeded=succeeded, cacheToDisc=cache_to_disc)


def spotify_write_context_menu(entry, kind, liked=None, saved=None):
    """Return Spotify library/playlist actions for supported rows.

    ``liked`` and ``saved`` hold the ids Spotify already keeps in the account
    library. They are ``None`` when Spotify could not answer, in which case
    both the add and the remove action are offered so nothing is hidden by a
    failed check.
    """
    identity = str(entry.get("id") or "")
    if kind == "track" and re.fullmatch(r"[A-Za-z0-9]{22}", identity):
        actions = []
        if liked is None or identity not in liked:
            actions.append((_t(11561), "RunPlugin(" + url(action="spotify_library", kind="track", id=identity, save="1") + ")"))
        if liked is None or identity in liked:
            actions.append((_t(11562), "RunPlugin(" + url(action="spotify_library", kind="track", id=identity, save="0") + ")"))
        actions.append((_t(11563), "RunPlugin(" + url(action="choose_playlist", id=identity) + ")"))
        actions.append((_t(11564), "RunPlugin(" + url(action="create_playlist", id=identity) + ")"))
        return actions
    if kind == "album" and re.fullmatch(r"[A-Za-z0-9]{22}", identity):
        actions = []
        if saved is None or identity not in saved:
            actions.append((_t(11565), "RunPlugin(" + url(action="spotify_library", kind="album", id=identity, save="1") + ")"))
        if saved is None or identity in saved:
            actions.append((_t(11566), "RunPlugin(" + url(action="spotify_library", kind="album", id=identity, save="0") + ")"))
        return actions
    return []


def playlist_remove_context_menu(entry, playlist=None):
    """Return the removal action for a track shown inside a playlist.

    The playlist comes from the page the track is listed on, so the track is
    removed from that playlist. There is no playlist picker, because only a
    playlist the track really is in can be removed from.
    """
    if not playlist:
        return []
    playlist_id, playlist_name, editable = playlist
    identity = str(entry.get("id") or "")
    if not editable or not re.fullmatch(r"[A-Za-z0-9]{22}", identity):
        return []
    label = _t(11567) + (playlist_name or _t(11568))
    return [(label, "RunPlugin(" + url(
        action="remove_from_playlist", playlist=playlist_id, id=identity,
        name=playlist_name or "",
    ) + ")")]


def playlist_context_menu(entry, client=None):
    """Return library actions for a playlist row this account follows."""
    identity = str(entry.get("id") or "")
    owner = entry.get("owner") or {}
    owner_id = str(owner.get("id") or "") if isinstance(owner, dict) else ""
    user_id = str(getattr(client, "spotify_user_id", "") or "")
    if not re.fullmatch(r"[A-Za-z0-9]{22}", identity) or not owner_id or not user_id:
        return []
    if owner_id == user_id:
        # Owned playlists are not followed, so there is nothing to unfollow.
        return []
    return [(
        _t(11569),
        "RunPlugin(" + url(action="unfollow_playlist", id=identity, name=entry.get("name") or "") + ")",
    )]


def _image_url(entry):
    images = (entry or {}).get("images") or []
    if not isinstance(images, list):
        return images or ""
    for image in images:
        value = image.get("url") if isinstance(image, dict) else image
        if value:
            return value
    return ""


def _apply_art(item, image):
    if image:
        item.setArt({"thumb": image, "icon": image, "album.thumb": image, "fanart": image})


def _artist_ref(entry, kind):
    if kind == "artist":
        return entry.get("id") or "", entry.get("name") or ""
    artists = entry.get("artists") or []
    if isinstance(artists, list):
        for artist in artists:
            if isinstance(artist, dict) and re.fullmatch(r"[A-Za-z0-9]{22}", str(artist.get("id") or "")):
                return artist.get("id") or "", artist.get("name") or ""
    return "", ""


def artist_context_menu(entry, kind, followed=None):
    artist_id, artist_name = _artist_ref(entry, kind)
    if not artist_id:
        return []
    suffix = " - " + artist_name if artist_name else ""
    actions = [
        (_t(11570) + suffix, "artist_everything"),
        (_t(11571) + suffix, "artist_top_tracks"),
        (_t(11572) + suffix, "artist_albums"),
        (_t(11573) + suffix, "artist_singles"),
        (_t(11574) + suffix, "artist_appears_on"),
    ]
    items = [
        (label, "Container.Update(" + url(action=action, id=artist_id, name=artist_name) + ")")
        for label, action in actions
    ]
    if kind == "artist" and followed is not None and artist_id in followed:
        # Only artists the account follows can be unfollowed.
        items.append((
            _t(11575) + suffix,
            "RunPlugin(" + url(action="unfollow_artist", id=artist_id, name=artist_name) + ")",
        ))
    return items


def track_item(track, path):
    item = xbmcgui.ListItem(track.get("name") or _t(11576), path=path)
    item.setProperty("IsPlayable", "true")
    item.setProperty("do_not_analyze", "true")
    album = track.get("album") or {}
    info = item.getMusicInfoTag()
    info.setTitle(track.get("name") or _t(11576))
    info.setArtist(" / ".join(a.get("name", "") for a in track.get("artists", [])))
    info.setAlbum(album.get("name") or "")
    info.setDuration(int(track.get("duration_ms") or 0) // 1000)
    date = str(album.get("release_date") or track.get("release_date") or "")
    if re.match(r"\d{4}", date) and int(date[:4]) > 1900:
        info.setYear(int(date[:4]))
    _apply_art(item, _image_url(track) or _image_url(album))
    return item


def directory_item(entry, kind, path, client=None, saved=None, followed=None):
    item = xbmcgui.ListItem(entry.get("name") or _t(11576))
    _apply_art(item, _image_url(entry))
    item.addContextMenuItems(
        spotify_write_context_menu(entry, kind, saved=saved)
        + playlist_context_menu(entry, client)
        + artist_context_menu(entry, kind, followed=followed)
    )
    return item


def owned_playlists(client):
    """Return every playlist owned by the signed-in account."""
    owned = []
    offset = 0
    while True:
        items, more = client.playlists(offset)
        owned.extend(
            item for item in items
            if str((item.get("owner") or {}).get("id") or "") == str(client.spotify_user_id or "")
        )
        if more is None:
            return owned
        offset = more


def section(label):
    """Add a section header to the current listing."""
    row("[COLOR skyblue][" + label + "][/COLOR]", icon="section.png")


def status(client):
    """Render the account and status page.

    Each group shows the current state in one row, and the row itself is the
    action that changes it, so the page reads as a status overview.
    """
    from utils import get_playback_settings, get_spotify_device_name, spotty_has_credentials

    section(_t(11452))
    if spotty_has_credentials():
        row(_t(11458, get_spotify_device_name()), "connect_playback", icon="cast.png")
    else:
        row(_t(11459), "guided_setup", icon="setup.png")
    if token():
        row(_t(11460), "web_login", icon="key.png")
    else:
        row(_t(11461), "web_login", icon="key.png")
    try:
        import web_auth
        from utils import get_spotify_webapi_client_id
        if web_auth.load_login_request(DATA, get_spotify_webapi_client_id(), clock=time.time):
            row(_t(11462), "web_login_paste", icon="key.png")
    except Exception:
        pass

    section(_t(11454))
    refreshed = client.cached("playlists-refreshed", stale=True)
    if isinstance(refreshed, dict) and refreshed.get("count"):
        count = int(refreshed.get("count") or 0)
        stamp = float(refreshed.get("at") or 0)
        when = time.strftime("%d %b %H:%M", time.localtime(stamp)) if stamp else _t(11465)
        row(_t(11463, count, when), "refresh_playlists", icon="refresh.png")
    else:
        row(_t(11464), "refresh_playlists", icon="refresh.png")

    section(_t(11453))
    row(_t(11466), "playback_status", icon="playback.png")
    row(_t(11467, get_playback_settings()[0]), "playback_quality", icon="quality.png")

    section(_t(11455))
    version = str(ADDON.getAddonInfo("version") or "")
    row(_t(11471, version) if version else _t(11472), "whats_new", icon="info.png")
    row(_t(11473), "clear_cache", icon="cache.png")
    row(_t(11474), "sign_out", icon="sign_out.png")
    end_directory(cache_to_disc=False)


def playback_status():
    from utils import get_playback_settings, get_spotify_device_name, spotty_has_credentials

    section(_t(11456))
    try:
        player = xbmc.Player()
        if player.isPlayingAudio():
            tag = player.getMusicInfoTag()
            title = tag.getTitle() or _t(11470)
            artists = tag.getArtist() or []
            if isinstance(artists, str):
                artists = [artists]
            label = title
            if artists:
                label += " - " + " / ".join(artists)
            row(label, icon="playback.png")
        else:
            row(_t(11468), icon="playback.png")
    except Exception:
        row(_t(11469), icon="playback.png")

    section(_t(11457))
    row(_t(11467, get_playback_settings()[0]), "playback_quality", icon="quality.png")
    if spotty_has_credentials():
        row(_t(11458, get_spotify_device_name()), "connect_playback", icon="cast.png")
    else:
        row(_t(11459), "connect_playback", icon="cast.png")
    account_state = xbmcgui.Window(10000).getProperty("resonance.status")
    if account_state:
        section(_t(11452))
        row(account_state, icon="account.png")
    end_directory(cache_to_disc=False)


def _library_ids(client, identities, kind, params):
    """Return the ids on this page that Spotify already keeps in the library.

    The answer is ``None`` when Spotify could not be asked, so the context
    menu keeps offering both the add and the remove action.
    """
    try:
        if kind == "track":
            if params.get("action") == "liked_songs":
                # Every track in Liked Songs is liked by definition.
                return {str(identity) for identity in identities}
            return client.liked_track_ids(identities)
        if kind == "album":
            if params.get("action") == "saved_albums":
                # Every album in Saved Albums is saved by definition.
                return {str(identity) for identity in identities}
            return client.saved_album_ids(identities)
        if kind == "artist":
            if params.get("action") == "followed_artists":
                # Every artist in Followed Artists is followed by definition.
                return {str(identity) for identity in identities}
            return client.saved_artist_ids(identities)
    except Exception:
        return None
    return None


def render(client, items, kind, more, params):
    if kind == "track":
        items = [client.metadata(item) for item in items]
    xbmcplugin.setContent(HANDLE, {"track": "songs", "album": "albums", "artist": "artists"}.get(kind, "files"))
    if client.notice:
        row(client.notice)
    page_key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    if kind == "track":
        client.save("queue:" + page_key, items, ttl=86400)
    library = _library_ids(client, [entry.get("id") for entry in items], kind, params)
    playlist = None
    if kind == "track" and params.get("action") == "collection" and params.get("kind") == "playlist":
        # The page is a playlist, so a track on it is in that playlist.
        playlist = (
            params.get("id", ""),
            params.get("name", ""),
            client.playlist_edit_access(params.get("id", ""), params.get("owner", "")),
        )
    for index, entry in enumerate(items):
        identity = entry.get("id") or ""
        if not re.fullmatch(r"[A-Za-z0-9]{22}", identity):
            continue
        if kind == "track":
            client.save("track:" + identity, entry, ttl=86400)
            path = url(action="play", id=identity)
            item = track_item(entry, path)
            context = [
                ("Play this page from here", "RunPlugin(" + url(action="queue", key=page_key, index=index) + ")")
            ]
            if params.get("action") == "collection" and params.get("kind") == "playlist":
                start_index = max(0, int(params.get("offset", 0))) + index
                context.append(("Play playlist from here", "RunPlugin(" + url(
                    action="queue_playlist", id=params["id"], offset=start_index
                ) + ")"))
            item.addContextMenuItems(context + spotify_write_context_menu(entry, kind, liked=library)
                                     + artist_context_menu(entry, kind)
                                     + playlist_remove_context_menu(entry, playlist))
            add_item(path, item, False)
        else:
            client.save("collection:" + kind + ":" + identity, entry, ttl=86400)
            path = url(action="collection", kind=kind, id=identity, name=entry.get("name", ""))
            display_entry = entry
            if kind == "playlist":
                owner = entry.get("owner") or {}
                owner_id = str(owner.get("id") or "") if isinstance(owner, dict) else ""
                # The owner travels with the link, so the playlist page knows
                # whether the account may change the items of it.
                path = url(action="collection", kind=kind, id=identity,
                           name=entry.get("name", ""), owner=owner_id)
                if owner_id and client.spotify_user_id and owner_id != client.spotify_user_id:
                    display_entry = dict(entry)
                    if entry.get("collaborative"):
                        marker = "[COLOR skyblue][SHARED][/COLOR] "
                    else:
                        marker = "[COLOR orange][FOLLOWED][/COLOR] "
                    display_entry["name"] = marker + (entry.get("name") or _t(11576))
            item = directory_item(
                display_entry, kind, path, client=client,
                saved=library if kind == "album" else None,
                followed=library if kind == "artist" else None,
            )
            add_item(path, item, True)
    if more is not None:
        if params.get("action") == "followed_artists":
            next_params = dict(params, after=more)
        else:
            next_params = dict(params, offset=more)
        row(_t(11487), **next_params)
    if not items:
        row(_t(11488))
    end_directory(cache_to_disc=False)


def render_artist_everything(client, sections, artist_name, artist_id):
    xbmcplugin.setContent(HANDLE, "files")
    if artist_name:
        xbmcplugin.setProperty(HANDLE, "FolderName", artist_name)
    # One library lookup for the whole page keeps the request count low.
    track_ids = []
    album_ids = []
    for label, kind, items in sections:
        for entry in items:
            if kind == "track":
                track_ids.append(entry.get("id"))
            elif kind == "album":
                album_ids.append(entry.get("id"))
    liked = _library_ids(client, track_ids, "track", {}) if track_ids else None
    saved = _library_ids(client, album_ids, "album", {}) if album_ids else None
    for label, kind, items in sections:
        row("[" + label + "]")
        for entry in items:
            identity = entry.get("id") or ""
            if not re.fullmatch(r"[A-Za-z0-9]{22}", identity):
                continue
            if kind == "track":
                client.save("track:" + identity, entry, ttl=86400)
                path = url(action="play", id=identity)
                item = track_item(entry, path)
                item.addContextMenuItems(spotify_write_context_menu(entry, kind, liked=liked)
                                         + artist_context_menu(entry, kind))
                add_item(path, item, False)
            else:
                client.save("collection:" + kind + ":" + identity, entry, ttl=86400)
                path = url(action="collection", kind=kind, id=identity, name=entry.get("name", ""))
                add_item(path, directory_item(entry, kind, path, client=client, saved=saved), True)
        if kind == "album":
            more_action = {
                "Albums": "artist_albums",
                "Singles": "artist_singles",
                "Appears on": "artist_appears_on",
            }.get(label)
            if more_action:
                more_label = {"Albums": _t(11479), "Singles": _t(11578), "Appears on": _t(11579)}.get(label, label)
                row(_t(11580).format(more_label.lower()), more_action, id=artist_id, name=artist_name)
    if not sections:
        row(_t(11489))
    end_directory(cache_to_disc=False)


SIGN_IN_WAIT_SECONDS = 300


def web_login():
    """Sign in through the regular browser authorization-code flow.

    The resulting Web API token is cached in window properties right away so
    search works without rate-limiting; the service renews it afterwards.
    """
    import web_auth
    from utils import get_spotify_webapi_client_id

    client_id = get_spotify_webapi_client_id()
    if not re.fullmatch(r"[0-9A-Fa-f]{32}", client_id or ""):
        xbmcgui.Dialog().ok(
            "Resonance",
            _t(11451),
        )
        return
    redirect_uri = (ADDON.getSetting("web_auth_redirect_uri").strip()
                    or web_auth.DEFAULT_WEB_AUTH_REDIRECT_URI)
    try:
        port = int(urlparse(redirect_uri).port or web_auth.DEFAULT_PORT)
    except ValueError:
        port = web_auth.DEFAULT_PORT

    verifier, challenge = web_auth.pkce_pair()
    state = web_auth.random_state()
    scopes = list(web_auth.DEFAULT_SCOPE)
    address = web_auth.authorize_url(client_id, redirect_uri, scopes, challenge, state)
    display = web_auth.shorten_url(address) or address
    # Hosted redirect (Vercel relay etc.): the browser cannot reach this box,
    # so the add-on polls the relay for the code instead of a local listener.
    poll_url = web_auth.derive_poll_url(redirect_uri)

    server = None
    if not poll_url:
        try:
            server = web_auth.CallbackServer(port=port)
        except web_auth.WebAuthError as exc:
            xbmcgui.Dialog().ok("Resonance", str(exc))
            return

    if not os.path.exists(DATA):
        os.makedirs(DATA, exist_ok=True)
    try:
        with open(os.path.join(DATA, "web-auth-url.txt"), "w", encoding="utf-8") as stream:
            stream.write("Short: " + display + "\nFull: " + address + "\n")
    except OSError:
        pass
    web_auth.save_login_request(DATA, client_id, redirect_uri, state, verifier, clock=time.time)
    qr_dialog = None
    try:
        from qr_dialog import show_qr_dialog
        qr_dialog = show_qr_dialog(
            display,
            title=_t(11527),
            message=_t(11528),
            modal=False,
        )
    except Exception:
        qr_dialog = None

    if server is not None:
        server.start()
    canceled = False
    monitor = xbmc.Monitor()
    try:
        start = time.monotonic()
        next_poll = 0.0
        result = {}
        while time.monotonic() - start < SIGN_IN_WAIT_SECONDS:
            if server is not None and server.result:
                result = dict(server.result)
            if not result.get("code") and poll_url and time.monotonic() >= next_poll:
                next_poll = time.monotonic() + 2.0
                code = web_auth.poll_for_code(poll_url, state)
                if code:
                    result = {"code": code, "state": state}
            if result.get("code"):
                break
            if monitor.waitForAbort(0.5):
                canceled = True
                break
    finally:
        if qr_dialog and hasattr(qr_dialog, "close"):
            qr_dialog.close()
    if server is not None:
        server.stop()
    if canceled:
        return
    if result.get("error"):
        web_auth.clear_login_request(DATA)
        xbmcgui.Dialog().notification("Resonance", "Spotify login was refused: " + result["error"])
        return
    if result.get("code") and result.get("state") == state:
        try:
            token_data = web_auth.exchange_code(client_id, redirect_uri, result["code"], verifier)
        except web_auth.WebAuthError as exc:
            web_auth.clear_login_request(DATA)
            xbmcgui.Dialog().ok("Resonance", str(exc))
            return
        _finish_web_login(client_id, redirect_uri, token_data)
        return
    if result.get("state"):
        web_auth.clear_login_request(DATA)
        xbmcgui.Dialog().notification("Resonance", _t(11493))
        return
    # The callback never arrived (the Spotify page opened on a device that
    # cannot reach this box). Offer to paste the code from the address bar.
    if xbmcgui.Dialog().yesno(
        "Resonance",
        _t(11529),
        nolabel=_t(11419),
        yeslabel=_t(11530),
    ):
        code = xbmcgui.Dialog().input(_t(11531)).strip()
        if code:
            request = web_auth.load_login_request(DATA, client_id)
            if not request:
                web_auth.clear_login_request(DATA)
                xbmcgui.Dialog().notification("Resonance", _t(11494))
                return
            try:
                token_data = web_auth.exchange_code(client_id, request["redirect_uri"], code, request["verifier"])
            except web_auth.WebAuthError as exc:
                web_auth.clear_login_request(DATA)
                xbmcgui.Dialog().ok("Resonance", str(exc))
                return
            _finish_web_login(client_id, request["redirect_uri"], token_data)
    return


def _finish_web_login(client_id, redirect_uri, token_data):
    """Persist the browser-authorization token and refresh the menu."""
    import web_auth
    try:
        session = web_auth.WebAuth(DATA, client_id)
        session.save(web_auth._normalize(token_data, client_id, time.time()))
    except web_auth.WebAuthError as exc:
        xbmcgui.Dialog().ok("Resonance", str(exc))
        return
    web_auth.clear_login_request(DATA)
    # The previous rate-limit gate belonged to the old token.
    try:
        from utils import clear_provider_gate
        clear_provider_gate("spotify")
    except Exception:
        pass
    access = token_data.get("access_token") or ""
    if access:
        from utils import cache_auth_token, cache_auth_token_expires_at, cache_auth_client_id
        cache_auth_token(access)
        expires_in = int(token_data.get("expires_in") or 3600)
        cache_auth_token_expires_at(str(int(time.time()) + expires_in))
        cache_auth_client_id(client_id)
    window = xbmcgui.Window(10000)
    window.setProperty("resonance.signed-in", "1")
    window.setProperty("resonance.status", _t(11535))
    try:
        from utils import spotty_has_credentials
        complete = spotty_has_credentials()
    except Exception:
        complete = False
    message = (
        _t(11533)
        if complete
        else _t(11534)
    )
    xbmcgui.Dialog().notification("Resonance", message)
    xbmc.executebuiltin("Container.Update(" + url(action="home") + ",replace)")


def resume_web_login_paste():
    """Finish a pending browser sign-in by pasting the code."""
    import web_auth
    from utils import get_spotify_webapi_client_id
    client_id = get_spotify_webapi_client_id()
    request = web_auth.load_login_request(DATA, client_id)
    if not request:
        xbmcgui.Dialog().notification("Resonance", _t(11495))
        return
    code = xbmcgui.Dialog().input(_t(11532)).strip()
    if not code:
        return
    try:
        token_data = web_auth.exchange_code(client_id, request["redirect_uri"], code, request["verifier"])
    except web_auth.WebAuthError as exc:
        web_auth.clear_login_request(DATA)
        xbmcgui.Dialog().ok("Resonance", str(exc))
        return
    _finish_web_login(client_id, request["redirect_uri"], token_data)


def _ensure_device_connect_receiver():
    """Ask the service to start pairing; it owns the receiver and start lock."""
    xbmcgui.Window(10000).setProperty("resonance.connect", "1")


def connect_device(navigate, wait_for_playback=False):
    """Guide the user through Spotify Device Connect.

    Browser sign-in already covers the catalogue when ``wait_for_playback``
    is set; this variant waits specifically for the librespot Device-Connect
    pairing that playback requires.  Without it, waits for the normal
    sign-in transition (search token + spotty credentials).
    """
    from utils import get_spotify_device_name, resonance_signed_in, spotty_has_credentials
    device = get_spotify_device_name()
    while True:
        _ensure_device_connect_receiver()
        progress = xbmcgui.DialogProgress()
        progress.create("Spotify", _t(11446, device))
        try:
            start = time.monotonic()
            while True:
                ready = spotty_has_credentials() if wait_for_playback else resonance_signed_in()
                if ready or progress.iscanceled():
                    break
                elapsed = time.monotonic() - start
                if elapsed >= SIGN_IN_WAIT_SECONDS:
                    break
                progress.update(int(elapsed * 100 / SIGN_IN_WAIT_SECONDS))
                xbmc.sleep(500)
        finally:
            progress.close()
        ready = spotty_has_credentials() if wait_for_playback else resonance_signed_in()
        if ready:
            # Do not endOfDirectory: the connect folder has no items and would
            # flash an empty menu before the redirect below replaces it.
            if navigate:
                xbmc.executebuiltin("Container.Update(" + url(action="home") + ",replace)")
            return True
        retry = xbmcgui.Dialog().yesno(
            "Spotify",
            _t(11447, device),
            nolabel=_t(11419),
            yeslabel=_t(11418),
        )
        if not retry:
            if navigate:
                xbmc.executebuiltin("Container.Update(" + url(action="home") + ",replace)")
            return False


WIZARD_DONE_SETTING = "setup_wizard_completed"
SETUP_FLOW_PROPERTY = "resonance.setup-active"
SETUP_PROMPTED_PROPERTY = "resonance.setup-prompted"


@contextlib.contextmanager
def _setup_flow():
    """Mark the window as owned by the guided setup while it runs.

    The service reads this and does not reload the Resonance listing during
    the flow, because a reload behind open dialogs leaves a busy spinner.
    """
    window = xbmcgui.Window(10000)
    previous = window.getProperty(SETUP_FLOW_PROPERTY)
    window.setProperty(SETUP_FLOW_PROPERTY, "1")
    try:
        yield
    finally:
        window.setProperty(SETUP_FLOW_PROPERTY, previous)


def setup_prompt_needed():
    """Return True when Resonance is not fully signed in and has not asked yet.

    The question is asked once per Kodi session, so choosing Later does not
    keep coming back on every menu open.
    """
    window = xbmcgui.Window(10000)
    if window.getProperty(SETUP_FLOW_PROPERTY) or window.getProperty(SETUP_PROMPTED_PROPERTY):
        return False
    return next_setup_step() is not None


def guided_setup(prompt=False):
    """Run the guided setup, then refresh the Resonance listing if it is shown.

    Only one setup runs at a time. Opening Resonance or Settings while a setup
    is already on screen does not start a second one. When started from the
    prompt on opening Resonance, the question is asked there and the welcome
    screen is skipped.
    """
    if xbmcgui.Window(10000).getProperty(SETUP_FLOW_PROPERTY):
        xbmcgui.Dialog().notification("Resonance", _t(11428))
        return
    if prompt and not xbmcgui.Dialog().yesno(
        "Resonance",
        _t(11443),
        nolabel=_t(11445),
        yeslabel=_t(11444),
    ):
        return
    with _setup_flow():
        _run_guided_setup(welcome=not prompt)
    if "plugin://plugin.audio.resonance/" in (xbmc.getInfoLabel("Container.FolderPath") or ""):
        xbmc.executebuiltin("Container.Update(" + url(action="home") + ",replace)")


def _setup_started():
    from utils import get_spotify_webapi_client_id, spotty_has_credentials
    return bool(get_spotify_webapi_client_id()) or spotty_has_credentials()


def _run_guided_setup(welcome=True):
    """Walk a new user through the three one-time setup steps.

    Each step ends with a dialog that says what happened, and the last dialog
    lists every step. Skipping marks the wizard as done; it can always be run
    again from Settings > Account. A step that is not finished stops the wizard
    without marking it done, so the next launch offers it again. Steps that
    are already complete are skipped and reported as already done.
    """
    import web_auth
    from utils import get_spotify_device_name, get_spotify_webapi_client_id, spotty_has_credentials
    dialog = xbmcgui.Dialog()
    if welcome:
        text = _t(11403) if _setup_started() else _t(11402)
        if not dialog.yesno(_t(11401), text, nolabel=_t(11405), yeslabel=_t(11404)):
            ADDON.setSetting(WIZARD_DONE_SETTING, "true")
            return

    results = []

    def finish(complete):
        if complete:
            ADDON.setSetting(WIZARD_DONE_SETTING, "true")
        lines = []
        for name, status in results:
            lines.append("{}: {}".format(_t(name), _t(status)))
        dialog.ok(_t(11435), "\n".join(lines) + "\n\n" + _t(11414 if complete else 11439))

    if re.fullmatch(r"[0-9A-Fa-f]{32}", get_spotify_webapi_client_id() or ""):
        results.append((11440, 11438))
    else:
        dialog.ok(
            _t(11406),
            _t(11407, web_auth.DEFAULT_WEB_AUTH_REDIRECT_URI),
        )
        client_id = (dialog.input(_t(11408), type=xbmcgui.INPUT_ALPHANUM) or "").strip()
        if not re.fullmatch(r"[0-9A-Fa-f]{32}", client_id):
            results.append((11440, 11437))
            dialog.ok(_t(11406), _t(11430))
            finish(False)
            return
        from utils import save_spotify_webapi_client_id
        save_spotify_webapi_client_id(client_id)
        results.append((11440, 11436))
        dialog.ok(_t(11406), _t(11429))

    if spotty_has_credentials():
        results.append((11441, 11438))
    else:
        dialog.ok(_t(11410), _t(11411))
        if not guide_playback_pairing(announce=False):
            results.append((11441, 11437))
            dialog.ok(_t(11410), _t(11432))
            finish(False)
            return
        results.append((11441, 11436))
        dialog.ok(_t(11410), _t(11431, get_spotify_device_name()))

    if token():
        results.append((11442, 11438))
    else:
        dialog.ok(_t(11412), _t(11413))
        web_login()
        if not token():
            results.append((11442, 11437))
            dialog.ok(_t(11412), _t(11434))
            finish(False)
            return
        results.append((11442, 11436))
        dialog.ok(_t(11412), _t(11433))

    finish(True)


def guide_playback_pairing(track_id=None, announce=True):
    """Walk the user through the one-time Spotify Device Connect pairing.

    Browser sign-in already covers search and playlists; this box still
    needs a librespot session for playback.  The Spotify app picks the box
    from its speaker list once, and credentials are kept for next time.
    """
    from utils import get_spotify_device_name, spotty_has_credentials
    device = get_spotify_device_name()
    while not spotty_has_credentials():
        # Wait first and only ask to retry after a wait ends without pairing.
        # Asking first left a Retry/Cancel prompt on screen after the box had
        # already been paired from the phone.
        _ensure_device_connect_receiver()
        progress = xbmcgui.DialogProgress()
        progress.create("Resonance", _t(11415, device))
        try:
            start = time.monotonic()
            while not spotty_has_credentials():
                elapsed = time.monotonic() - start
                if progress.iscanceled() or elapsed >= SIGN_IN_WAIT_SECONDS:
                    break
                progress.update(int(elapsed * 100 / SIGN_IN_WAIT_SECONDS))
                xbmc.sleep(500)
        finally:
            progress.close()
        if spotty_has_credentials():
            break
        if not xbmcgui.Dialog().yesno(
            "Resonance",
            _t(11416, device),
            nolabel=_t(11419),
            yeslabel=_t(11418),
        ):
            if announce:
                xbmcgui.Dialog().notification("Resonance", _t(11417, device))
            return False
    if track_id:
        xbmc.executebuiltin('PlayMedia("' + url(action="play", id=track_id) + '")')
    return True


def reset_resonance():
    """Move Resonance state into a backup folder so the add-on starts fresh.

    Nothing is deleted. The backup keeps the old sign-in, cache and runtime
    files. The legacy-migration marker stays, so old data is never copied back.
    Kodi keeps running the old receiver until it restarts, so the user is asked
    to restart Kodi afterwards.
    """
    if not xbmcgui.Dialog().yesno("Resonance", _t(11425), nolabel=_t(11419), yeslabel=_t(11427)):
        return
    backup = os.path.join(DATA, "reset-backup-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(backup, exist_ok=True)
    for name in os.listdir(DATA):
        if name in ("settings.xml", ".legacy-data-migrated") or name.startswith("reset-backup-"):
            continue
        os.replace(os.path.join(DATA, name), os.path.join(backup, name))
    ADDON.setSetting("spotify_webapi_client_id", "")
    ADDON.setSetting("spotify_account_email", "")
    ADDON.setSetting(WIZARD_DONE_SETTING, "false")
    xbmcgui.Dialog().notification("Resonance", _t(11426))


def next_setup_step():
    """Return (label, action) for the first unfinished setup step, or None."""
    from utils import get_spotify_webapi_client_id, spotty_has_credentials
    if not re.fullmatch(r"[0-9A-Fa-f]{32}", get_spotify_webapi_client_id() or ""):
        return _t(11420), "guided_setup"
    if not spotty_has_credentials():
        return _t(11421), "guided_setup"
    if not token() and not _browser_session_saved():
        return _t(11422), "web_login"
    return None


def _browser_session_saved():
    from utils import web_auth as browser_store
    try:
        return browser_store().has_session()
    except Exception:
        return False


def stream_url(track, client):
    identity = track.get("id") or ""
    duration = int(track.get("duration_ms") or 0) / 1000
    if not re.fullmatch(r"[A-Za-z0-9]{22}", identity) or duration <= 0:
        raise catalogue.Unavailable(_t(11522))
    reason = client.unplayable(track)
    if reason:
        raise catalogue.Unavailable(reason)
    return f"http://127.0.0.1:52309/track/{identity}/{duration}"


def show_playlist_copy_help(identity, name="", client=None):
    """Offer a Spotify link when Spotify blocks reading a followed playlist."""
    title = (name or _t(11568)).strip()
    spotify_url = "https://open.spotify.com/playlist/" + identity
    message = _t(11582).format(spotify_url)
    xbmcgui.Dialog().textviewer(
        _t(11536) + title,
        message,
        usemono=False,
    )
    choice = xbmcgui.Dialog().select(
        _t(11537),
        [
            _t(11538),
            _t(11539),
            _t(11540),
            _t(11541),
        ],
    )
    if choice == 0:
        ADDON.setSetting("hide_unreadable_playlists", "true")
        xbmcgui.Dialog().notification(
            "Resonance",
            _t(11542),
        )
    elif choice == 1:
        from qr_dialog import show_qr_dialog
        show_qr_dialog(
            spotify_url,
            title=_t(11543) + title,
            message=_t(11544),
        )
    elif choice == 2 and client is not None:
        refreshed = client.refresh_playlists()
        xbmcgui.Dialog().notification(
            "Resonance",
            _t(11545)
            if refreshed else _t(11546),
        )


def home_menu():
    from utils import resonance_signed_in
    step = next_setup_step()
    if step:
        row(step[0], step[1], icon="setup.png")
    if resonance_signed_in():
        row(_t(11475), "spotify_library_menu")
        row(_t(11476), "search_menu")
    else:
        row(_t(11476), "search_menu")
    row(_t(11466), "playback_status")
    row(_t(11477), "status")
    end_directory(cache_to_disc=False)


def run():
    from utils import migrate_legacy_addon_data, resonance_signed_in, spotty_has_credentials
    # A context menu entry runs the add-on as a script. Kodi then hands over
    # the handle -1 and the action still has to run, only the listing is
    # skipped.
    migrate_legacy_addon_data()
    action = PARAMS.get("action", "home")
    if HANDLE >= 0 and action in (
        "web_login", "web_login_paste", "connect", "connect_settings", "connect_playback", "whats_new",
        "guided_setup",
    ):
        # Finish the folder request before opening long-running auth dialogs.
        # Kodi rejects Container.Update while that request is still active,
        # which can leave a busy overlay on an already-rendered home page.
        home_menu()
        command = "RunPlugin(" + url(**PARAMS) + ")"
        xbmc.executebuiltin("AlarmClock(ResonanceAuth," + command + ",00:00:01,silent)")
        return
    if action == "home" and HANDLE >= 0 and setup_prompt_needed():
        xbmcgui.Window(10000).setProperty(SETUP_PROMPTED_PROPERTY, "1")
        home_menu()
        command = "RunPlugin(" + url(action="guided_setup", prompt="1") + ")"
        xbmc.executebuiltin("AlarmClock(ResonanceAuth," + command + ",00:00:01,silent)")
        return
    if action == "guided_setup":
        guided_setup(prompt=PARAMS.get("prompt") == "1")
        return
    if action == "reset_addon":
        reset_resonance()
        return
    if action == "whats_new" and HANDLE < 0:
        show_release_notes()
        return
    if action in PAGE_ACTIONS:
        # The release notes are shown on the first page the user opens after
        # an update.
        maybe_show_release_notes()
    if action == "home":
        # Account-specific rows disappear after logout; the public catalogue
        # (search) keeps working without an account.
        home_menu()
        return
    if action == "spotify_library_menu":
        row(_t(11481), "liked_songs")
        row(_t(11482), "saved_albums")
        row(_t(11483), "followed_artists")
        row(_t(11484), "saved_playlists")
        row(_t(11485), "create_playlist", icon="plus.png")
        end_directory(cache_to_disc=False)
        return
    if action == "search_menu":
        # Playlists are not searchable. Spotify only returns playlist items to
        # the owner and collaborators, so a global playlist search could only
        # list playlists that Resonance cannot open. Playlists stay reachable
        # through Your Spotify Library.
        for kind, label, icon in (("track", _t(11478), "songs.png"),
                                  ("album", _t(11479), "albums.png"),
                                  ("artist", _t(11480), "artists.png")):
            row(label, "search", icon=icon, kind=kind)
        end_directory(cache_to_disc=False)
        return
    if action == "playback_status":
        playback_status()
        return
    if action == "noop":
        # Rows that only carry information point here. Kodi runs the add-on in
        # the background, so clicking such a row leaves the page untouched.
        end_directory(cache_to_disc=False)
        return
    if action in ("sign_out", "clear_cache"):
        if action == "sign_out":
            if xbmcgui.Dialog().yesno(
                "Resonance",
                _t(11547),
                nolabel=_t(11419),
                yeslabel=_t(11474),
            ):
                window = xbmcgui.Window(10000)
                window.clearProperty("resonance.logout.result")
                window.setProperty("resonance.logout", "1")
                deadline = time.monotonic() + 30
                monitor = xbmc.Monitor()
                while not window.getProperty("resonance.logout.result") and time.monotonic() < deadline:
                    if monitor.waitForAbort(0.1):
                        break
                if window.getProperty("resonance.logout.result") == "ok":
                    xbmcgui.Dialog().notification("Resonance", _t(11496))
                    xbmc.executebuiltin("Container.Refresh")
                else:
                    xbmcgui.Dialog().ok("Resonance", _t(11497))
        else:
            from utils import clear_resonance_cache
            if xbmcgui.Dialog().yesno(
                "Resonance",
                _t(11548),
                nolabel=_t(11419),
                yeslabel=_t(11473),
            ):
                try:
                    clear_resonance_cache()
                except Exception as exc:
                    xbmc.log("Resonance cache clearing failed: " + str(exc), xbmc.LOGERROR)
                    xbmcgui.Dialog().ok("Resonance", _t(11498))
                else:
                    xbmcgui.Dialog().notification("Resonance", _t(11499))
                    xbmc.executebuiltin("Container.Refresh")
        return
    if action == "web_login":
        web_login()
        return
    if action == "web_login_paste":
        resume_web_login_paste()
        return
    if action in ("connect", "connect_settings"):
        # Older menus linked here. The guided setup is the one setup path now.
        guided_setup()
        return
    if action == "connect_playback":
        # Pairing from the menu continues straight into browser sign-in when
        # that is still missing, so the user does not have to find it again.
        if connect_device(navigate=False, wait_for_playback=True) and not token():
            web_login()
        else:
            xbmc.executebuiltin("Container.Update(" + url(action="home") + ",replace)")
        return
    from utils import get_catalogue_settings
    client = catalogue.Catalogue(DATA, token, signed_in=resonance_signed_in(), **get_catalogue_settings())
    try:
        client.import_playlists()
        offset = max(0, int(PARAMS.get("offset", 0)))
        if action == "refresh_playlists":
            try:
                updated = client.refresh_playlists()
            except Exception:
                updated = False
            xbmcgui.Dialog().notification("Resonance", _t(11500) if updated else _t(11501))
            status(client)
        elif action == "spotify_library":
            kind = PARAMS.get("kind", "track")
            identity = PARAMS.get("id", "")
            save = PARAMS.get("save") == "1"
            question = {
                (True, "track"): _t(11502),
                (False, "track"): _t(11503),
                (True, "album"): _t(11504),
                (False, "album"): _t(11505),
            }[(save, kind)]
            if xbmcgui.Dialog().yesno("Spotify", question):
                client.save_library_item(kind, identity, save=save)
                result = (_t(11506) if kind == "album" and save
                          else _t(11507) if kind == "album"
                          else (_t(11508) if save else _t(11509)))
                xbmcgui.Dialog().ok("Resonance", result)
                from utils import clear_resonance_cache
                clear_resonance_cache()
                xbmc.executebuiltin("Container.Refresh")
            end_directory(cache_to_disc=False)
        elif action == "choose_playlist":
            track_id = PARAMS.get("id", "")
            owned = owned_playlists(client)
            if not owned:
                raise catalogue.Unavailable(_t(11510))
            choice = xbmcgui.Dialog().select(
                _t(11511),
                [item.get("name") or _t(11576) for item in owned],
            )
            if choice >= 0:
                target = owned[choice]
                if xbmcgui.Dialog().yesno("Spotify", _t(11552).format(target.get("name") or _t(11576))):
                    client.add_track_to_playlist(target["id"], track_id)
                    xbmcgui.Dialog().notification("Resonance", _t(11554) + (target.get("name") or _t(11581)))
                    from utils import clear_resonance_cache
                    clear_resonance_cache()
            end_directory(cache_to_disc=False)
        elif action == "remove_from_playlist":
            track_id = PARAMS.get("id", "")
            playlist_id = PARAMS.get("playlist", "")
            name = PARAMS.get("name", "") or _t(11568)
            if not re.fullmatch(r"[A-Za-z0-9]{22}", playlist_id or ""):
                raise catalogue.Unavailable(
                    _t(11512) +
                    _t(11513)
                )
            if xbmcgui.Dialog().yesno("Spotify", _t(11553).format(name)):
                client.remove_track_from_playlist(playlist_id, track_id)
                xbmcgui.Dialog().notification("Resonance", _t(11555) + name)
                from utils import clear_resonance_cache
                clear_resonance_cache()
                xbmc.executebuiltin("Container.Refresh")
            end_directory(cache_to_disc=False)
        elif action == "unfollow_playlist":
            identity = PARAMS.get("id", "")
            name = PARAMS.get("name", "") or _t(11568)
            if xbmcgui.Dialog().yesno(
                "Spotify",
                _t(11549).format(name),
                nolabel=_t(11419),
                yeslabel=_t(11550),
            ):
                client.save_library_item("playlist", identity, save=False)
                from utils import clear_resonance_cache
                clear_resonance_cache()
                xbmcgui.Dialog().notification("Resonance", _t(11514))
                xbmc.executebuiltin("Container.Refresh")
            end_directory(cache_to_disc=False)
        elif action == "unfollow_artist":
            identity = PARAMS.get("id", "")
            name = PARAMS.get("name", "") or _t(11597)
            if xbmcgui.Dialog().yesno(
                "Spotify",
                _t(11551).format(name),
                nolabel=_t(11419),
                yeslabel=_t(11550),
            ):
                client.save_library_item("artist", identity, save=False)
                from utils import clear_resonance_cache
                clear_resonance_cache()
                xbmcgui.Dialog().notification("Resonance", _t(11515))
                xbmc.executebuiltin("Container.Refresh")
            end_directory(cache_to_disc=False)
        elif action == "create_playlist":
            track_id = PARAMS.get("id", "")
            heading = _t(11516)
            if track_id:
                heading = _t(11517)
            name = (xbmcgui.Dialog().input(heading, defaultt="") or "").strip()
            if name:
                playlist = client.create_playlist(name)
                if track_id and playlist.get("id"):
                    client.add_track_to_playlist(playlist.get("id"), track_id)
                from utils import clear_resonance_cache
                clear_resonance_cache()
                xbmcgui.Dialog().notification("Resonance", _t(11556) + name)
                xbmc.executebuiltin("Container.Refresh")
            end_directory(cache_to_disc=False)
        elif action == "playback_quality":
            from utils import get_playback_settings
            qualities = ("96", "160", "320")
            labels = (_t(11519), _t(11520), _t(11521))
            current = get_playback_settings()[0]
            choice = xbmcgui.Dialog().select(_t(11518), list(labels), preselect=qualities.index(current) if current in qualities else 2)
            if choice >= 0:
                ADDON.setSetting("audio_quality", qualities[choice])
                xbmcgui.Dialog().notification("Resonance", _t(11557) + labels[choice])
            status(client)
        elif action == "whats_new":
            show_release_notes()
            status(client)
        elif action == "playlists":
            items, more = client.playlists(offset)
            render(client, items, "playlist", more, dict(action="playlists", offset=offset))
        elif action == "saved_playlists":
            items, more = client.playlists(offset)
            xbmcplugin.setProperty(HANDLE, "FolderName", "Saved and Followed Playlists")
            render(client, items, "playlist", more, dict(action="saved_playlists", offset=offset))
        elif action == "liked_songs":
            items, more = client.liked_songs(offset)
            xbmcplugin.setProperty(HANDLE, "FolderName", "Liked Songs")
            render(client, items, "track", more, dict(action="liked_songs", offset=offset))
        elif action == "saved_albums":
            items, more = client.saved_albums(offset)
            xbmcplugin.setProperty(HANDLE, "FolderName", "Saved Albums")
            render(client, items, "album", more, dict(action="saved_albums", offset=offset))
        elif action == "followed_artists":
            after = PARAMS.get("after", "")
            items, more = client.followed_artists(after)
            xbmcplugin.setProperty(HANDLE, "FolderName", "Followed Artists")
            render(client, items, "artist", more, dict(action="followed_artists", after=after))
        elif action == "search":
            kind = PARAMS.get("kind", "track")
            query = PARAMS.get("q")
            if not query:
                query = xbmcgui.Dialog().input("Search Spotify").strip()
                if not query:
                    end_directory(succeeded=False)
                    return
                # Give the results directory its own URL containing the query.
                # Kodi then reopens this exact result page when backing out of
                # an album, artist, or playlist opened from the results.
                end_directory(cache_to_disc=False)
                destination = "Container.Update(" + url(
                    action="search", kind=kind, q=query, offset=offset
                ) + ",replace)"
                # Kodi refuses a container update while the plugin directory
                # callback is still active, even after endOfDirectory(). Defer
                # the navigation until this invocation has returned.
                xbmc.executebuiltin("AlarmClock(ResonanceSearch," + destination + ",00:00:01,silent)")
                return
            items, more = client.search(query, kind, offset)
            render(client, items, kind, more, dict(action="search", kind=kind, q=query, offset=offset))
        elif action == "collection":
            kind = PARAMS["kind"]
            items, more = client.tracks(kind, PARAMS["id"], offset)
            xbmcplugin.setProperty(HANDLE, "FolderName", PARAMS.get("name", ""))
            render(client, items, "album" if kind == "artist" else "track", more, dict(PARAMS, offset=offset))
        elif action == "artist_top_tracks":
            items, more = client.artist_top_tracks(PARAMS["id"], PARAMS.get("name", ""))
            xbmcplugin.setProperty(HANDLE, "FolderName", (PARAMS.get("name") or _t(11577)) + " - Top songs")
            render(client, items, "track", more, dict(action=action, id=PARAMS["id"], name=PARAMS.get("name", ""), offset=offset))
        elif action in ("artist_albums", "artist_singles", "artist_appears_on"):
            group = {
                "artist_albums": "album",
                "artist_singles": "single",
                "artist_appears_on": "appears_on",
            }[action]
            title = {
                "album": _t(11479),
                "single": _t(11578),
                "appears_on": _t(11579),
            }[group]
            items, more = client.artist_albums(PARAMS["id"], group, offset)
            xbmcplugin.setProperty(HANDLE, "FolderName", (PARAMS.get("name") or _t(11577)) + " - " + title)
            render(client, items, "album", more, dict(action=action, id=PARAMS["id"], name=PARAMS.get("name", ""), offset=offset))
        elif action == "artist_everything":
            sections = client.artist_everything(PARAMS["id"], PARAMS.get("name", ""))
            render_artist_everything(client, sections, PARAMS.get("name", ""), PARAMS["id"])
        elif action == "play":
            if not spotty_has_credentials():
                xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
                guide_playback_pairing(PARAMS["id"])
                return
            if not token():
                raise catalogue.Unavailable(_t(11450))
            track = client.metadata(client.track(PARAMS["id"]))
            item = track_item(track, stream_url(track, client))
            item.setMimeType("audio/wav")
            item.setContentLookup(False)
            xbmcplugin.setResolvedUrl(HANDLE, True, item)
        elif action == "queue":
            if not spotty_has_credentials():
                guide_playback_pairing()
                if not spotty_has_credentials():
                    return
            tracks = client.cached("queue:" + PARAMS["key"], stale=True) or []
            prepared = []
            for entry in tracks[int(PARAMS.get("index", 0)):]:
                if entry.get("id") and entry.get("duration_ms"):
                    path = stream_url(entry, client)
                    prepared.append((path, track_item(entry, path)))
            if not prepared:
                raise catalogue.Unavailable(_t(11523))
            queue = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
            queue.clear()
            for path, item in prepared:
                queue.add(path, item)
            xbmc.Player().play(queue)
        elif action == "queue_playlist":
            if not spotty_has_credentials():
                guide_playback_pairing()
                if not spotty_has_credentials():
                    return
            playlist_id = PARAMS["id"]
            start_index = max(0, int(PARAMS.get("offset", 0)))
            page_offset = (start_index // 40) * 40
            first_index = start_index - page_offset
            progress = xbmcgui.DialogProgress()
            progress.create("Resonance", "Loading playlist tracks...")
            prepared = []
            try:
                while True:
                    if progress.iscanceled():
                        raise catalogue.Unavailable(_t(11524))
                    page, more = client.tracks("playlist", playlist_id, page_offset)
                    if page_offset == (start_index // 40) * 40:
                        page = page[first_index:]
                    for entry in page:
                        entry = client.metadata(entry)
                        if entry.get("id") and entry.get("duration_ms"):
                            path = stream_url(entry, client)
                            prepared.append((path, track_item(entry, path)))
                    progress.update(0, _t(11526).format(len(prepared)))
                    if more is None:
                        break
                    page_offset = more
                if not prepared:
                    raise catalogue.Unavailable(_t(11525))
            finally:
                progress.close()
            queue = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
            queue.clear()
            for path, item in prepared:
                queue.add(path, item)
            xbmc.Player().play(queue)
        elif action == "open_link":
            value = xbmcgui.Dialog().input("Spotify link or URI").strip()
            match = re.search(r"(?:open\.spotify\.com/(?:intl-[^/]+/)?|spotify:)(track|album|playlist|artist)[/:]([A-Za-z0-9]{22})(?:[?/#]|$)", value)
            if not match:
                if value:
                    row(_t(11490))
                end_directory(cache_to_disc=False)
                return
            kind, identity = match.groups()
            if kind == "track":
                track = client.track(identity)
                render(client, [track], "track", None, dict(action="open_link"))
            else:
                items, more = client.tracks(kind, identity)
                render(client, items, "album" if kind == "artist" else "track", more, dict(action="collection", kind=kind, id=identity))
        elif action == "status":
            status(client)
        elif action == "debug_open_settings":
            xbmc.executebuiltin('Addon.OpenSettings("plugin.audio.resonance")')
            end_directory(cache_to_disc=False)
        elif action == "debug_strings":
            for sid in (11395, 11380, 11109, 11110, 11346, 11384, 11104, 11385, 11386, 11096, 11118):
                value = ADDON.getLocalizedString(sid)
                xbmc.log("RESONANCE_DEBUG str %d => " + str(value), xbmc.LOGINFO)
            end_directory(cache_to_disc=False)
        else:
            row(_t(11491))
            row("Resonance", "home")
            end_directory(cache_to_disc=False)
    except (catalogue.Unavailable, ValueError, KeyError) as exc:
        if action in ("spotify_library", "choose_playlist", "remove_from_playlist", "unfollow_playlist", "unfollow_artist"):
            message = str(exc)
            if "refused" in message.lower() or "403" in message:
                message += "\n\nSpotify may not have granted Resonance permission for this change yet. Open Account and status, sign in with Spotify again, then approve the requested permissions."
            xbmcgui.Dialog().ok(_t(11558), message)
            end_directory(succeeded=False, cache_to_disc=False)
        elif action == "play":
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            xbmcgui.Dialog().notification("Resonance", str(exc))
        elif action == "queue":
            xbmcgui.Dialog().notification("Resonance", str(exc))
        elif action == "queue_playlist":
            xbmcgui.Dialog().notification("Resonance", str(exc))
        elif action == "collection" and PARAMS.get("kind") == "playlist":
            if str(exc) == _t(11601):
                show_playlist_copy_help(PARAMS.get("id", ""), PARAMS.get("name", ""), client)
            else:
                xbmcgui.Dialog().ok("Resonance", str(exc))
            end_directory(succeeded=False, cache_to_disc=False)
        else:
            row(str(exc))
            end_directory(cache_to_disc=False)
    except Exception as exc:
        xbmc.log("Resonance plugin error: " + type(exc).__name__ + ": " + str(exc), xbmc.LOGERROR)
        if action in ("spotify_library", "choose_playlist", "remove_from_playlist", "unfollow_playlist", "unfollow_artist"):
            xbmcgui.Dialog().ok(_t(11558), _t(11559))
            end_directory(succeeded=False, cache_to_disc=False)
        elif action == "play":
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        elif action not in ("queue", "queue_playlist"):
            row(_t(11492))
            end_directory(cache_to_disc=False)
    finally:
        client.close()


if __name__ == "__main__":
    run()
