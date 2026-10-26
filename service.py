"""Authentication and loopback audio only; no catalogue polling."""

import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import xbmc
import xbmcgui

from resources.lib import utils
from spotty import get_spotty
from spotty_auth import SpottyAuth
from spotty_helper import SpottyHelper
from spotty_audio_streamer import SpottyAudioStreamer
from connect_bridge import ConnectBridge, LIVE_WAV_HEADER


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


PAIRING_CHECK_SECONDS = 5
PAIRING_FAILURE_LIMIT = 3


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _new_pairing_failures(path: str, offset: int):
    """Count Spotify pairing rejections written since ``offset``."""
    try:
        if os.path.getsize(path) < offset:
            offset = 0
        with open(path, "rb") as stream:
            stream.seek(offset)
            data = stream.read()
    except OSError:
        return 0, offset
    return data.count(b"MAC mismatch"), offset + len(data)


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
        if xbmcgui.Window(10000).getProperty("resonance.setup-active"):
            # The guided setup owns the screen; a container update here would
            # be refused behind its dialogs and leave a busy spinner behind.
            return
        if "plugin://plugin.audio.resonance/" not in (xbmc.getInfoLabel("Container.FolderPath") or ""):
            return
        xbmc.executebuiltin("Container.Update(plugin://plugin.audio.resonance/?action=home,replace)")
        xbmc.log("Resonance sign-in: returned to main menu", xbmc.LOGINFO)
    except Exception as exc:
        xbmc.log("Resonance sign-in navigation failed: " + type(exc).__name__ + ": " + str(exc), xbmc.LOGWARNING)


def refresh_current_resonance_menu() -> None:
    """Reload the visible Resonance folder after settings change."""
    try:
        if xbmc.getCondVisibility("Container.IsUpdating"):
            return
        path = xbmc.getInfoLabel("Container.FolderPath") or ""
        if "plugin://plugin.audio.resonance/" not in path:
            return
        xbmc.executebuiltin("Container.Update(" + quote(path, safe=":/?&=%") + ",replace)")
        xbmc.log("Resonance settings changed: refreshed current menu", xbmc.LOGINFO)
    except Exception as exc:
        xbmc.log("Resonance settings refresh failed: " + type(exc).__name__ + ": " + str(exc), xbmc.LOGWARNING)


class ResonanceMonitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.hide_unreadable_playlists = self._hide_unreadable_playlists()
        self.menu_settings = self._menu_settings()

    @staticmethod
    def _menu_settings():
        # Internal settings such as last_notes_version must not navigate the
        # browser while its plugin invocation is creating a directory.
        return (tuple(utils.get_playback_settings()), utils.get_spotify_webapi_client_id())

    @staticmethod
    def _hide_unreadable_playlists() -> bool:
        try:
            return bool(utils.get_catalogue_settings().get("hide_unreadable_playlists"))
        except Exception:
            return False

    def onSettingsChanged(self):
        try:
            utils.reload_verbose_debug_logging()
        except Exception:
            pass
        hide_unreadable_playlists = self._hide_unreadable_playlists()
        menu_settings = self._menu_settings()
        changed = (hide_unreadable_playlists != self.hide_unreadable_playlists
                   or menu_settings != self.menu_settings)
        self.menu_settings = menu_settings
        if hide_unreadable_playlists != self.hide_unreadable_playlists:
            self.hide_unreadable_playlists = hide_unreadable_playlists
            try:
                utils.clear_resonance_cache()
                utils.clear_resonance_addon_cache()
                xbmc.log("Resonance settings changed: cleared catalogue and addon caches after playlist hide toggle", xbmc.LOGINFO)
                notify(
                    "Playlist visibility changed. Resonance cleared its catalogue and addon caches, then refreshed the menu.",
                    "Resonance settings",
                )
            except Exception as exc:
                xbmc.log("Resonance settings cache clear failed: " + type(exc).__name__ + ": " + str(exc), xbmc.LOGWARNING)
        if changed:
            refresh_current_resonance_menu()


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
        self.connect = None

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
        if self.path.startswith("/connect/"):
            self.live_connect(head)
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

    def live_connect(self, head):
        bridge = self.server.connect
        if bridge is None or not bridge.active or self.path != bridge.stream_path():
            self.send_error(404)
            return
        generation = bridge.audio.generation
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if head:
            return
        reader = bridge.audio.claim_reader()
        xbmc.log("Resonance Connect HTTP reader=" + str(reader) + " generation=" + str(generation), xbmc.LOGDEBUG)
        written = 0
        try:
            self.connection.settimeout(15)
            # Give the live decoder a short reserve before Kodi begins reading.
            # Native decoding is paced in real time, so a zero-reserve reader
            # would expose every scheduling/network delay to the audio sink.
            deadline = time.monotonic() + 3
            with bridge.audio.condition:
                while (bridge.audio.generation == generation and not bridge.audio.closed
                       and sum(map(len, bridge.audio.readers.get(reader, ()))) < 176400
                       and time.monotonic() < deadline):
                    bridge.audio.condition.wait(0.1)
            self.wfile.write(LIVE_WAV_HEADER)
            next_report = time.monotonic() + 30
            while True:
                chunk = bridge.audio.read(generation, reader)
                if chunk == b"":
                    break
                if chunk is not None:
                    self.wfile.write(chunk)
                    written += len(chunk)
                if time.monotonic() >= next_report:
                    xbmc.log("Resonance Connect buffer: received={} dropped={} waits={} sent={}".format(
                        bridge.audio.received, bridge.audio.dropped, bridge.audio.waits, written), xbmc.LOGDEBUG)
                    next_report = time.monotonic() + 30
        except (OSError, ConnectionError) as exc:
            xbmc.log("Resonance Connect HTTP: " + str(exc), xbmc.LOGINFO)
        finally:
            bridge.audio.release_reader(reader)
            xbmc.log("Resonance Connect HTTP closed reader={} bytes={} generation={}/{}".format(reader, written, generation, bridge.audio.generation), xbmc.LOGDEBUG)


