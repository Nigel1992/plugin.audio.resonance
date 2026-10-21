"""Small Kodi music browser. No background catalogue requests."""

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
BASE = "plugin://plugin.audio.resonance/"
DATA = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
HANDLE = int(sys.argv[1])
PARAMS = dict(parse_qsl(sys.argv[2].lstrip("?")))


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
    # Folder items so a click runs the action; the handler then reloads status.
    xbmcplugin.addDirectoryItem(HANDLE, url(action=action, **params) if action else "", item, bool(action))


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
            actions.append(("Like on Spotify", "RunPlugin(" + url(action="spotify_library", kind="track", id=identity, save="1") + ")"))
        if liked is None or identity in liked:
            actions.append(("Unlike on Spotify", "RunPlugin(" + url(action="spotify_library", kind="track", id=identity, save="0") + ")"))
        actions.append(("Add to Spotify playlist...", "RunPlugin(" + url(action="choose_playlist", id=identity) + ")"))
        actions.append(("Remove from Spotify playlist...", "RunPlugin(" + url(action="remove_from_playlist", id=identity) + ")"))
        return actions
    if kind == "album" and re.fullmatch(r"[A-Za-z0-9]{22}", identity):
        actions = []
        if saved is None or identity not in saved:
            actions.append(("Save album on Spotify", "RunPlugin(" + url(action="spotify_library", kind="album", id=identity, save="1") + ")"))
        if saved is None or identity in saved:
            actions.append(("Remove album from Spotify library", "RunPlugin(" + url(action="spotify_library", kind="album", id=identity, save="0") + ")"))
        return actions
    return []


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
        "Unfollow playlist",
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
        ("Artist: everything" + suffix, "artist_everything"),
        ("Artist: top songs" + suffix, "artist_top_tracks"),
        ("Artist: albums" + suffix, "artist_albums"),
        ("Artist: singles" + suffix, "artist_singles"),
        ("Artist: appears on" + suffix, "artist_appears_on"),
    ]
    items = [
        (label, "Container.Update(" + url(action=action, id=artist_id, name=artist_name) + ")")
        for label, action in actions
    ]
    if kind == "artist" and followed is not None and artist_id in followed:
        # Only artists the account follows can be unfollowed.
        items.append((
            "Unfollow artist" + suffix,
            "RunPlugin(" + url(action="unfollow_artist", id=artist_id, name=artist_name) + ")",
        ))
    return items


