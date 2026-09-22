import inspect
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import sys
import time
import unicodedata
from traceback import format_exception
from typing import Any, Dict, List, Tuple, Union

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
from xbmc import LOGDEBUG, LOGINFO, LOGERROR, LOGWARNING

from process_utils import terminate_process

DEBUG_SETTING_ID = "verbose_debug_logging"
_VERBOSE_DEBUG_LOGGING = None


def reload_verbose_debug_logging() -> bool:
    """Reload the debug switch after Kodi commits the add-on settings.

    The service is a long-lived Python process.  Keeping the old cached value
    until the next Kodi restart made a settings change appear ineffective.
    """
    global _VERBOSE_DEBUG_LOGGING
    _VERBOSE_DEBUG_LOGGING = None
    return verbose_debug_logging_enabled()


def verbose_debug_logging_enabled() -> bool:
    """Return cached verbose logging state for this Python invocation.

    The Kodi addon object is deliberately opened at most once. Logging must
    never repeatedly enter xbmcaddon while Device Connect or Spotty is active.
    """
    global _VERBOSE_DEBUG_LOGGING

    if _VERBOSE_DEBUG_LOGGING is not None:
        return _VERBOSE_DEBUG_LOGGING

    enabled = False
    try:
        addon = xbmcaddon.Addon(id=ADDON_ID)
        try:
            enabled = addon.getSettingBool(DEBUG_SETTING_ID)
        except Exception:
            enabled = addon.getSetting(DEBUG_SETTING_ID).lower() == "true"
    except Exception:
        enabled = False

    _VERBOSE_DEBUG_LOGGING = bool(enabled)
    return _VERBOSE_DEBUG_LOGGING

# Separate Resonance OAuth/Bottle/Connect port
PROXY_PORT = 52309
# Windows can resolve localhost through IPv6 first while the local Bottle server
# is reached through IPv4, causing a long first-connect fallback. Keep other
# platforms unchanged until their behavior has been verified independently.
LOCAL_STREAM_HOST = "127.0.0.1" if platform.system() == "Windows" else "localhost"

# Independent addon instance
ADDON_ID = "plugin.audio.resonance"

ADDON_DATA_PATH = xbmcvfs.translatePath(
    f"special://profile/addon_data/{ADDON_ID}"
)

# Compatibility data source for one-time profile migration.
LEGACY_ADDON_ID = "plugin.audio.spotify2"
LEGACY_ADDON_DATA_PATH = xbmcvfs.translatePath(
    f"special://profile/addon_data/{LEGACY_ADDON_ID}"
)


def migrate_legacy_addon_data() -> None:
    """Copy compatible existing profile data on first start.

    Credentials, cache, settings and playback state may already exist under a
    compatible profile id. This is a one-way, non-destructive copy: nothing is
    removed from the source directory and existing profile files are never
    overwritten.
    """
    _migrate_runtime_directory()

    if ADDON_ID == LEGACY_ADDON_ID:
        return

    marker = os.path.join(ADDON_DATA_PATH, ".legacy-data-migrated")
    try:
        if os.path.exists(marker):
            return
        if not os.path.isdir(LEGACY_ADDON_DATA_PATH):
            return
        os.makedirs(ADDON_DATA_PATH, exist_ok=True)
        migrated = 0
        for name in os.listdir(LEGACY_ADDON_DATA_PATH):
            source = os.path.join(LEGACY_ADDON_DATA_PATH, name)
            target = os.path.join(ADDON_DATA_PATH, name)
            if os.path.exists(target):
                continue
            try:
                if os.path.isdir(source):
                    shutil.copytree(source, target)
                else:
                    shutil.copy2(source, target)
                migrated += 1
            except OSError as exc:
                log_msg(f"Legacy data migration skipped {name}: {exc}", LOGWARNING)
        try:
            with open(marker, "w", encoding="ascii") as handle:
                handle.write(LEGACY_ADDON_ID)
        except OSError:
            pass
        log_msg(
            f"Migrated {migrated} legacy entries from {LEGACY_ADDON_ID} into {ADDON_DATA_PATH}",
            LOGINFO,
        )
    except OSError as exc:
        log_msg(f"Legacy data migration failed: {exc}", LOGWARNING)


