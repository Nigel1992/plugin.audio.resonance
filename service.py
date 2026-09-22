"""Authentication and loopback audio only; no catalogue polling."""

import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import xbmc
import xbmcgui

from resources.lib import utils
from spotty import get_spotty
from spotty_auth import SpottyAuth
from spotty_helper import SpottyHelper
from spotty_audio_streamer import SpottyAudioStreamer


def byte_range(value, size):
    if not value:
        return 0, size - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()):
        raise ValueError("Invalid range")
    first, last = match.groups()
    if not first:
        length = int(last)
        if length <= 0:
            raise ValueError("Invalid suffix")
        return max(0, size - length), size - 1
    start, end = int(first), min(int(last), size - 1) if last else size - 1
    if start > end:
        raise ValueError("Range outside stream")
    return start, end


# Refresh the public playlist listing every half hour while signed in.
PLAYLIST_REFRESH_SECONDS = 30 * 60


def notify(message: str, heading: str = "Resonance") -> None:
    """Show a short Kodi notification without breaking the service loop."""
    xbmc.log(f"Resonance notification: {heading}: {message}", xbmc.LOGINFO)
    try:
        xbmcgui.Dialog().notification(heading, message, xbmcgui.NOTIFICATION_INFO, 5000)
    except Exception:
        pass


def return_to_home() -> None:
    """Refresh the addon home after sign-in when Resonance is on screen.

    Pairing is handled by this service, so the browser does not always see the
    transition; reloading the home listing here makes the account rows appear
    (and returns the user to the main menu) without yanking them out of
    whatever non-Resonance window they are using.
    """
    try:
        if "plugin://plugin.audio.resonance/" not in (xbmc.getInfoLabel("Container.FolderPath") or ""):
            return
        xbmc.executebuiltin("Container.Update(plugin://plugin.audio.resonance/?action=home,replace)")
        xbmc.log("Resonance sign-in: returned to main menu", xbmc.LOGINFO)
    except Exception as exc:
        xbmc.log("Resonance sign-in navigation failed: " + type(exc).__name__ + ": " + str(exc), xbmc.LOGWARNING)


def refresh_playlists() -> None:
    """Best-effort background refresh of the public playlist listing."""
    from resources.lib import catalogue as _catalogue

    client = None
    try:
        client = _catalogue.Catalogue(utils.ADDON_DATA_PATH, utils.get_cached_auth_token,
                                      signed_in=True, **utils.get_catalogue_settings())
        if client.refresh_playlists():
            xbmc.log("Resonance playlist refresh: updated", xbmc.LOGINFO)
        else:
            xbmc.log("Resonance playlist refresh: no change", xbmc.LOGDEBUG)
    except Exception as exc:
        xbmc.log(
            "Resonance playlist refresh failed: " + type(exc).__name__ + ": " + str(exc),
            xbmc.LOGWARNING,
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


class AudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, spotty):
        super().__init__(("127.0.0.1", 52309), AudioRequest)
        self.spotty = spotty
        self.active = set()
        self.lock = threading.Lock()

    def stop_audio(self):
        with self.lock:
            for stream in self.active:
                stream.terminate_stream()
            self.active.clear()


