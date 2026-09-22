import ipaddress
import os
import platform
import socket
import subprocess
import stat
import time
from typing import Dict, List
from pathlib import Path

import xbmc
import xbmcgui
from xbmc import LOGDEBUG, LOGINFO, LOGERROR, LOGWARNING

from spotty_helper import SpottyHelper, get_android_auth_spotty_command, _is_android_runtime
from utils import (
    log_msg,
    ADDON_DATA_PATH,
    get_spotify_device_name,
    get_spotify_webapi_client_id,
)


# Spotify Connect device name
# Must match the successful manual test:
# spotty-aarch64 -v --name KODI-Resonance --zeroconf-port 10002
SPOTTY_PLAYER_NAME = "Kodi-Resonance"


# librespot/spotty 2.1.0 defaults
# Validated with manual Device Connect test on LibreELEC:
#
#   spotty-aarch64
#   -v
#   --name KODI-Resonance
#   --zeroconf-port 10002
#   --disable-audio-cache
#
SPOTTY_DEFAULT_ARGS = [
    "--disable-audio-cache",
]


SPOTTY_TOKEN_FILE = "spotty-token"
SPOTTY_TOKEN_BACKUP_FILE = "spotty-token.bak"

# Spotify Web API client used by the original addon/Spotty token flow.
SPOTTY_WEB_API_SCOPE = [
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-modify-playback-state",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-follow-modify",
    "user-follow-read",
    "user-library-read",
    "user-library-modify",
    "user-read-private",
    "user-read-email",
    "user-top-read",
    "user-read-recently-played",
    "user-read-playback-position",
]
SPOTTY_CACHE_DIR_NAME = "spotty-cache"


SPOTTY_CACHE_DIR = os.path.join(
    ADDON_DATA_PATH,
    SPOTTY_CACHE_DIR_NAME,
)


SPOTTY_CREDENTIALS_FILENAME = "credentials.json"
SPOTTY_CREDENTIALS_BACKUP_FILENAME = "credentials.json.bak"

# Keep the proven legacy Spotty/librespot path exclusively for Device Connect
# and Web API token creation. Playback uses separately generated platform
# payloads and never falls back to these legacy auth/token binaries.
SPOTTY_POSIX_AUTH_BINARY = os.path.join(
    os.path.dirname(__file__), "deps", "spotty", "auth-compat", "spotty-auth-muslhf"
)
SPOTTY_WINDOWS_AUTH_BINARY = os.path.join(
    os.path.dirname(__file__), "deps", "spotty", "windows", "spotty.exe"
)
def _get_native_legacy_auth_binary() -> str:
    """Return the legacy auth/token binary matching the current native platform.

    ARM Linux keeps the proven musl auth-compat receiver.  Native x86 Linux
    and macOS packages intentionally use their bundled legacy Spotty binary
    for Device Connect/token as well as playback until separate Spotty 2
    playback payloads are available for those targets.
    """
    base = os.path.join(os.path.dirname(__file__), "deps", "spotty")
    machine = (platform.machine() or "").lower()

    try:
        is_macos = bool(xbmc.getCondVisibility("System.Platform.OSX"))
        is_linux = bool(xbmc.getCondVisibility("System.Platform.Linux"))
    except Exception:
        is_macos = False
        is_linux = False

    if is_macos:
        return os.path.join(base, "macos", "spotty")

    if is_linux and machine in ("x86_64", "amd64"):
        return os.path.join(base, "x86-linux", "spotty-x86_64")

    if is_linux and machine in ("x86", "i386", "i486", "i586", "i686"):
        return os.path.join(base, "x86-linux", "spotty")

    return SPOTTY_POSIX_AUTH_BINARY


SPOTTY_DEFAULT_AUTH_BINARY = (
    SPOTTY_WINDOWS_AUTH_BINARY
    if os.name == "nt"
    else _get_native_legacy_auth_binary()
)