ADDON_WINDOW_ID = 10000
SPOTIFY_ACCOUNT_PROFILE_FILE = os.path.join(ADDON_DATA_PATH, "spotify-account.json")
SPOTIFY_DEVICE_ID_FILE = os.path.join(ADDON_DATA_PATH, "device-name-id")

# Runtime state owned by the service (credentials, spotty token, volume).
RUNTIME_PATH = os.path.join(ADDON_DATA_PATH, "runtime")
# Pre-rename location, migrated once by _migrate_runtime_directory().
LEGACY_RUNTIME_PATH = os.path.join(ADDON_DATA_PATH, "runtime-v2")


def _migrate_runtime_directory() -> None:
    """Move runtime state from the pre-rename ``runtime-v2`` directory."""
    if os.path.isdir(RUNTIME_PATH) or not os.path.isdir(LEGACY_RUNTIME_PATH):
        return
    try:
        os.rename(LEGACY_RUNTIME_PATH, RUNTIME_PATH)
    except OSError:
        try:
            shutil.copytree(LEGACY_RUNTIME_PATH, RUNTIME_PATH)
        except OSError as exc:
            log_msg(f"Runtime directory migration failed: {exc}", LOGWARNING)


def resonance_signed_in() -> bool:
    """Return True when either reusable Spotify credentials are present.

    ``credentials.json`` from Device Connect or Web-Auth tokens from the
    regular browser authorization-code login both count as signed in.  A
    partial/truncated credentials file must not count as signed in.
    """
    credentials_file = os.path.join(RUNTIME_PATH, "credentials.json")
    try:
        if os.path.getsize(credentials_file) >= 32:
            with open(credentials_file, "r", encoding="utf-8") as stream:
                credentials = json.load(stream)
            username = (credentials.get("username") or "").strip()
            auth_data = credentials.get("auth_data") or credentials.get("authData") or ""
            if username and auth_data:
                return True
    except (OSError, ValueError, TypeError):
        pass
    try:
        from web_auth import WebAuth
        return WebAuth(ADDON_DATA_PATH, get_spotify_webapi_client_id()).has_session()
    except Exception:
        return False


def spotty_has_credentials() -> bool:
    """Return True only when librespot Device-Connect credentials exist.

    The browser token powers the catalogue; playback still requires the
    Device-Connect session written to ``runtime/credentials.json``.
    """
    credentials_file = os.path.join(RUNTIME_PATH, "credentials.json")
    try:
        if os.path.getsize(credentials_file) < 32:
            return False
        with open(credentials_file, "r", encoding="utf-8") as stream:
            credentials = json.load(stream)
        username = (credentials.get("username") or "").strip()
        auth_data = credentials.get("auth_data") or credentials.get("authData") or ""
        return bool(username and auth_data)
    except (OSError, ValueError, TypeError):
        return False


def web_auth() -> "WebAuth":
    """Return the browser-authorization-code token store for this add-on."""
    from web_auth import WebAuth
    return WebAuth(ADDON_DATA_PATH, get_spotify_webapi_client_id())


KODI_PROPERTY_SPOTIFY_AUTH_TOKEN = f"{ADDON_ID}.spotify-auth-token"
KODI_PROPERTY_AUTH_TOKEN_EXPIRES_AT = f"{ADDON_ID}.spotify-auth-token-expires-at"
KODI_PROPERTY_SPOTIFY_AUTH_CLIENT_ID = f"{ADDON_ID}.spotify-auth-client-id"
KODI_PROPERTY_SPOTIFY_ACCOUNT_EMAIL = f"{ADDON_ID}.spotify.account.email"
KODI_PROPERTY_SPOTIFY_ACCOUNT_NAME = f"{ADDON_ID}.spotify.account.name"
KODI_PROPERTY_SPOTIFY_ACCOUNT_ID = f"{ADDON_ID}.spotify.account.id"


def log_msg(
    msg: str,
    loglevel: int = LOGDEBUG,
    caller_name: str = ""
) -> None:

    if loglevel == LOGDEBUG and verbose_debug_logging_enabled():
        loglevel = LOGINFO

    if not caller_name:
        frame = inspect.currentframe()
        try:
            caller_frame = frame.f_back if frame else None
            caller_name = get_formatted_caller_name(
                caller_frame.f_code.co_filename if caller_frame else "",
                caller_frame.f_code.co_name if caller_frame else ""
            )
        finally:
            del frame

    xbmc.log(
        f"{ADDON_ID}:{caller_name}: {msg}",
        level=loglevel
    )


