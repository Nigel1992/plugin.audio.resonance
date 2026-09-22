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


MENU_ICONS = {
    "setup": "DefaultAddonProgram.png",
    "playlists": "DefaultMusicPlaylists.png",
    "search_menu": "DefaultAddonsSearch.png",
    "open_link": "DefaultFile.png",
    "status": "DefaultAddonService.png",
    "refresh_playlists": "DefaultRefresh.png",
    "playback_quality": "DefaultAudioSettings.png",
    "sign_out": "DefaultUser.png",
    "clear_cache": "DefaultHardDisk.png",
}


def row(label, action=None, icon=None, **params):
    item = xbmcgui.ListItem(label)
    # Explicit art stops Kodi probing action paths for a thumbnail.
    art = icon or MENU_ICONS.get(action) or os.path.join(ADDON.getAddonInfo("path"), "resources", "logo.png")
    item.setArt({"icon": art, "thumb": art})
    item.setProperty("IsPlayable", "false")
    # Folder items so a click runs the action; the handler then reloads status.
    xbmcplugin.addDirectoryItem(HANDLE, url(action=action, **params) if action else "", item, bool(action))


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


def status(client):
    from utils import get_playback_settings, get_spotify_device_name, resonance_signed_in, spotty_has_credentials

    signed_in = resonance_signed_in()
    row("Resonance (Spotify) - " + ("Signed in" if signed_in else "Signed out"))
    if not signed_in:
        row("To sign in or retry: Settings > Account & Connection")
        row("Playlists: sign in to load your public playlists")
        row("Set up Spotify", "setup")
        try:
            import web_auth
            from utils import get_spotify_webapi_client_id
            if web_auth.load_login_request(DATA, get_spotify_webapi_client_id(), clock=time.time):
                row("Resume browser sign-in (paste code)", "web_login_paste")
        except Exception:
            pass
    else:
        refreshed = client.cached("playlists-refreshed", stale=True)
        if isinstance(refreshed, dict) and refreshed.get("count"):
            count = int(refreshed.get("count") or 0)
            stamp = float(refreshed.get("at") or 0)
            when = time.strftime("%d %b %H:%M", time.localtime(stamp)) if stamp else "unknown"
            row(f"Playlists: {count} playlist{'s' if count != 1 else ''} - updated {when} (click to manually refresh)", "refresh_playlists")
        else:
            row("Playlists: not loaded yet > tap to refresh now", "refresh_playlists")
        if not token():
            row("Search and playlists token: not set > run setup", "setup")
        device = get_spotify_device_name()
        if spotty_has_credentials():
            row("Playback: paired with '" + device + "' (tap to re-pair)", "connect_playback")
        else:
            row("Playback: not paired > run setup", "setup")
    row("Playback: " + get_playback_settings()[0] + " kbps (select to change)", "playback_quality")
    row("Coming soon: Podcasts and audiobooks")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def render(client, items, kind, more, params):
    if kind == "track":
        items = [client.metadata(item) for item in items]
    xbmcplugin.setContent(HANDLE, {"track": "songs", "album": "albums", "artist": "artists"}.get(kind, "files"))
    if client.notice:
        row(client.notice)
    page_key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    if kind == "track":
        client.save("queue:" + page_key, items, ttl=86400)
    for index, entry in enumerate(items):
        identity = entry.get("id") or ""
        if not re.fullmatch(r"[A-Za-z0-9]{22}", identity):
            continue
        if kind == "track":
            client.save("track:" + identity, entry, ttl=86400)
            path = url(action="play", id=identity)
            item = track_item(entry, path)
            item.addContextMenuItems([("Play this page from here", "RunPlugin(" + url(action="queue", key=page_key, index=index) + ")")])
            xbmcplugin.addDirectoryItem(HANDLE, path, item, False)
        else:
            client.save("collection:" + kind + ":" + identity, entry, ttl=86400)
            path = url(action="collection", kind=kind, id=identity, name=entry.get("name", ""))
            item = xbmcgui.ListItem(entry.get("name") or "Untitled")
            _apply_art(item, _image_url(entry))
            xbmcplugin.addDirectoryItem(HANDLE, path, item, True)
    if more is not None:
        next_params = dict(params, offset=more)
        row("More...", **next_params)
    if not items:
        row("No results")
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


def run():
    from utils import migrate_legacy_addon_data, resonance_signed_in, spotty_has_credentials
    migrate_legacy_addon_data()
    action = PARAMS.get("action", "home")
    if action == "home":
        # Account-specific rows disappear after logout; the public catalogue
        # (search and Spotify links) keeps working without an account.
        if resonance_signed_in():
            row("Playlists", "playlists")
            row("Search", "search_menu")
            if not spotty_has_credentials():
                row("Set up Spotify", "setup")
        else:
            row("Search", "search_menu")
            row("Set up Spotify", "setup")
        row("Open Spotify link", "open_link")
        row("Account and status", "status")
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    if action == "search_menu":
        for kind, label in (("track", "Songs"), ("album", "Albums"), ("artist", "Artists"), ("playlist", "Playlists")):
            row(label, "search", kind=kind)
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
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
        elif action == "search":
            query = PARAMS.get("q") or xbmcgui.Dialog().input("Search Spotify").strip()
            if not query:
                xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
                return
            kind = PARAMS.get("kind", "track")
            items, more = client.search(query, kind, offset)
            render(client, items, kind, more, dict(action="search", kind=kind, q=query, offset=offset))
        elif action == "collection":
            kind = PARAMS["kind"]
            items, more = client.tracks(kind, PARAMS["id"], offset)
            xbmcplugin.setProperty(HANDLE, "FolderName", PARAMS.get("name", ""))
            render(client, items, "album" if kind == "artist" else "track", more, dict(PARAMS, offset=offset))
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
        if action == "play":
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            xbmcgui.Dialog().notification("Resonance", str(exc))
        elif action == "queue":
            xbmcgui.Dialog().notification("Resonance", str(exc))
        elif action == "collection" and PARAMS.get("kind") == "playlist":
            xbmcgui.Dialog().ok("Resonance", str(exc))
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        else:
            row(str(exc))
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
    except Exception as exc:
        xbmc.log("Resonance plugin error: " + type(exc).__name__ + ": " + str(exc), xbmc.LOGERROR)
        if action == "play":
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        elif action != "queue":
            row("Unable to load this page")
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
    finally:
        client.close()


if __name__ == "__main__":
    run()