SPOTTY_LINUX_BINARIES = (
    os.path.join(os.path.dirname(__file__), "deps", "spotty", "arm-linux", "spotty"),
    os.path.join(os.path.dirname(__file__), "deps", "spotty", "arm-linux", "spotty-aarch64"),
    os.path.join(os.path.dirname(__file__), "deps", "spotty", "arm-linux", "spotty-armhf"),
    os.path.join(os.path.dirname(__file__), "deps", "spotty", "arm-linux", "spotty-muslhf"),
    SPOTTY_POSIX_AUTH_BINARY,
)


def _windows_process_kwargs():
    """Return subprocess options that suppress Windows console windows."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def _get_spotty_auth_command():
    """Select legacy auth/token command, including Android's Bionic linker."""
    if _is_android_runtime():
        return get_android_auth_spotty_command() or []
    return [SPOTTY_DEFAULT_AUTH_BINARY]


_LEGACY_ZEROCONF_INTERFACE_SUPPORT = None


def _legacy_auth_supports_zeroconf_interface(auth_command: List[str]) -> bool:
    """Return whether the bundled legacy auth Spotty supports interface pinning."""
    global _LEGACY_ZEROCONF_INTERFACE_SUPPORT
    if _LEGACY_ZEROCONF_INTERFACE_SUPPORT is not None:
        return _LEGACY_ZEROCONF_INTERFACE_SUPPORT
    if os.name == "nt" or _is_android_runtime() or not auth_command:
        _LEGACY_ZEROCONF_INTERFACE_SUPPORT = False
        return False
    try:
        result = subprocess.run(
            auth_command + ["--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=3,
            **_windows_process_kwargs(),
        )
        output = result.stdout or ""
        _LEGACY_ZEROCONF_INTERFACE_SUPPORT = "--zeroconf-interface" in output
    except (OSError, subprocess.TimeoutExpired):
        _LEGACY_ZEROCONF_INTERFACE_SUPPORT = False
    return _LEGACY_ZEROCONF_INTERFACE_SUPPORT


def _valid_zeroconf_ipv4(value: str) -> str:
    """Normalize a usable unicast IPv4 address or return an empty string."""
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return ""
    if (
        address.version != 4
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        return ""
    return str(address)


_ZEROCONF_EXCLUDED_INTERFACE_PREFIXES = (
    "docker",
    "br-",
    "virbr",
    "veth",
    "tun",
    "tap",
    "wg",
    "tailscale",
    "zt",
)

_ZEROCONF_PREFERRED_INTERFACE_PREFIXES = (
    "eth",
    "en",
    "wlan",
    "wl",
)


def _is_zeroconf_excluded_interface(interface: str) -> bool:
    """Reject virtual bridge, container and VPN/tunnel interfaces."""
    interface = (interface or "").strip().lower()
    return not interface or interface == "lo" or interface.startswith(
        _ZEROCONF_EXCLUDED_INTERFACE_PREFIXES
    )


def _is_zeroconf_preferred_interface(interface: str) -> bool:
    """Return whether an interface looks like physical Ethernet/Wi-Fi."""
    interface = (interface or "").strip().lower()
    return bool(interface) and interface.startswith(_ZEROCONF_PREFERRED_INTERFACE_PREFIXES)


def _is_private_zeroconf_ipv4(value: str) -> bool:
    """Return whether value is a usable RFC1918 IPv4 suitable for LAN mDNS."""
    normalized = _valid_zeroconf_ipv4(value)
    if not normalized:
        return False
    return ipaddress.ip_address(normalized).is_private


def _get_default_route_ipv4():
    """Return (interface, IPv4) selected by the host default route."""
    try:
        result = subprocess.run(
            ["ip", "-4", "route", "get", "1.1.1.1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        words = (result.stdout or "").split()
        route_interface = words[words.index("dev") + 1] if "dev" in words else ""
        source_ip = words[words.index("src") + 1] if "src" in words else ""
        source_ip = _valid_zeroconf_ipv4(source_ip)
        if source_ip:
            return route_interface, source_ip
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass

    # Portable fallback: no traffic is sent; connect() only asks the kernel
    # which source address it would use for the route.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 53))
            source_ip = _valid_zeroconf_ipv4(sock.getsockname()[0])
            if source_ip:
                return "", source_ip
    except OSError:
        pass
    return "", ""


def _get_physical_lan_ipv4():
    """Return a preferred physical LAN/WLAN interface and private IPv4."""
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", ""

    candidates = []
    for line in (result.stdout or "").splitlines():
        words = line.split()
        if len(words) < 4 or "inet" not in words:
            continue
        try:
            interface = words[1].split("@", 1)[0]
            cidr = words[words.index("inet") + 1]
            source_ip = cidr.split("/", 1)[0]
        except (ValueError, IndexError):
            continue
        if _is_zeroconf_excluded_interface(interface):
            continue
        source_ip = _valid_zeroconf_ipv4(source_ip)
        if not source_ip or not _is_private_zeroconf_ipv4(source_ip):
            continue
        priority = 0 if _is_zeroconf_preferred_interface(interface) else 1
        candidates.append((priority, interface, source_ip))

    if not candidates:
        return "", ""
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    _, interface, source_ip = candidates[0]
    return interface, source_ip


def _get_zeroconf_lan_ipv4(route_interface: str = "", route_ip: str = ""):
    """Choose LAN/WLAN IPv4 while avoiding VPN, Docker and bridge routes."""
    if not route_ip:
        route_interface, route_ip = _get_default_route_ipv4()
    if (
        route_ip
        and not _is_zeroconf_excluded_interface(route_interface)
        and _is_zeroconf_preferred_interface(route_interface)
        and _is_private_zeroconf_ipv4(route_ip)
    ):
        return route_interface, route_ip, "default_route"

    lan_interface, lan_ip = _get_physical_lan_ipv4()
    if lan_ip:
        return lan_interface, lan_ip, "physical_lan_scan"

    return "", "", "no_safe_lan_interface"

def _ensure_executable(binary_path: str) -> None:
    """Ensure bundled Spotty binaries are executable on POSIX Kodi installs.

    Windows does not use POSIX executable mode bits, so it must never be
    subjected to chmod-based permission handling.
    """
    if os.name == "nt" or not binary_path:
        return
    try:
        if os.path.isfile(binary_path):
            os.chmod(binary_path, 0o755)
    except OSError as exc:
        log_msg(
            f"Could not set executable permissions on '{binary_path}': {exc}",
            LOGWARNING,
        )


def ensure_linux_spotty_permissions() -> None:
    """Enforce exact 0755 mode on every bundled Linux Spotty executable."""
    if os.name == "nt":
        return
    try:
        window = xbmcgui.Window(10000)
        if window.getProperty("plugin.audio.resonance.binary-permissions-verified") == "true":
            return
    except Exception:
        window = None
    checked = 0
    all_valid = True
    for binary_path in SPOTTY_LINUX_BINARIES:
        if not os.path.isfile(binary_path):
            continue
        try:
            old_mode = stat.S_IMODE(os.stat(binary_path).st_mode)
            if old_mode != 0o755:
                os.chmod(binary_path, 0o755)
                log_msg(
                    "BINARY_DIAG permission_repaired "
                    f"binary={os.path.basename(binary_path)} "
                    f"old_mode={old_mode:04o} new_mode=0755",
                    LOGINFO,
                )
            final_mode = stat.S_IMODE(os.stat(binary_path).st_mode)
            checked += 1
            executable = os.access(binary_path, os.X_OK)
            all_valid = all_valid and final_mode == 0o755 and executable
            log_msg(
                "BINARY_DIAG permission_check "
                f"binary={os.path.basename(binary_path)} "
                f"mode={final_mode:04o} executable={str(executable).lower()}",
                LOGDEBUG,
            )
        except OSError as exc:
            all_valid = False
            log_msg(
                "BINARY_DIAG permission_failed "
                f"binary={os.path.basename(binary_path)} error={exc}",
                LOGERROR,
            )
    if all_valid and checked:
        log_msg(
            f"BINARY_DIAG permission_summary checked={checked} mode=0755 executable=true",
            LOGINFO,
        )
        try:
            if window is not None:
                window.setProperty("plugin.audio.resonance.binary-permissions-verified", "true")
        except Exception:
            pass


class Spotty:

    def __init__(self, cache_directory=None):

        self.__spotty_binary = ""
        self.__spotty_command = []
        self.__auth_command = _get_spotty_auth_command()
        self.__auth_binary = self.__auth_command[-1] if self.__auth_command else ""
        self.__spotty_cache = cache_directory or SPOTTY_CACHE_DIR
        self.__spotify_username = ""
        self.__spotify_password = ""
        self.__spotty_rust_env = None

        self.__playback_supported = True
        self.__last_token_error_kind = ""


    def set_spotty_path(
        self,
        spotty_binary: str,
        spotty_command=None,
    ) -> None:

        ensure_linux_spotty_permissions()
        self.__spotty_binary = spotty_binary
        self.__spotty_command = list(spotty_command or ([spotty_binary] if spotty_binary else []))

        if self.__spotty_binary:

            self.__playback_supported = True

            xbmc.executebuiltin(
                "SetProperty(plugin.audio.resonance.supportsplayback, true, Home)"
            )

            log_msg(
                "AUTH_DIAG split_config "
                f"device_connect_binary={os.path.basename(self.__auth_binary or '')} "
                f"token_binary={os.path.basename(self.__auth_binary or '')} "
                f"playback_binary={os.path.basename(self.__spotty_binary)}",
                LOGINFO,
            )
            log_msg(
                "PLAYBACK_DIAG "
                f"binary={os.path.basename(self.__spotty_binary)} role=audio",
                LOGINFO,
            )

        else:

            self.__playback_supported = False

            log_msg(
                "Error while verifying spotty. "
                "Local playback is disabled.",
                loglevel=LOGERROR,
            )


    def set_spotty_env(
        self,
        env: Dict[str, str]
    ):

        self.__spotty_rust_env = env


    def get_spotty_token_file(
        self
    ) -> str:

        return os.path.join(
            self.__spotty_cache,
            SPOTTY_TOKEN_FILE,
        )


    def get_spotty_token_backup_file(
        self
    ) -> str:

        return os.path.join(
            self.__spotty_cache,
            SPOTTY_TOKEN_BACKUP_FILE,
        )


    def get_spotty_credentials_file(
        self
    ) -> str:

        return os.path.join(
            self.__spotty_cache,
            SPOTTY_CREDENTIALS_FILENAME,
        )


    def get_spotty_credentials_backup_file(
        self
    ) -> str:

        return os.path.join(
            self.__spotty_cache,
            SPOTTY_CREDENTIALS_BACKUP_FILENAME,
        )



    def get_access_token(self, quiet: bool = False) -> str:
        """Create/refresh and persist a Spotify Web API token.

        Device Connect stores the reusable account credentials in
        ``credentials.json``.  Spotty then uses those credentials to request
        a Web API token.  v1.0.41 deliberately persists that token to
        ``spotty-token`` in the Resonance userdata cache so service restarts
        and plugin invocations share the same authentication state.
        """
        import json
        os.makedirs(self.__spotty_cache, exist_ok=True)

        credentials_file = self.get_spotty_credentials_file()
        token_file = self.get_spotty_token_file()
        client_id = get_spotify_webapi_client_id()

        if not os.path.exists(credentials_file):
            log_msg("Cannot create Spotty token: credentials.json is missing.", LOGERROR)
            return ""

        # Reuse a still-valid persistent token.  Device Connect removes the
        # old token before an explicit account switch, so this cannot leak
        # authentication state between Resonance accounts.  It also prevents
        # the background service and the GUI authentication invocation from
        # requesting the same token twice within a few seconds.
        if os.path.exists(token_file):
            try:
                cached_data = json.loads(
                    Path(token_file).read_text(encoding="utf-8").strip() or "{}"
                )
                if isinstance(cached_data, dict):
                    cached_token = (
                        cached_data.get("accessToken")
                        or cached_data.get("access_token")
                        or ""
                    )
                    created_at = int(cached_data.get("createdAt") or 0)
                    expires_in = int(
                        cached_data.get("expiresIn")
                        or cached_data.get("expires_in")
                        or 0
                    )
                    if (
                        cached_token
                        and created_at > 0
                        and expires_in > 0
                        and int(time.time()) < (created_at + expires_in - 60)
                        and (not client_id or cached_data.get("clientId") == client_id)
                    ):
                        log_msg(
                            "Reusing valid persistent Spotify Web API token from spotty-token."
                        )
                        log_msg(
                            "AUTH_DIAG state=token_reused source=spotty-token "
                            "credentials_preserved=true",
                            LOGINFO,
                        )
                        return cached_token
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # Invalid/legacy token files are replaced below.
                pass

        scope = ",".join(SPOTTY_WEB_API_SCOPE)

        # Credentials must be consumed by the same librespot generation that
        # created them.  Native Spotty 2 Device Connect writes a credential
        # blob the legacy helper cannot exchange for a Web API token.
        temp_token_file = token_file + ".tmp"
        try:
            if os.path.exists(temp_token_file):
                os.remove(temp_token_file)
        except OSError:
            pass

        use_native_token_helper = bool(
            os.name != "nt"
            and self.__spotty_binary
            and self.__spotty_binary != self.__auth_binary
        )
        token_binary = self.__spotty_binary if use_native_token_helper else self.__auth_binary
        token_command = self.__spotty_command if use_native_token_helper else self.__auth_command

        if not token_binary:
            self.__last_token_error_kind = "auth_binary_unavailable"
            log_msg("No compatible Spotty token binary is available.", LOGERROR)
            return ""

        args = token_command + [
            "--cache", self.__spotty_cache,
        ]
        if use_native_token_helper:
            args.extend(["--system-cache", self.__spotty_cache])
        args.extend([
            "--client-id", client_id,
            "--scope", scope,
            "--save-token", temp_token_file,
        ])

        log_msg(f"Creating Spotify Web API token file: {token_file}")
        log_msg(
            "AUTH_DIAG operation=save-token "
            f"binary={os.path.basename(token_binary)} result=starting",
            LOGINFO,
        )
        self.__last_token_error_kind = ""
        started_at = time.monotonic()

        try:
            _ensure_executable(token_binary)
            result = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                env=self.__spotty_rust_env,
                **_windows_process_kwargs(),
            )

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                stdout = (result.stdout or "").strip()
                log_msg(
                    f"Spotty --save-token failed rc={result.returncode}; "
                    f"stdout='{stdout[:300]}'; stderr='{stderr[:300]}'",
                    LOGWARNING if quiet else LOGERROR,
                )
                log_msg(
                    "AUTH_DIAG operation=save-token "
                    f"binary={os.path.basename(token_binary)} "
                    f"elapsed={time.monotonic() - started_at:.3f}s "
                    f"result=failed rc={result.returncode}",
                    LOGWARNING if quiet else LOGERROR,
                )
                return ""

            # Spotty normally writes JSON to token_file.  Be tolerant of raw
            # token files and of versions that additionally print JSON.
            raw = ""
            if os.path.exists(temp_token_file):
                try:
                    raw = Path(temp_token_file).read_text(encoding="utf-8").strip()
                except Exception as exc:
                    log_msg(f"Could not read temporary Spotty token file: {exc}", LOGERROR)

            if not raw:
                raw = (result.stdout or "").strip()

            token = ""
            expires_in = 3600

            if raw:
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        token = (
                            data.get("accessToken")
                            or data.get("access_token")
                            or ""
                        )
                        try:
                            expires_in = int(
                                data.get("expiresIn")
                                or data.get("expires_in")
                                or 3600
                            )
                        except (TypeError, ValueError):
                            expires_in = 3600
                    elif isinstance(data, str):
                        token = data
                except (ValueError, TypeError):
                    token = raw

            if not token:
                log_msg("Spotty token file did not contain an access token.", LOGWARNING if quiet else LOGERROR)
                return ""

            # Persist normalized metadata so v1.1.64 can safely reuse the token
            # only with the Client ID that created it. Spotty itself continues
            # to own credentials.json; this file is consumed by Resonance.
            try:
                normalized = json.dumps(
                    {
                        "accessToken": token,
                        "expiresIn": expires_in,
                        "createdAt": int(time.time()),
                        "clientId": client_id,
                    },
                    separators=(",", ":"),
                )
                Path(token_file).write_text(normalized, encoding="utf-8")
                if os.path.exists(temp_token_file):
                    os.remove(temp_token_file)
            except OSError as exc:
                log_msg(f"Could not commit Spotify token file: {exc}", LOGERROR)
                return ""

            log_msg(
                "Legacy-auth Spotify Web API token persisted successfully.",
                LOGDEBUG,
            )
            log_msg(
                "AUTH_DIAG operation=save-token "
                f"binary={os.path.basename(token_binary)} "
                f"elapsed={time.monotonic() - started_at:.3f}s "
                f"result=success expires_in={expires_in}s",
                LOGINFO,
            )
            log_msg(
                "AUTH_DIAG state=token_renewed source=credentials.json "
                "credentials_preserved=true",
                LOGINFO,
            )
            return token

        except subprocess.TimeoutExpired:
            self.__last_token_error_kind = "timeout"
            log_msg("Spotty --save-token timed out.", LOGWARNING if quiet else LOGERROR)
        except (PermissionError, FileNotFoundError, OSError) as exc:
            self.__last_token_error_kind = "local_binary"
            log_msg(f"Spotty get_access_token failed locally: {exc}", LOGWARNING if quiet else LOGERROR)
        except Exception as exc:
            self.__last_token_error_kind = "other"
            log_msg(f"Spotty get_access_token failed: {exc}", LOGWARNING if quiet else LOGERROR)

        return ""

    def get_last_token_error_kind(self) -> str:
        return self.__last_token_error_kind


    def get_device_connect_zeroconf_args(self) -> List[str]:
        """Pin legacy POSIX Device Connect mDNS to the LAN/default-route IPv4."""
        if os.name == "nt" or _is_android_runtime():
            return []

        supported = _legacy_auth_supports_zeroconf_interface(self.__auth_command)
        log_msg(
            "DEVICE_CONNECT_DIAG "
            f"binary={os.path.basename(self.__auth_binary or '')} "
            f"zeroconf_interface_supported={str(supported).lower()}",
            LOGINFO,
        )
        if not supported:
            return []

        default_interface, default_ip = _get_default_route_ipv4()
        log_msg(
            "DEVICE_CONNECT_DIAG "
            f"default_interface={default_interface or 'unknown'} "
            f"default_ipv4={default_ip or 'none'}",
            LOGINFO,
        )

        route_interface, source_ip, selection = _get_zeroconf_lan_ipv4(
            default_interface, default_ip
        )
        if not source_ip:
            log_msg(
                "DEVICE_CONNECT_DIAG zeroconf_interface=none "
                f"selection={selection}",
                LOGWARNING,
            )
            return []

        log_msg(
            "DEVICE_CONNECT_DIAG "
            f"zeroconf_interface={source_ip} "
            f"route_interface={route_interface or 'unknown'} selection={selection}",
            LOGINFO,
        )
        return ["--zeroconf-interface", source_ip]


    def run_auth_spotty(self, extra_args: List[str] = None) -> subprocess.Popen:
        """Run the legacy Spotty binary against Resonance's own cache directory.

        This is intentionally restricted to authentication/token work. Audio
        playback always stays on the platform playback binary selected by
        SpottyHelper (separate playback binaries on Windows x64 and Android).
        """
        if not self.__auth_binary:
            raise RuntimeError("No compatible legacy Spotty auth binary is available")
        device_name = get_spotify_device_name()
        args = self.__auth_command + ["--cache", self.__spotty_cache, "--verbose", "--name", device_name]
        if extra_args:
            args.extend(extra_args)
        _ensure_executable(self.__auth_binary)
        log_msg(
            "AUTH_DIAG operation=device-connect "
            f"binary={os.path.basename(self.__auth_binary)} "
            f"device_name={device_name} "
            f"token_persistence={'enabled' if extra_args and '--save-token' in extra_args else 'disabled'} "
            "result=starting",
            LOGINFO,
        )
        log_msg("Legacy auth Spotty args: " + " ".join(args), LOGDEBUG)
        return subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=self.__spotty_rust_env,
            **_windows_process_kwargs(),
        )

    def run_device_connect_spotty(self, extra_args: List[str] = None) -> subprocess.Popen:
        """Start the best available receiver for persistent Device Connect auth.

        Current native playback payloads use librespot 0.8 and provide a
        dedicated ``--authenticate`` mode.  Use that mode for pairing instead
        of forcing ARM/Linux through the legacy 0.4.2 token helper.  Keep the
        legacy helper exclusively for ``--save-token``, for which Resonance
        still requires its compatible Web API implementation.
        """
        if os.name != "nt" and self.__spotty_binary and self.__spotty_binary != self.__auth_binary:
            device_name = get_spotify_device_name()
            args = self.__spotty_command + [
                "--cache", self.__spotty_cache,
                "--system-cache", self.__spotty_cache,
                "--verbose",
                "--name", device_name,
                "--authenticate",
            ]
            if extra_args:
                args.extend(extra_args)
            _ensure_executable(self.__spotty_binary)
            log_msg(
                "AUTH_DIAG operation=device-connect "
                f"binary={os.path.basename(self.__spotty_binary)} "
                f"device_name={device_name} credential_persistence=enabled result=starting",
                LOGINFO,
            )
            return subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self.__spotty_rust_env,
                **_windows_process_kwargs(),
            )

        return self.run_auth_spotty(extra_args=extra_args)


    def run_spotty(
        self,
        extra_args: List[str] = None,
        disable_discovery: bool = True,
        use_audio_backend: bool = True,
        capture_output: bool = True,
    ) -> subprocess.Popen:


        # v1.1.61 AUTH-SPLIT: future callers cannot accidentally route token
        # or Device-Connect operations through the native playback binary.
        if extra_args and any(
            auth_arg in extra_args
            for auth_arg in (
                "--authenticate",
                "--get-token",
                "--enable-oauth",
                "--client-id",
                "--save-token",
            )
        ):
            log_msg(
                "AUTH_DIAG native_auth_redirected "
                f"from={os.path.basename(self.__spotty_binary)} "
                f"to={os.path.basename(self.__auth_binary or '')}",
                LOGWARNING,
            )
            return self.run_auth_spotty(extra_args=extra_args)

        log_msg(
            "Running spotty...",
            LOGDEBUG,
        )


        try:

            args = self.__spotty_command + [
                "--cache",
                self.__spotty_cache,
            ]


            oauth_mode = False


            if extra_args:

                oauth_mode = (

                    "--authenticate" in extra_args
                    or "--get-token" in extra_args
                    or "--enable-oauth" in extra_args
                    or "--client-id" in extra_args
                    or "--save-token" in extra_args

                )


            #
            # Add normal Spotify Connect parameters.
            #
            # OAuth token generation must not get these defaults.
            #
            if not oauth_mode:

                args += SPOTTY_DEFAULT_ARGS



            if extra_args:

                args += extra_args



            #
            # librespot 0.8.x:
            #
            # OAuth/token mode:
            # no audio backend
            #
            # Playback mode:
            # ALSA backend
            #

            oauth_mode = (

                "--authenticate" in args
                or "--get-token" in args
                or "--enable-oauth" in args
                or "--client-id" in args
                or "--save-token" in args

            )


            if use_audio_backend and not oauth_mode:

                args += [
                    "--backend",
                    "alsa",
                ]



            log_msg(
                f"Spotty args: {' '.join(args)}",
                LOGDEBUG,
            )



            _ensure_executable(self.__spotty_binary)
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
                text=False,
                bufsize=0,
                env=self.__spotty_rust_env,
                **_windows_process_kwargs(),
            )


            log_msg(
                f"Spotty started PID={process.pid}",
                LOGDEBUG,
            )


            return process


        except Exception as ex:

            raise Exception(
                f"Run spotty error: {ex}"
            )



def get_spotty(
    spotty_helper: SpottyHelper, cache_directory=None
) -> Spotty:


    spotty = Spotty(cache_directory=cache_directory)


    spotty.set_spotty_path(
        spotty_helper.spotty_binary_path,
        spotty_helper.spotty_launch_command,
    )


    spotty.set_spotty_env(
        spotty_helper.spotty_rust_env
    )


    return spotty