def log_exception(
    exc: Exception,
    exception_details: str
) -> None:

    frame = inspect.currentframe()
    try:
        caller_frame = frame.f_back if frame else None
        the_caller_name = get_formatted_caller_name(
            caller_frame.f_code.co_filename if caller_frame else "",
            caller_frame.f_code.co_name if caller_frame else ""
        )
    finally:
        del frame

    # Kodi 21 platform builds do not all embed the same Python minor version.
    # Python 3.8 still requires the classic (type, value, traceback) form,
    # while newer runtimes also accept the exception alone.  The classic form
    # remains valid on both Kodi 21 and 22 and must never mask the real error.
    log_msg(
        " ".join(format_exception(type(exc), exc, exc.__traceback__)),
        loglevel=LOGERROR,
        caller_name=the_caller_name
    )

    log_msg(
        f"Exception --> {exception_details}.",
        loglevel=LOGERROR,
        caller_name=the_caller_name
    )


def get_formatted_caller_name(
    filename: str,
    function_name: str
) -> str:

    return (
        f"{os.path.splitext(os.path.basename(filename))[0]}"
        f":{function_name}"
    )


def get_time_str(raw_time: int) -> str:
    return time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(float(raw_time))
    )


def get_username() -> str:

    addon = xbmcaddon.Addon(
        id=ADDON_ID
    )

    spotify_username = addon.getSetting(
        "username"
    )

    if not spotify_username:
        raise Exception(
            "Could not get spotify username."
        )

    return spotify_username


def kill_this_plugin() -> None:
    sys.exit(1)


def kill_process_by_pid(pid: int) -> None:

    try:
        if platform.system() != "Windows":
            os.kill(
                pid,
                signal.SIGKILL
            )

    except OSError:
        pass


def bytes_to_megabytes(byts: int) -> float:
    return (
        byts / 1024.0
    ) / 1024.0


def get_chunks(
    data,
    chunk_size: int
):
    return [
        data[x:x + chunk_size]
        for x in range(0, len(data), chunk_size)
    ]


def try_encode(
    text,
    encoding="utf-8"
):

    try:
        return text.encode(
            encoding,
            "ignore"
        )

    except UnicodeEncodeError:
        return text


def try_decode(
    text,
    encoding="utf-8"
):

    try:
        return text.decode(
            encoding,
            "ignore"
        )

    except UnicodeDecodeError:
        return text


def normalize_string(text):

    text = text.replace(":", "")
    text = text.replace("/", "-")
    text = text.replace("\\", "-")
    text = text.replace("<", "")
    text = text.replace(">", "")
    text = text.replace("*", "")
    text = text.replace("?", "")
    text = text.replace("|", "")
    text = text.replace("(", "")
    text = text.replace(")", "")
    text = text.replace('"', "")
    text = text.strip()
    text = text.rstrip(".")

    return unicodedata.normalize(
        "NFKD",
        try_decode(text)
    )



def get_spotify_webapi_client_id() -> str:
    """Return the validated Spotify Client ID the user configured themselves.

    There is no bundled default: signing in with a browser requires a Client
    ID that belongs to your own Spotify app (created on the Spotify Developer
    Dashboard and entered in Settings > Advanced). Reading this setting must
    never rewrite it. Kodi may publish intermediate edit values while its
    settings dialog is open; replacing such a value here made manual Client-ID
    changes appear not to persist.
    """
    try:
        addon = xbmcaddon.Addon(id=ADDON_ID)
        value = addon.getSetting("spotify_webapi_client_id").strip()
    except Exception:
        value = ""
    if re.fullmatch(r"[0-9A-Fa-f]{32}", value or ""):
        return value.lower()
    return ""