class Player(xbmc.Player):
    def __init__(self, server):
        super().__init__()
        self.server = server

    def onAVStarted(self):
        if self.server.connect is not None:
            self.server.connect.kodi_started()

    def onPlayBackPaused(self):
        if self.server.connect is not None:
            self.server.connect.kodi_paused()

    def onPlayBackResumed(self):
        if self.server.connect is not None:
            self.server.connect.kodi_resumed()

    def onPlayBackStopped(self):
        self.server.stop_audio()
        if self.server.connect is not None:
            self.server.connect.kodi_stopped()

    def onPlayBackError(self):
        self.server.stop_audio()


def run():
    utils.migrate_legacy_addon_data()
    monitor = ResonanceMonitor()
    window = xbmcgui.Window(10000)
    spotty = get_spotty(SpottyHelper(), cache_directory=utils.RUNTIME_PATH)
    auth = SpottyAuth(spotty)
    server = AudioServer(spotty)
    player = Player(server)
    connect = ConnectBridge(spotty, auth, player, utils.RUNTIME_PATH)
    server.connect = connect
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    next_auth = 0
    active_client_id = utils.get_spotify_webapi_client_id()
    next_playlist_refresh = 0.0
    # None on startup so a device that was already paired does not pop a
    # notification on every Kodi launch; only a live pairing transition does.
    was_signed_in = None
    pairing_log = os.path.join(utils.RUNTIME_PATH, "device-connect.log")
    pairing_offset = _file_size(pairing_log)
    pairing_failures = 0
    next_pairing_check = 0.0

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
                    connect.stop()
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
            connect_requested = bool(window.getProperty("resonance.connect"))
            if now >= next_auth or connect_requested:
                window.clearProperty("resonance.connect")
                signed_in = auth.has_credentials() or _web_session_available()
                if signed_in:
                    if auth.has_credentials():
                        if connect.available:
                            connect.enabled = True
                        else:
                            auth.ensure_spotify_connect_receiver_running()
                    else:
                        # Browser login covers the catalogue; playback still
                        # needs a librespot session. Advertise the box so the
                        # user can pair it from Spotify (speaker picker).
                        auth.ensure_device_connect_running(
                            fresh=False, force=connect_requested
                        )
                    access = restore_access_token()
                    window.setProperty("resonance.status", "Signed in" if access else "Signed in; catalogue token unavailable")
                    window.setProperty("resonance.signed-in", "1")
                    if was_signed_in is False and not window.getProperty("resonance.setup-active"):
                        # Only claim what this step really did: the browser
                        # token is the catalogue sign-in, playback pairing is
                        # the Device-Connect credentials.
                        if _web_session_available() and auth.has_credentials():
                            notify("Fully signed in to Spotify")
                        elif auth.has_credentials():
                            notify("Playback pairing complete. Next, sign in with the URL for search and playlists.")
                        else:
                            notify("Browser sign-in complete. Search and playlists are ready.")
                        return_to_home()
                    was_signed_in = True
                    next_auth = now + (3000 if access else 60)
                else:
                    # An explicit pairing request from the GUI always gets a
                    # fresh attempt, even when the automatic retries are
                    # waiting out their backoff.
                    auth.ensure_device_connect_running(
                        fresh=False, force=connect_requested
                    )
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
            if not auth.has_credentials() and now >= next_pairing_check:
                next_pairing_check = now + PAIRING_CHECK_SECONDS
                failures, pairing_offset = _new_pairing_failures(pairing_log, pairing_offset)
                pairing_failures += failures
                if pairing_failures >= PAIRING_FAILURE_LIMIT:
                    pairing_failures = 0
                    xbmc.log("Resonance pairing rejected repeatedly; restarting the pairing receiver", xbmc.LOGWARNING)
                    notify(
                        "Spotify rejected this pairing. Restarting the pairing receiver; pick this box in Spotify again.",
                    )
                    auth.ensure_device_connect_running(fresh=True, force=True)
                    pairing_offset = _file_size(pairing_log)
            connect.tick()
            if monitor.waitForAbort(1):
                break
    finally:
        connect.stop()
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