class AudioRequest(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_HEAD(self):
        self.audio(head=True)

    def do_GET(self):
        self.audio(head=False)

    def audio(self, head):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            if not head:
                self.wfile.write(b"ok")
            return
        match = re.fullmatch(r"/track/([A-Za-z0-9]{22})/(\d+(?:\.\d+)?)", self.path)
        if not match or not 0 < float(match[2]) <= 21600:
            self.send_error(404)
            return
        identity, duration = match[1], float(match[2])
        size = 44 + max(1, round(duration * 44100)) * 4
        try:
            start, end = byte_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(206 if self.headers.get("Range") else 200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if self.headers.get("Range"):
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        self.connection.settimeout(60)
        stream = SpottyAudioStreamer(self.server.spotty)
        try:
            stream.configure_playback(*utils.get_playback_settings())
        except Exception as exc:
            xbmc.log("Resonance playback settings ignored: " + type(exc).__name__ + ": " + str(exc), xbmc.LOGWARNING)
        stream.set_track(identity, duration)
        with self.server.lock:
            # Kodi may buffer the next song while the current reader is
            # still active. Each HTTP reader owns its decoder lifetime.
            self.server.active.add(stream)
        try:
            remaining = end - start + 1
            written = 0
            for chunk in stream.send_part_audio_stream(end - start + 1, start):
                chunk = chunk[:remaining]
                self.wfile.write(chunk)
                written += len(chunk)
                remaining -= len(chunk)
                if remaining <= 0:
                    break
        except (OSError, ConnectionError):
            pass
        finally:
            if written == 0:
                from resources.lib import catalogue as _catalogue
                _catalogue.remember_failed(utils.ADDON_DATA_PATH, identity)
            stream.terminate_stream()
            with self.server.lock:
                self.server.active.discard(stream)


class Player(xbmc.Player):
    def __init__(self, server):
        super().__init__()
        self.server = server

    def onPlayBackStopped(self):
        self.server.stop_audio()

    def onPlayBackError(self):
        self.server.stop_audio()


def run():
    utils.migrate_legacy_addon_data()
    monitor = xbmc.Monitor()
    window = xbmcgui.Window(10000)
    spotty = get_spotty(SpottyHelper(), cache_directory=utils.RUNTIME_PATH)
    auth = SpottyAuth(spotty)
    server = AudioServer(spotty)
    player = Player(server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    next_auth = 0
    active_client_id = utils.get_spotify_webapi_client_id()
    next_playlist_refresh = 0.0
    # None on startup so a device that was already paired does not pop a
    # notification on every Kodi launch; only a live pairing transition does.
    was_signed_in = None

    def _web_session_available():
        try:
            return utils.web_auth().has_session()
        except Exception:
            return False

    def restore_access_token():
        """Return a usable catalogue access token.

        Prefers the regular browser-authorization-code token (which the
        Spotify Web API accepts without rate-limiting); otherwise falls back
        to the legacy Spotty Device-Connect token.
        """
        try:
            web = utils.web_auth()
        except Exception:
            web = None
        if web is not None and web.has_session():
            try:
                access = web.access_token()
            except Exception as exc:
                xbmc.log("Resonance web token refresh error: " + str(exc), xbmc.LOGERROR)
                access = ""
            if access:
                stored = web.load() or {}
                try:
                    expires_at = int(stored.get("expires_at") or (time.time() + 3600))
                except (TypeError, ValueError):
                    expires_at = int(time.time()) + 3600
                utils.cache_auth_token(access)
                utils.cache_auth_token_expires_at(str(expires_at))
                utils.cache_auth_client_id(utils.get_spotify_webapi_client_id())
                return access
        if auth.has_credentials():
            return auth.renew_token(quiet=True)
        return ""

    try:
        while not monitor.abortRequested():
            now = time.monotonic()
            client_id = utils.get_spotify_webapi_client_id()
            if client_id != active_client_id:
                active_client_id = client_id
                utils.cache_auth_token("")
                utils.cache_auth_token_expires_at("")
                utils.cache_auth_client_id("")
                next_auth = 0
            if window.getProperty("resonance.logout"):
                window.clearProperty("resonance.logout")
                try:
                    server.stop_audio()
                    auth.stop_device_connect_process()
                    utils.delete_resonance_credentials()
                    utils.clear_resonance_cache()
                    window.clearProperty("resonance.signed-in")
                    window.setProperty("resonance.status", "Signed out")
                    was_signed_in = False
                    next_auth = 0
                    window.setProperty("resonance.logout.result", "ok")
                except Exception as exc:
                    xbmc.log("Resonance logout failed: " + str(exc), xbmc.LOGERROR)
                    window.setProperty("resonance.logout.result", "error")
            if now >= next_auth or window.getProperty("resonance.connect"):
                window.clearProperty("resonance.connect")
                signed_in = auth.has_credentials() or _web_session_available()
                if signed_in:
                    if auth.has_credentials():
                        auth.ensure_spotify_connect_receiver_running()
                    else:
                        # Browser login covers the catalogue; playback still
                        # needs a librespot session. Advertise the box so the
                        # user can pair it from Spotify (speaker picker).
                        auth.ensure_device_connect_running(fresh=False)
                    access = restore_access_token()
                    window.setProperty("resonance.status", "Signed in" if access else "Signed in; catalogue token unavailable")
                    window.setProperty("resonance.signed-in", "1")
                    if was_signed_in is False:
                        if access and auth.has_credentials():
                            notify("Fully signed in to Spotify")
                        elif auth.has_credentials():
                            notify("Playback pairing complete. Next, sign in with the URL for search and playlists.")
                        else:
                            notify("Browser sign-in complete. Search and playlists are ready.")
                        return_to_home()
                    was_signed_in = True
                    next_auth = now + (3000 if access else 60)
                else:
                    auth.ensure_device_connect_running(fresh=False)
                    window.setProperty("resonance.status", "Waiting for Spotify connection")
                    window.clearProperty("resonance.signed-in")
                    was_signed_in = False
                    next_auth = now + 5
                    next_playlist_refresh = 0.0
            if (
                window.getProperty("resonance.signed-in") == "1"
                and now >= next_playlist_refresh
            ):
                next_playlist_refresh = now + PLAYLIST_REFRESH_SECONDS
                threading.Thread(target=refresh_playlists, daemon=True).start()
            if monitor.waitForAbort(1):
                break
    finally:
        server.stop_audio()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        auth.stop_device_connect_process()
        window.clearProperty("resonance.status")
        window.clearProperty("resonance.signed-in")
        del player


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        xbmc.log("Resonance service error: " + type(exc).__name__ + ": " + str(exc), xbmc.LOGERROR)