def set_music_info(list_item, info_labels: Dict[str, Any]) -> None:
    """Set Kodi 21/22 music metadata through ``InfoTagMusic``.

    ``ListItem.setInfo('music', ...)`` is deprecated by current Kodi releases.
    Keep a compatibility fallback only for an unexpected older/stub runtime.
    """
    try:
        tag = list_item.getMusicInfoTag()
    except Exception:
        list_item.setInfo("music", info_labels)
        return

    setters = {
        "title": ("setTitle", str),
        "album": ("setAlbum", str),
        "comment": ("setComment", str),
        "year": ("setYear", int),
        "tracknumber": ("setTrack", int),
        "duration": ("setDuration", lambda value: max(0, int(round(float(value))))),
        "rating": ("setRating", float),
    }
    for key, (method_name, convert) in setters.items():
        value = info_labels.get(key)
        if value in (None, ""):
            continue
        if key == "year":
            try:
                if int(value) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
        try:
            getattr(tag, method_name)(convert(value))
        except (AttributeError, TypeError, ValueError):
            pass

    for key, method_name, legacy_method in (
        ("artist", "setArtists", "setArtist"),
        ("genre", "setGenres", "setGenre"),
    ):
        value = info_labels.get(key)
        if value in (None, ""):
            continue
        values = (
            [str(item) for item in value if str(item)]
            if isinstance(value, (list, tuple)) else [str(value)]
        )
        try:
            setter = getattr(tag, method_name, None)
            if setter is not None:
                setter(values)
            else:
                # Kodi 21.3/LibreELEC exposes the singular legacy setters even
                # though newer generated Python docs advertise list setters.
                getattr(tag, legacy_method)(" / ".join(values))
        except (AttributeError, TypeError, ValueError):
            pass


def get_spotify_device_hardware_label() -> str:
    """Return a short, privacy-safe OS/hardware label for Spotify Connect."""
    model = ""
    try:
        with open("/proc/device-tree/model", "rb") as model_file:
            model = model_file.read().decode("utf-8", "ignore").strip("\x00 ")
    except OSError:
        pass
    match = re.search(r"Raspberry Pi\s+(\d+)", model, re.IGNORECASE)
    if match:
        return f"Pi{match.group(1)}"
    if xbmc.getCondVisibility("System.Platform.Android"):
        return "Android"
    if xbmc.getCondVisibility("System.Platform.Windows"):
        return "Windows"
    machine = (platform.machine() or "unknown").lower()
    if xbmc.getCondVisibility("System.Platform.Linux"):
        if machine in ("aarch64", "arm64"):
            return "Linux-ARM64"
        if machine.startswith("arm"):
            return "Linux-ARMHF"
        if machine in ("x86_64", "amd64"):
            return "Linux-x64"
        return "Linux"
    return re.sub(r"[^A-Za-z0-9-]", "", platform.system()) or "Kodi"


def _persistent_spotify_device_suffix(regenerate: bool = False) -> str:
    """Return a stable four-character installation identifier."""
    os.makedirs(ADDON_DATA_PATH, exist_ok=True)
    if not regenerate:
        try:
            value = open(SPOTIFY_DEVICE_ID_FILE, "r", encoding="ascii").read().strip().upper()
            if re.fullmatch(r"[0-9A-F]{4}", value):
                return value
        except OSError:
            pass
    seed = f"{secrets.token_hex(16)}|{platform.node()}|{get_spotify_device_hardware_label()}|{ADDON_ID}"
    value = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:4].upper()
    try:
        with open(SPOTIFY_DEVICE_ID_FILE, "w", encoding="ascii") as id_file:
            id_file.write(value)
        os.chmod(SPOTIFY_DEVICE_ID_FILE, 0o600)
    except OSError:
        pass
    return value


def sanitize_spotify_device_name(value: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]", "", value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:48].strip()


def get_spotify_device_name() -> str:
    """Return the persistent automatic name or a validated custom name."""
    try:
        addon = xbmcaddon.Addon(id=ADDON_ID)
        if addon.getSetting("spotify_device_name_mode") == "custom":
            custom = sanitize_spotify_device_name(addon.getSetting("spotify_custom_device_name"))
            if custom:
                set_addon_setting_if_changed(
                    addon, "spotify_current_device_name", custom
                )
                return custom
    except Exception:
        addon = None
    value = f"Kodi-Resonance-{get_spotify_device_hardware_label()}-{_persistent_spotify_device_suffix()}"
    try:
        set_addon_setting_if_changed(
            addon or xbmcaddon.Addon(id=ADDON_ID),
            "spotify_current_device_name",
            value,
        )
    except Exception:
        pass
    return value


def regenerate_spotify_device_name() -> str:
    """Generate and persist a new automatic installation suffix."""
    _persistent_spotify_device_suffix(regenerate=True)
    return get_spotify_device_name()