def track_item(track, path):
    item = xbmcgui.ListItem(track.get("name") or "Untitled", path=path)
    item.setProperty("IsPlayable", "true")
    item.setProperty("do_not_analyze", "true")
    album = track.get("album") or {}
    info = item.getMusicInfoTag()
    info.setTitle(track.get("name") or "Untitled")
    info.setArtist(" / ".join(a.get("name", "") for a in track.get("artists", [])))
    info.setAlbum(album.get("name") or "")
    info.setDuration(int(track.get("duration_ms") or 0) // 1000)
    date = str(album.get("release_date") or track.get("release_date") or "")
    if re.match(r"\d{4}", date) and int(date[:4]) > 1900:
        info.setYear(int(date[:4]))
    _apply_art(item, _image_url(track) or _image_url(album))
    return item


def directory_item(entry, kind, path, client=None, saved=None, followed=None):
    item = xbmcgui.ListItem(entry.get("name") or "Untitled")
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

    The page is grouped into short sections with one icon per row so the
    state of the add-on can be read at a glance instead of reading long
    sentences.
    """
    from utils import get_playback_settings, get_spotify_device_name, resonance_signed_in, spotty_has_credentials

    signed_in = resonance_signed_in()
    account_state = xbmcgui.Window(10000).getProperty("resonance.status")

    section("ACCOUNT")
    if signed_in:
        row(account_state or "Signed in", icon="account.png")
        row("Reauthorize Spotify", "web_login", icon="key.png")
    else:
        row(account_state or "Signed out", icon="account.png")
        row("Sign in with Spotify", "web_login", icon="key.png")
        row("Set up Spotify", "setup", icon="setup.png")
        try:
            import web_auth
            from utils import get_spotify_webapi_client_id
            if web_auth.load_login_request(DATA, get_spotify_webapi_client_id(), clock=time.time):
                row("Resume browser sign-in", "web_login_paste", icon="key.png")
        except Exception:
            pass

    section("CATALOGUE")
    refreshed = client.cached("playlists-refreshed", stale=True)
    if isinstance(refreshed, dict) and refreshed.get("count"):
        count = int(refreshed.get("count") or 0)
        stamp = float(refreshed.get("at") or 0)
        when = time.strftime("%d %b %H:%M", time.localtime(stamp)) if stamp else "unknown"
        row(f"{count} playlist{'s' if count != 1 else ''} · {when}", "refresh_playlists", icon="refresh.png")
    else:
        row("Playlists not loaded", "refresh_playlists", icon="refresh.png")
    if not token():
        row("Catalogue token missing", "setup", icon="setup.png")

    section("PLAYBACK")
    row("Playback status", "playback_status", icon="playback.png")
    if spotty_has_credentials():
        row("Connect: " + get_spotify_device_name(), "connect_playback", icon="cast.png")
    else:
        row("Not paired", "setup", icon="setup.png")
    row("Quality: " + get_playback_settings()[0] + " kbps", "playback_quality", icon="quality.png")

    section("MAINTENANCE")
    row("Clear cache", "clear_cache", icon="cache.png")
    row("Sign out", "sign_out", icon="sign_out.png")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def playback_status():
    from utils import get_playback_settings, get_spotify_device_name, spotty_has_credentials

    section("NOW PLAYING")
    try:
        player = xbmc.Player()
        if player.isPlayingAudio():
            tag = player.getMusicInfoTag()
            title = tag.getTitle() or "Unknown track"
            artists = tag.getArtist() or []
            if isinstance(artists, str):
                artists = [artists]
            label = title
            if artists:
                label += " - " + " / ".join(artists)
            row(label, icon="playback.png")
        else:
            row("Nothing playing", icon="playback.png")
    except Exception:
        row("Playback unavailable", icon="playback.png")

    section("SETTINGS")
    row("Quality: " + get_playback_settings()[0] + " kbps", "playback_quality", icon="quality.png")
    if spotty_has_credentials():
        row("Connect: " + get_spotify_device_name(), "connect_playback", icon="cast.png")
    else:
        row("Not paired", "setup", icon="setup.png")
    account_state = xbmcgui.Window(10000).getProperty("resonance.status")
    if account_state:
        section("ACCOUNT")
        row(account_state, icon="account.png")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


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
                                     + artist_context_menu(entry, kind))
            xbmcplugin.addDirectoryItem(HANDLE, path, item, False)
        else:
            client.save("collection:" + kind + ":" + identity, entry, ttl=86400)
            path = url(action="collection", kind=kind, id=identity, name=entry.get("name", ""))
            display_entry = entry
            if kind == "playlist":
                owner = entry.get("owner") or {}
                owner_id = str(owner.get("id") or "") if isinstance(owner, dict) else ""
                if owner_id and client.spotify_user_id and owner_id != client.spotify_user_id:
                    display_entry = dict(entry)
                    if entry.get("collaborative"):
                        marker = "[COLOR skyblue][SHARED][/COLOR] "
                    else:
                        marker = "[COLOR orange][FOLLOWED][/COLOR] "
                    display_entry["name"] = marker + (entry.get("name") or "Untitled")
            item = directory_item(
                display_entry, kind, path, client=client,
                saved=library if kind == "album" else None,
                followed=library if kind == "artist" else None,
            )
            xbmcplugin.addDirectoryItem(HANDLE, path, item, True)
    if more is not None:
        if params.get("action") == "followed_artists":
            next_params = dict(params, after=more)
        else:
            next_params = dict(params, offset=more)
        row("More...", **next_params)
    if not items:
        row("No results")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


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
                xbmcplugin.addDirectoryItem(HANDLE, path, item, False)
            else:
                client.save("collection:" + kind + ":" + identity, entry, ttl=86400)
                path = url(action="collection", kind=kind, id=identity, name=entry.get("name", ""))
                xbmcplugin.addDirectoryItem(HANDLE, path, directory_item(entry, kind, path, client=client, saved=saved), True)
        if kind == "album":
            more_action = {
                "Albums": "artist_albums",
                "Singles": "artist_singles",
                "Appears on": "artist_appears_on",
            }.get(label)
            if more_action:
                row("More " + label.lower() + "...", more_action, id=artist_id, name=artist_name)
    if not sections:
        row("No artist results")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


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
            "Set your own Spotify Web API Client ID in Settings > Advanced first "
            "(create a Spotify app at developer.spotify.com and paste its Client ID).",
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
            title="Spotify Login",
            message="Scan this QR code to sign in, or use the URL below manually on another device.",
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
        xbmcgui.Dialog().notification("Resonance", "The login response did not match this request. Please retry.")
        return
    # The callback never arrived (the Spotify page opened on a device that
    # cannot reach this box). Offer to paste the code from the address bar.
    if xbmcgui.Dialog().yesno(
        "Resonance",
        "Spotify sign-in timed out here.\n\nIf Spotify opened on your phone or computer, the browser now shows a local address with ?code=... . Pick Paste code and enter that value (it is 100-200 characters).\n\nYou can also leave it and pick Resume browser sign-in from Account and status.",
        nolabel="Cancel",
        yeslabel="Paste code",
    ):
        code = xbmcgui.Dialog().input("Code: the part after ?code= in the browser address").strip()
        if code:
            request = web_auth.load_login_request(DATA, client_id)
            if not request:
                web_auth.clear_login_request(DATA)
                xbmcgui.Dialog().notification("Resonance", "The pending sign-in expired. Start again with Sign in with browser.")
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
    window.setProperty("resonance.status", "Signed in")
    try:
        from utils import spotty_has_credentials
        complete = spotty_has_credentials()
    except Exception:
        complete = False
    message = (
        "Fully signed in to Spotify. Search, playlists and playback are ready."
        if complete
        else "Browser sign-in complete. Search and playlists are ready; pair playback from setup when needed."
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
        xbmcgui.Dialog().notification("Resonance", "No pending Spotify sign-in. Start it from Sign in with browser first.")
        return
    code = xbmcgui.Dialog().input("Paste the code: the part after ?code= in the browser address").strip()
    if not code:
        return
    try:
        token_data = web_auth.exchange_code(client_id, request["redirect_uri"], code, request["verifier"])
    except web_auth.WebAuthError as exc:
        web_auth.clear_login_request(DATA)
        xbmcgui.Dialog().ok("Resonance", str(exc))
        return
    _finish_web_login(client_id, request["redirect_uri"], token_data)


def connect_device(navigate, wait_for_playback=False):
    """Guide the user through Spotify Device Connect.

    Browser sign-in already covers the catalogue when ``wait_for_playback``
    is set; this variant waits specifically for the librespot Device-Connect
    pairing that playback requires.  Without it, waits for the normal
    sign-in transition (search token + spotty credentials).
    """
    from utils import get_spotify_device_name, resonance_signed_in, spotty_has_credentials
    window = xbmcgui.Window(10000)
    device = get_spotify_device_name()
    while True:
        window.setProperty("resonance.connect", "1")
        progress = xbmcgui.DialogProgress()
        instruction = (
            "Waiting for playback pairing...\n\nIn Spotify on your phone or PC, tap the speaker icon and pick '{}'."
            if wait_for_playback
            else "Waiting for sign-in...\n\nIn Spotify on your phone or PC, tap the speaker icon and pick '{}'."
        )
        progress.create("Spotify", instruction.format(device))
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
            "Not signed in yet.\n\nIn Spotify on your phone or PC, tap the speaker icon and pick '{}'. Then retry.".format(device),
            noLabel="Cancel",
            yesLabel="Retry",
        )
        if not retry:
            if navigate:
                xbmc.executebuiltin("Container.Update(" + url(action="home") + ",replace)")
            return False


def setup_spotify():
    from utils import spotty_has_credentials
    if not spotty_has_credentials():
        if not connect_device(navigate=False, wait_for_playback=True):
            xbmc.executebuiltin("Container.Update(" + url(action="home") + ",replace)")
            return
        xbmcgui.Dialog().notification(
            "Resonance",
            "Playback pairing complete. Step 1 of 2 done; next, sign in with the URL for search and playlists.",
        )
    if not token():
        web_login()
        return
    xbmcgui.Dialog().notification("Resonance", "Spotify is already set up")
    xbmc.executebuiltin("Container.Update(" + url(action="home") + ",replace)")


def guide_playback_pairing(track_id=None):
    """Walk the user through the one-time Spotify Device Connect pairing.

    Browser sign-in already covers search and playlists; this box still
    needs a librespot session for playback.  The Spotify app picks the box
    from its speaker list once, and credentials are kept for next time.
    """
    from utils import get_spotify_device_name, spotty_has_credentials
    device = get_spotify_device_name()
    if not xbmcgui.Dialog().yesno(
        "Resonance",
        "Search and playlists are ready, but playing songs needs the Spotify app to pair this box once:\n\n"
        "On your phone or PC, open Spotify, tap the speaker icon, and pick '{}'.\n\n"
        "It is waiting to be found now.".format(device),
        nolabel="Cancel",
        yeslabel="Retry",
    ):
        return
    progress = xbmcgui.DialogProgress()
    progress.create(
        "Resonance",
        "Waiting for Spotify to pair...\n\nOn your phone or PC, open Spotify, tap the speaker icon, and pick '{}'.".format(device),
    )
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
        if track_id:
            xbmc.executebuiltin('PlayMedia("' + url(action="play", id=track_id) + '")')
    else:
        xbmcgui.Dialog().notification(
            "Resonance",
            "Not paired yet. Open Spotify, tap the speaker icon, pick '{}', then try again.".format(device),
        )


def stream_url(track, client):
    identity = track.get("id") or ""
    duration = int(track.get("duration_ms") or 0) / 1000
    if not re.fullmatch(r"[A-Za-z0-9]{22}", identity) or duration <= 0:
        raise catalogue.Unavailable("Track metadata unavailable; cannot start playback")
    reason = client.unplayable(track)
    if reason:
        raise catalogue.Unavailable(reason)
    return f"http://127.0.0.1:52309/track/{identity}/{duration}"


def show_playlist_copy_help(identity, name="", client=None):
    """Offer a Spotify link when Spotify blocks reading a followed playlist."""
    title = (name or "this playlist").strip()
    spotify_url = "https://open.spotify.com/playlist/" + identity
    message = (
        "Spotify will not let Resonance read the songs in this playlist because your account does not own it "
        "or collaborate on it. Spotify's API only provides playlist contents to the owner or collaborators, "
        "so Resonance cannot copy the songs automatically.\n\n"
        "To use it in Resonance:\n"
        "1. Open the playlist in Spotify using the link below.\n"
        "2. In Spotify, make your own playlist copy, or ask the owner to add you as a collaborator.\n"
        "3. Return to Resonance and refresh playlists from Account and status.\n\n"
        "Following or saving the playlist to Your Library alone does not grant Resonance access.\n\n"
        "Spotify link:\n" + spotify_url
    )
    xbmcgui.Dialog().textviewer(
        "Playlist unavailable: " + title,
        message,
        usemono=False,
    )
    choice = xbmcgui.Dialog().select(
        "Resonance playlist options",
        [
            "Hide unreadable followed playlists in future",
            "Show Spotify QR code",
            "I made a copy - refresh playlists",
            "Close",
        ],
    )
    if choice == 0:
        ADDON.setSetting("hide_unreadable_playlists", "true")
        xbmcgui.Dialog().notification(
            "Resonance",
            "Unreadable followed playlists will be hidden from the playlist list",
        )
    elif choice == 1:
        from qr_dialog import show_qr_dialog
        show_qr_dialog(
            spotify_url,
            title="Open in Spotify: " + title,
            message="Scan to open this playlist in Spotify. Make your own copy there, then refresh playlists in Resonance.",
        )
    elif choice == 2 and client is not None:
        refreshed = client.refresh_playlists()
        xbmcgui.Dialog().notification(
            "Resonance",
            "Playlists refreshed. Reopen Playlists to find your copy."
            if refreshed else "Could not refresh playlists. Try again from Account and status.",
        )


def run():
    from utils import migrate_legacy_addon_data, resonance_signed_in, spotty_has_credentials
    migrate_legacy_addon_data()
    action = PARAMS.get("action", "home")
    if action == "home":
        # Account-specific rows disappear after logout; the public catalogue
        # (search) keeps working without an account.
        if resonance_signed_in():
            row("Your Spotify Library", "spotify_library_menu")
            row("Search", "search_menu")
            if not spotty_has_credentials():
                row("Set up Spotify", "setup")
        else:
            row("Search", "search_menu")
            row("Set up Spotify", "setup")
        row("Playback status", "playback_status")
        row("Account and status", "status")
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    if action == "spotify_library_menu":
        row("Liked Songs", "liked_songs")
        row("Saved Albums", "saved_albums")
        row("Followed Artists", "followed_artists")
        row("Saved and Followed Playlists", "saved_playlists")
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    if action == "search_menu":
        # Playlists are not searchable. Spotify only returns playlist items to
        # the owner and collaborators, so a global playlist search could only
        # list playlists that Resonance cannot open. Playlists stay reachable
        # through Your Spotify Library.
        for kind, label, icon in (("track", "Songs", "songs.png"),
                                  ("album", "Albums", "albums.png"),
                                  ("artist", "Artists", "artists.png")):
            row(label, "search", icon=icon, kind=kind)
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    if action == "playback_status":
        playback_status()
        return
    if action in ("sign_out", "clear_cache"):
        if action == "sign_out":
            if xbmcgui.Dialog().yesno(
                "Resonance",
                "Sign out of Spotify?\n\nYour playlists and settings are kept. You can sign in again from Settings.",
                nolabel="Cancel",
                yeslabel="Sign out",
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
                    xbmcgui.Dialog().notification("Resonance", "Signed out of Spotify")
                    xbmc.executebuiltin("Container.Refresh")
                else:
                    xbmcgui.Dialog().ok("Resonance", "Could not complete sign out. Please try again.")
        else:
            from utils import clear_resonance_cache
            if xbmcgui.Dialog().yesno(
                "Resonance",
                "Clear the music cache (playlists, search results, rate-limit state)?\n\nYou stay signed in.",
                nolabel="Cancel",
                yeslabel="Clear cache",
            ):
                try:
                    clear_resonance_cache()
                except Exception as exc:
                    xbmc.log("Resonance cache clearing failed: " + str(exc), xbmc.LOGERROR)
                    xbmcgui.Dialog().ok("Resonance", "Could not clear the cache. Please try again.")
                else:
                    xbmcgui.Dialog().notification("Resonance", "Cache cleared")
                    xbmc.executebuiltin("Container.Refresh")
        return
    if action == "setup":
        setup_spotify()
        return
    if action == "web_login":
        web_login()
        return
    if action == "web_login_paste":
        resume_web_login_paste()
        return
    if action in ("connect", "connect_settings"):
        # Both the in-browser button and the Settings action return to a
        # refreshed main menu once the account is signed in.
        setup_spotify()
        return
    if action == "connect_playback":
        # Browser login is done; this waits for the Device-Connect pairing
        # that unlocks playback.
        connect_device(navigate=True, wait_for_playback=True)
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
            xbmcgui.Dialog().notification("Resonance", "Playlists updated" if updated else "Could not refresh playlists")
            status(client)
        elif action == "spotify_library":
            kind = PARAMS.get("kind", "track")
            identity = PARAMS.get("id", "")
            save = PARAMS.get("save") == "1"
            label = ("Like" if kind == "track" else "Save") if save else ("Unlike" if kind == "track" else "Remove")
            if xbmcgui.Dialog().yesno("Spotify", f"{label} this {kind} in your Spotify library?"):
                client.save_library_item(kind, identity, save=save)
                result = ("Saved to Spotify Your Library → Albums" if kind == "album" and save
                          else "Removed from Spotify Your Library → Albums" if kind == "album"
                          else ("Liked on Spotify" if save else "Removed from Spotify Liked Songs"))
                xbmcgui.Dialog().ok("Resonance", result)
                from utils import clear_resonance_cache
                clear_resonance_cache()
                xbmc.executebuiltin("Container.Refresh")
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        elif action == "choose_playlist":
            track_id = PARAMS.get("id", "")
            owned = owned_playlists(client)
            if not owned:
                raise catalogue.Unavailable("No playlists owned by this Spotify account were found")
            choice = xbmcgui.Dialog().select(
                "Add track to Spotify playlist",
                [item.get("name") or "Untitled" for item in owned],
            )
            if choice >= 0:
                target = owned[choice]
                if xbmcgui.Dialog().yesno("Spotify", "Add this track to '{}' ?".format(target.get("name") or "Untitled")):
                    client.add_track_to_playlist(target["id"], track_id)
                    xbmcgui.Dialog().notification("Resonance", "Track added to " + (target.get("name") or "playlist"))
                    from utils import clear_resonance_cache
                    clear_resonance_cache()
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        elif action == "remove_from_playlist":
            track_id = PARAMS.get("id", "")
            owned = owned_playlists(client)
            if not owned:
                raise catalogue.Unavailable("No playlists owned by this Spotify account were found")
            choice = xbmcgui.Dialog().select(
                "Remove track from Spotify playlist",
                [item.get("name") or "Untitled" for item in owned],
            )
            if choice >= 0:
                target = owned[choice]
                name = target.get("name") or "Untitled"
                if xbmcgui.Dialog().yesno("Spotify", "Remove this track from '{}' ?".format(name)):
                    client.remove_track_from_playlist(target["id"], track_id)
                    xbmcgui.Dialog().notification("Resonance", "Track removed from " + name)
                    from utils import clear_resonance_cache
                    clear_resonance_cache()
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        elif action == "unfollow_playlist":
            identity = PARAMS.get("id", "")
            name = PARAMS.get("name", "") or "this playlist"
            if xbmcgui.Dialog().yesno(
                "Spotify",
                "Unfollow '{}'?\n\nIt leaves Your Spotify Library. The playlist itself is not deleted.".format(name),
                nolabel="Cancel",
                yeslabel="Unfollow",
            ):
                client.save_library_item("playlist", identity, save=False)
                from utils import clear_resonance_cache
                clear_resonance_cache()
                xbmcgui.Dialog().notification("Resonance", "Playlist unfollowed")
                xbmc.executebuiltin("Container.Refresh")
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        elif action == "unfollow_artist":
            identity = PARAMS.get("id", "")
            name = PARAMS.get("name", "") or "this artist"
            if xbmcgui.Dialog().yesno(
                "Spotify",
                "Unfollow '{}'?\n\nThe artist leaves your Followed Artists list. The artist itself is not deleted.".format(name),
                nolabel="Cancel",
                yeslabel="Unfollow",
            ):
                client.save_library_item("artist", identity, save=False)
                from utils import clear_resonance_cache
                clear_resonance_cache()
                xbmcgui.Dialog().notification("Resonance", "Artist unfollowed")
                xbmc.executebuiltin("Container.Refresh")
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        elif action == "playback_quality":
            from utils import get_playback_settings
            qualities = ("96", "160", "320")
            labels = ("96 kbps (low)", "160 kbps (normal)", "320 kbps (high)")
            current = get_playback_settings()[0]
            choice = xbmcgui.Dialog().select("Playback quality", list(labels), preselect=qualities.index(current) if current in qualities else 2)
            if choice >= 0:
                ADDON.setSetting("audio_quality", qualities[choice])
                xbmcgui.Dialog().notification("Resonance", "Playback quality: " + labels[choice])
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
                    xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
                    return
                # Give the results directory its own URL containing the query.
                # Kodi then reopens this exact result page when backing out of
                # an album, artist, or playlist opened from the results.
                xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
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
            xbmcplugin.setProperty(HANDLE, "FolderName", (PARAMS.get("name") or "Artist") + " - Top songs")
            render(client, items, "track", more, dict(action=action, id=PARAMS["id"], name=PARAMS.get("name", ""), offset=offset))
        elif action in ("artist_albums", "artist_singles", "artist_appears_on"):
            group = {
                "artist_albums": "album",
                "artist_singles": "single",
                "artist_appears_on": "appears_on",
            }[action]
            title = {
                "album": "Albums",
                "single": "Singles",
                "appears_on": "Appears on",
            }[group]
            items, more = client.artist_albums(PARAMS["id"], group, offset)
            xbmcplugin.setProperty(HANDLE, "FolderName", (PARAMS.get("name") or "Artist") + " - " + title)
            render(client, items, "album", more, dict(action=action, id=PARAMS["id"], name=PARAMS.get("name", ""), offset=offset))
        elif action == "artist_everything":
            sections = client.artist_everything(PARAMS["id"], PARAMS.get("name", ""))
            render_artist_everything(client, sections, PARAMS.get("name", ""), PARAMS["id"])
        elif action == "play":
            if not spotty_has_credentials():
                xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
                guide_playback_pairing(PARAMS["id"])
                return
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
                raise catalogue.Unavailable("This page has no playable tracks")
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
                        raise catalogue.Unavailable("Playlist loading cancelled")
                    page, more = client.tracks("playlist", playlist_id, page_offset)
                    if page_offset == (start_index // 40) * 40:
                        page = page[first_index:]
                    for entry in page:
                        entry = client.metadata(entry)
                        if entry.get("id") and entry.get("duration_ms"):
                            path = stream_url(entry, client)
                            prepared.append((path, track_item(entry, path)))
                    progress.update(0, "Loaded {} tracks...".format(len(prepared)))
                    if more is None:
                        break
                    page_offset = more
                if not prepared:
                    raise catalogue.Unavailable("This playlist has no playable tracks")
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
                    row("Invalid Spotify link")
                xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
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
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        elif action == "debug_strings":
            for sid in (11395, 11380, 11109, 11110, 11346, 11384, 11104, 11385, 11386, 11096, 11118):
                value = ADDON.getLocalizedString(sid)
                xbmc.log("RESONANCE_DEBUG str %d => " + str(value), xbmc.LOGINFO)
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        else:
            row("This old menu has been replaced. Open Resonance from Music add-ons.")
            row("Resonance", "home")
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
    except (catalogue.Unavailable, ValueError, KeyError) as exc:
        if action in ("spotify_library", "choose_playlist", "remove_from_playlist", "unfollow_playlist", "unfollow_artist"):
            message = str(exc)
            if "refused" in message.lower() or "403" in message:
                message += "\n\nSpotify may not have granted Resonance permission for this change yet. Choose Reauthorize Spotify under Account and status, then approve the requested permissions."
            xbmcgui.Dialog().ok("Spotify change failed", message)
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        elif action == "play":
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            xbmcgui.Dialog().notification("Resonance", str(exc))
        elif action == "queue":
            xbmcgui.Dialog().notification("Resonance", str(exc))
        elif action == "queue_playlist":
            xbmcgui.Dialog().notification("Resonance", str(exc))
        elif action == "collection" and PARAMS.get("kind") == "playlist":
            if "Spotify will not let Resonance read this playlist" in str(exc):
                show_playlist_copy_help(PARAMS.get("id", ""), PARAMS.get("name", ""), client)
            else:
                xbmcgui.Dialog().ok("Resonance", str(exc))
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        else:
            row(str(exc))
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
    except Exception as exc:
        xbmc.log("Resonance plugin error: " + type(exc).__name__ + ": " + str(exc), xbmc.LOGERROR)
        if action in ("spotify_library", "choose_playlist", "remove_from_playlist", "unfollow_playlist", "unfollow_artist"):
            xbmcgui.Dialog().ok("Spotify change failed", "Resonance could not complete the Spotify request. Check the Kodi log for details.")
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        elif action == "play":
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        elif action not in ("queue", "queue_playlist"):
            row("Unable to load this page")
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
    finally:
        client.close()


if __name__ == "__main__":
    run()