def get_playback_settings() -> "tuple[str, str, str]":
    """Return playback options with unity volume; Kodi controls output volume."""
    quality, volume, gain = "320", "100", "track"
    try:
        addon = xbmcaddon.Addon(id=ADDON_ID)
        quality = (addon.getSetting("audio_quality") or "320").strip()
        gain = (addon.getSetting("normalization_type") or "track").strip()
    except Exception:
        return quality, volume, gain
    if quality not in ("96", "160", "320"):
        quality = "320"
    if gain not in ("track", "album", "auto"):
        gain = "track"
    return quality, volume, gain


def get_catalogue_settings() -> dict:
    addon = xbmcaddon.Addon(id=ADDON_ID)
    return {
        "bypass_response_cache": addon.getSetting("dev_bypass_response_cache") == "true",
    }


def get_spotify_account_email_hint() -> str:
    """Return the user-supplied Spotify account e-mail, if configured.

    Spotify Development Mode no longer guarantees the e-mail field in /me.
    This setting is Resonance-only and is persisted by Kodi in this addon's
    userdata.
    """
    try:
        return xbmcaddon.Addon(id=ADDON_ID).getSetting("spotify_account_email").strip()
    except Exception:
        return ""

def set_spotify_account_email_hint(email: str) -> None:
    email = (email or "").strip()
    if not email:
        return
    try:
        xbmcaddon.Addon(id=ADDON_ID).setSetting("spotify_account_email", email)
    except Exception:
        pass


def clear_spotify_account_email_hint() -> None:
    """Clear only Resonance's locally persisted account e-mail hint."""
    try:
        xbmcaddon.Addon(id=ADDON_ID).setSetting("spotify_account_email", "")
    except Exception:
        pass


def set_addon_setting_if_changed(addon, setting_id: str, value: Any) -> bool:
    """Persist a Kodi add-on setting only when its serialized value changed.

    Kodi rewrites the add-on settings file and emits ``onSettingsChanged`` for
    every ``setSetting`` call, including read-only status labels.  Avoiding
    identical writes prevents needless flash I/O and service reload work on
    appliances such as LibreELEC.
    """
    normalized = str("—" if value is None or value == "" else value)
    try:
        if addon.getSetting(setting_id) == normalized:
            return False
        addon.setSetting(setting_id, normalized)
        return True
    except Exception:
        return False


def set_spotify_connection_status(connected: bool) -> None:
    """Update the read-only connection value shown in Resonance settings."""
    try:
        addon = xbmcaddon.Addon(id=ADDON_ID)
        label_id = 11094 if connected else 11087
        set_addon_setting_if_changed(
            addon,
            "spotify_connection_status",
            addon.getLocalizedString(label_id),
        )
    except Exception:
        pass


def ensure_valid_addon_settings() -> None:
    """Migrate empty/legacy settings to values supported by Resonance."""
    try:
        addon = xbmcaddon.Addon(id=ADDON_ID)
    except Exception:
        return

    allowed_values = {
        "browse_page_size": ({"20", "40", "60", "80", "100"}, "40"),
        "audio_quality": ({"96", "160", "320"}, "320"),
        "normalization_type": ({"track", "album", "auto"}, "track"),
        "gap_between_playlist_tracks": ({"0", "1", "2", "3", "5", "10"}, "0"),
        "audio_preload_mode": ({"automatic", "manual"}, "automatic"),
        "audio_preload_seconds": ({"5", "10", "15"}, "10"),
        "spotify_device_name_mode": ({"automatic", "custom"}, "automatic"),
        "music_playlist_resume_rewind_seconds": ({"0", "5", "10", "15", "30"}, "5"),
        "music_playlist_resume_minimum_seconds": ({"5", "10", "15", "30", "60"}, "15"),
        "music_playlist_resume_max_sessions": ({"1", "3", "5", "10"}, "5"),
        "music_playlist_resume_retention_days": ({"30", "90", "180", "365"}, "90"),
        "podcast_resume_rewind_seconds": ({"0", "3", "5", "10", "15", "30"}, "5"),
        "podcast_resume_minimum_seconds": ({"0", "5", "10", "15", "20", "30", "60", "120"}, "10"),
        "podcast_resume_completion_threshold_seconds": ({"5", "10", "15", "30", "60", "120", "300"}, "30"),
        "audiobook_resume_rewind_seconds": ({"0", "5", "10", "15", "30", "60"}, "10"),
        "audiobook_resume_minimum_seconds": ({"5", "10", "15", "30", "60"}, "10"),
    }
    for setting_id, (allowed, default) in allowed_values.items():
        try:
            value = addon.getSetting(setting_id).strip()
            if value not in allowed:
                set_addon_setting_if_changed(addon, setting_id, default)
        except Exception:
            pass

    try:
        set_addon_setting_if_changed(
            addon,
            "spotify_connection_status",
            addon.getSetting("spotify_connection_status") or addon.getLocalizedString(11087),
        )
        get_spotify_device_name()
    except Exception:
        pass

def update_about_status(playback_binary: str = "") -> None:
    """Publish current runtime/authentication information as read-only labels."""
    try:
        addon = xbmcaddon.Addon(id=ADDON_ID)
    except Exception:
        return

    def localized(string_id: int, fallback: str) -> str:
        try:
            return addon.getLocalizedString(string_id) or fallback
        except Exception:
            return fallback

    def set_value(setting_id: str, value: str) -> None:
        set_addon_setting_if_changed(addon, setting_id, value)

    client_id = get_spotify_webapi_client_id()
    masked_client_id = (
        f"{client_id[:12]}…{client_id[-4:]}"
        if len(client_id) > 18 else client_id
    )
    cache_dir = os.path.join(ADDON_DATA_PATH, "spotty-cache")
    credentials_file = os.path.join(cache_dir, "credentials.json")
    token_file = os.path.join(cache_dir, "spotty-token")

    credentials_present = False
    try:
        credentials_present = os.path.getsize(credentials_file) >= 32
    except OSError:
        pass

    token_status = localized(11180, "Missing")
    try:
        if os.path.getsize(token_file) > 0:
            token_status = localized(11179, "Present")
            with open(token_file, "r", encoding="utf-8") as token_handle:
                data = json.loads(token_handle.read() or "{}")
            if isinstance(data, dict):
                created_at = int(data.get("createdAt") or 0)
                expires_in = int(data.get("expiresIn") or data.get("expires_in") or 0)
                if created_at and expires_in:
                    token_status = localized(
                        11181 if int(time.time()) < created_at + expires_in - 60 else 11182,
                        "Valid" if int(time.time()) < created_at + expires_in - 60 else "Expired",
                    )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    preload_enabled = addon.getSetting("enable_audio_preload").lower() != "false"
    preload_mode = addon.getSetting("audio_preload_mode") or "automatic"
    if not preload_enabled:
        preload_status = localized(11185, "Disabled")
    elif preload_mode == "manual":
        seconds = addon.getSetting("audio_preload_seconds") or "10"
        preload_status = f"{localized(11187, 'Manual')} ({seconds} s)"
    else:
        lead = addon.getSetting("prefetch_current_lead") or "5 s"
        preload_status = f"{localized(11186, 'Automatic')} ({lead})"

    set_value("resonance_version", addon.getAddonInfo("version"))
    set_value("resonance_addon_id", ADDON_ID)
    split_modern_playback = (
        platform.system() == "Windows"
        or xbmc.getCondVisibility("System.Platform.Android")
    )
    set_value(
        "resonance_playback_engine",
        (
            "Spotty 2.x / librespot 0.8.x playback + legacy Spotty auth/token"
            if split_modern_playback
            else "Spotty 2.1.2 / librespot 0.8.0"
        ),
    )
    set_value("resonance_kodi_version", xbmc.getInfoLabel("System.BuildVersion") or "—")
    set_value("resonance_python_version", platform.python_version())
    set_value("resonance_audio_proxy_port", str(PROXY_PORT))
    set_value("resonance_operating_system", f"{platform.system()} {platform.release()}")
    set_value("resonance_architecture", platform.machine())
    set_value("resonance_playback_binary", os.path.basename(playback_binary) if playback_binary else "—")
    set_value("resonance_preload_mode_status", preload_status)
    try:
        from .spotty import _get_spotty_auth_command
        auth_command = list(_get_spotty_auth_command() or [])
        auth_binary_name = os.path.basename(auth_command[-1]) if auth_command else "—"
    except Exception:
        auth_binary_name = (
            "spotty.exe"
            if platform.system() == "Windows"
            else "spotty-auth-muslhf"
        )
    set_value(
        "resonance_auth_method",
        "glk1001 plugin.audio.spotify-v1.3.14 legacy Device Connect + --save-token",
    )
    set_value("resonance_device_connect_binary", auth_binary_name)
    set_value("resonance_auth_binary", auth_binary_name)
    set_value("resonance_device_name", get_spotify_device_name())
    set_value("resonance_device_port", "10002")
    set_value("resonance_token_method", localized(11184, "Spotty --save-token"))
    set_value("resonance_active_client_id", masked_client_id)
    set_value(
        "resonance_credentials_status",
        localized(11179 if credentials_present else 11180, "Present" if credentials_present else "Missing"),
    )
    set_value("resonance_token_status", token_status)

def delete_resonance_credentials() -> None:
    """Remove only Resonance authentication state and stop its Connect receiver."""
    for name in ("spotify-credentials.json", "spotify-credentials.json.tmp", "web-auth.json"):
        try:
            os.remove(os.path.join(ADDON_DATA_PATH, name))
        except FileNotFoundError:
            pass
    cache_dirs = (
        RUNTIME_PATH,
        LEGACY_RUNTIME_PATH,
        os.path.join(ADDON_DATA_PATH, "spotty-cache"),
    )

    for cache_dir in cache_dirs:
        try:
            with open(os.path.join(cache_dir, "device-connect.pid"), "r", encoding="utf-8") as file:
                pid = int((file.read() or "0").strip())

            if os.name == "nt":
                terminate_process(pid, ("spotty.exe",))
            else:
                cmdline_path = f"/proc/{pid}/cmdline"
                if not (pid > 0 and os.path.exists(cmdline_path)):
                    raise OSError("Stored Device Connect PID is not active")
                with open(cmdline_path, "rb") as file:
                    cmdline = file.read().replace(b"\0", b" ").decode(
                        "utf-8", errors="replace"
                    )
                if (
                    "spotty" in cmdline
                    and cache_dir in cmdline
                    and "Kodi-Resonance" in cmdline
                ):
                    terminate_process(pid)
        except (OSError, ValueError, TypeError, SystemError):
            pass

        for filename in (
            "credentials.json",
            "credentials.json.bak",
            "spotty-token",
            "spotty-token.bak",
            "spotty-token.tmp",
            "device-connect.pid",
        ):
            try:
                os.remove(os.path.join(cache_dir, filename))
            except FileNotFoundError:
                pass

    delete_persistent_spotify_profile()
    clear_spotify_account_email_hint()
    clear_spotify_account_info()
    cache_auth_token("")
    cache_auth_token_expires_at("")
    cache_auth_client_id("")
    set_spotify_connection_status(False)


def clear_resonance_cache() -> None:
    """Delete the catalogue page cache and shared rate-limit cooldowns.

    Spotify credentials, settings and the device name are preserved.
    """
    import sqlite3
    from resources.lib.catalogue import database_path

    # Clear in place so existing readers keep using the same database.
    with sqlite3.connect(database_path(ADDON_DATA_PATH), timeout=10) as db:
        db.execute("CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, value TEXT, expires REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS gates (provider TEXT PRIMARY KEY, until REAL)")
        db.execute("DELETE FROM pages")
        db.execute("DELETE FROM gates")
        # Do not repopulate cleared pages from obsolete migration sources.
        db.execute("INSERT INTO pages VALUES (?, ?, ?)", ("migration-complete", "true", 0))
    for name in (
        "spotify-api-gate.json",
    ):
        try:
            os.remove(os.path.join(ADDON_DATA_PATH, name))
        except FileNotFoundError:
            pass

def clear_provider_gate(provider: str) -> None:
    """Remove the rate-limit gate for one provider (e.g. spotify).

    Called after a browser authorization-code login succeeds: the previous
    cooldown belonged to a rate-limited token and no longer applies.
    """
    import sqlite3
    from resources.lib.catalogue import database_path

    with sqlite3.connect(database_path(ADDON_DATA_PATH), timeout=10) as db:
        db.execute("DELETE FROM gates WHERE provider = ?", (provider,))

def cache_auth_client_id(client_id: str) -> None:
    cache_value_in_kodi(KODI_PROPERTY_SPOTIFY_AUTH_CLIENT_ID, client_id or "")

def get_cached_auth_client_id() -> str:
    return get_cached_value_from_kodi(KODI_PROPERTY_SPOTIFY_AUTH_CLIENT_ID) or ""

def cache_auth_token(
    auth_token: str
) -> None:

    cache_value_in_kodi(
        KODI_PROPERTY_SPOTIFY_AUTH_TOKEN,
        auth_token
    )


def get_cached_auth_token() -> str:
    active_client_id = get_spotify_webapi_client_id()

    try:
        web = web_auth()
        if web.has_session():
            access = web.access_token()
            if access:
                stored = web.load() or {}
                try:
                    web_expires_at = int(stored.get("expires_at") or (time.time() + 3600))
                except (TypeError, ValueError):
                    web_expires_at = int(time.time()) + 3600
                cache_auth_token(access)
                cache_auth_token_expires_at(str(web_expires_at))
                cache_auth_client_id(active_client_id)
                return access
    except Exception as exc:
        log_msg(f"Web API token restore failed: {exc}", LOGWARNING)

    return ""


def cache_auth_token_expires_at(
    auth_token: str
) -> None:

    cache_value_in_kodi(
        KODI_PROPERTY_AUTH_TOKEN_EXPIRES_AT,
        auth_token
    )


def get_cached_auth_token_expires_at() -> str:

    return get_cached_value_from_kodi(
        KODI_PROPERTY_AUTH_TOKEN_EXPIRES_AT
    )



def load_persistent_spotify_profile() -> Dict[str, Any]:
    """Load Resonance account metadata only from Resonance userdata."""
    try:
        with open(SPOTIFY_ACCOUNT_PROFILE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def save_persistent_spotify_profile(profile: Dict[str, Any]) -> None:
    """Persist account metadata in plugin.audio.resonance userdata only."""
    try:
        os.makedirs(ADDON_DATA_PATH, exist_ok=True)
        tmp_file = SPOTIFY_ACCOUNT_PROFILE_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as file:
            json.dump(profile, file, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_file, SPOTIFY_ACCOUNT_PROFILE_FILE)
    except OSError as exc:
        log_msg(f"Could not persist Resonance account profile: {exc}", LOGERROR)


def delete_persistent_spotify_profile() -> None:
    try:
        os.remove(SPOTIFY_ACCOUNT_PROFILE_FILE)
    except OSError:
        pass


def set_spotify_account_info(
    email: str,
    display_name: str = "",
    user_id: str = "",
) -> None:
    """Expose the authenticated Spotify account to Kodi GUI skins/windows."""
    win = xbmcgui.Window(ADDON_WINDOW_ID)
    win.setProperty(KODI_PROPERTY_SPOTIFY_ACCOUNT_EMAIL, email or "")
    win.setProperty(KODI_PROPERTY_SPOTIFY_ACCOUNT_NAME, display_name or "")
    win.setProperty(KODI_PROPERTY_SPOTIFY_ACCOUNT_ID, user_id or "")


def clear_spotify_account_info() -> None:
    set_spotify_account_info("", "", "")


def cache_value_in_kodi(
    kodi_property_id: str,
    value: Any
):

    win = xbmcgui.Window(
        ADDON_WINDOW_ID
    )

    win.setProperty(
        kodi_property_id,
        value
    )


def get_cached_value_from_kodi(
    kodi_property_id: str,
    wait_ms: int = 500
) -> Any:

    win = xbmcgui.Window(
        ADDON_WINDOW_ID
    )

    count = 10

    while count > 0:

        value = win.getProperty(
            kodi_property_id
        )

        if value:
            return value

        xbmc.sleep(
            wait_ms
        )

        count -= 1

    return None


def get_user_playlists(
    spotipy,
    limit: int = 50,
    offset: int = 0
) -> Tuple[List[Dict[str, Any]], List[str]]:

    userid = spotipy.me()["id"]

    playlists = spotipy.current_user_playlists(
        limit=limit,
        offset=offset
    )

    own_playlists = []
    own_playlist_names = []

    for playlist in playlists["items"]:

        if playlist["owner"]["id"] == userid:

            own_playlists.append(
                playlist
            )

            own_playlist_names.append(
                playlist["name"]
            )

    return (
        own_playlists,
        own_playlist_names
    )


def get_user_playlist_id(
    spotipy,
    playlist_name: str
) -> Union[str, None]:

    offset = 0

    while True:

        own_playlists, own_playlist_names = get_user_playlists(
            spotipy,
            limit=50,
            offset=offset
        )

        if len(own_playlists) == 0:
            break

        for playlist in own_playlists:

            if playlist_name == playlist["name"]:
                return playlist["id"]

        offset += 50

    return None
    
