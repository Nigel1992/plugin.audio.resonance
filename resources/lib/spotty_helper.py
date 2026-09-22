import os
import platform
import shutil
import struct
import subprocess
import time
from typing import Union

import xbmc
import xbmcgui
from xbmc import LOGERROR, LOGINFO, LOGWARNING

from utils import log_msg, log_exception


KODI_ANDROID_INTERNAL_WRITABLE_DIR = "/data/data/org.xbmc.kodi"


def _is_android_runtime() -> bool:
    """Detect Android even if Kodi's condition flag is temporarily unavailable."""
    try:
        if xbmc.getCondVisibility("System.Platform.Android"):
            return True
    except Exception:
        pass
    if os.environ.get("ANDROID_ROOT") or os.environ.get("ANDROID_DATA"):
        return True
    return os.path.isfile("/system/bin/getprop") and (
        os.path.isdir("/data/user/0/org.xbmc.kodi")
        or os.path.isdir("/data/data/org.xbmc.kodi")
    )


def _android_runtime_dir() -> str:
    """Return Kodi's private Android data directory, preferring the real user path."""
    candidates = (
        "/data/user/0/org.xbmc.kodi",
        "/data/data/org.xbmc.kodi",
    )
    for candidate in candidates:
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return KODI_ANDROID_INTERNAL_WRITABLE_DIR
SPOTTY_SUBDIR = "deps/spotty"
SPOTTY_WINDOWS_AUTH_BINARY = "spotty.exe"
# Playback is intentionally split from the proven legacy auth/token binaries.
# Windows and ARM-Android playback payloads may be omitted from compact
# development packages; startup diagnostics then tell the user exactly which
# platform payload is missing instead of silently falling back to legacy Spotty.
SPOTTY_WINDOWS_PLAYBACK_BINARY = "spotty-playback.exe"

_cached_android_auth_command = None
_cached_android_playback_command = None
_ANDROID_CACHE_PREFIX = "plugin.audio.resonance.AndroidSpotty"


def _android_cache_window():
    """Use Kodi's global window as a lightweight cache shared by plugin invocations."""
    try:
        return xbmcgui.Window(10000)
    except Exception:
        return None


def _android_source_signature(source_path: str) -> str:
    try:
        stat = os.stat(source_path)
        mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1000000000))
        return f"{stat.st_size}:{mtime_ns}"
    except OSError:
        return ""


def _get_cached_android_command(role: str, architecture: str, source_path: str, linker: str):
    """Restore a previously self-tested Android command when the payload is unchanged."""
    window = _android_cache_window()
    signature = _android_source_signature(source_path)
    if window is None or not signature:
        return None
    key = f"{_ANDROID_CACHE_PREFIX}.{role}.{architecture}"
    raw = window.getProperty(key) or ""
    try:
        cached_signature, launch_mode, runtime_path = raw.split("|", 2)
    except ValueError:
        return None
    if cached_signature != signature or not os.path.isfile(runtime_path):
        return None
    try:
        os.chmod(runtime_path, 0o755)
    except OSError:
        return None
    if launch_mode == "direct":
        return [runtime_path]
    if launch_mode == "linker" and os.path.isfile(linker):
        return [linker, runtime_path]
    return None


def _set_cached_android_command(role: str, architecture: str, source_path: str, launch_mode: str, runtime_path: str) -> None:
    window = _android_cache_window()
    signature = _android_source_signature(source_path)
    if window is None or not signature:
        return
    key = f"{_ANDROID_CACHE_PREFIX}.{role}.{architecture}"
    window.setProperty(key, f"{signature}|{launch_mode}|{runtime_path}")


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


def _deploy_android_binary(source_path: str, runtime_name: str) -> str:
    """Copy an Android binary to Kodi's executable private directory."""
    runtime_path = os.path.join(_android_runtime_dir(), runtime_name)
    if (
        not os.path.isfile(runtime_path)
        or os.path.getsize(runtime_path) != os.path.getsize(source_path)
    ):
        temporary_path = runtime_path + ".tmp"
        shutil.copyfile(source_path, temporary_path)
        os.chmod(temporary_path, 0o755)
        os.replace(temporary_path, runtime_path)
    else:
        os.chmod(runtime_path, 0o755)
    return runtime_path


def _android_family_from_value(value: str) -> str:
    value = (value or "").strip().lower()
    if value in ("aarch64", "arm64", "arm64-v8a"):
        return "aarch64"
    if value.startswith("armv7") or value in ("arm", "armeabi", "armeabi-v7a"):
        return "armv7"
    if value in ("x86_64", "amd64"):
        return "x86_64"
    if value in ("x86", "i386", "i686"):
        return "x86"
    return ""


def _android_getprop(prop_name: str) -> str:
    """Read an Android system property without depending on shell locale."""
    try:
        result = subprocess.run(
            ["/system/bin/getprop", prop_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
        return (result.stdout or b"").decode("ascii", errors="ignore").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _android_machine_family() -> str:
    """Resolve Android ABI robustly across Kodi/Python/Fire OS variants."""
    machine = platform.machine() or ""
    bits = struct.calcsize("P") * 8
    window = _android_cache_window()
    if window is not None:
        raw = window.getProperty(f"{_ANDROID_CACHE_PREFIX}.architecture") or ""
        try:
            cached_machine, cached_bits, cached_family = raw.split("|", 2)
        except ValueError:
            cached_machine = cached_bits = cached_family = ""
        if cached_machine == machine and cached_bits == str(bits) and cached_family:
            return cached_family

    family = _android_family_from_value(machine)
    source = "platform.machine" if family else ""
    abi = ""
    abilist = ""

    if not family:
        abi = _android_getprop("ro.product.cpu.abi")
        family = _android_family_from_value(abi)
        source = "ro.product.cpu.abi" if family else ""

    if not family:
        abilist = _android_getprop("ro.product.cpu.abilist")
        for candidate in abilist.split(","):
            family = _android_family_from_value(candidate)
            if family:
                source = "ro.product.cpu.abilist"
                break

    # Last-resort fallback for Android ARM builds where Python reports no
    # machine string and getprop is unavailable/restricted.  Kodi's process
    # bitness is sufficient to select the matching bundled ARM payload.
    if not family:
        if os.path.isfile("/system/bin/linker64") and bits == 64:
            family = "aarch64"
            source = "pointer-bitness"
        elif os.path.isfile("/system/bin/linker") and bits == 32:
            family = "armv7"
            source = "pointer-bitness"

    if window is not None and family:
        window.setProperty(
            f"{_ANDROID_CACHE_PREFIX}.architecture",
            f"{machine}|{bits}|{family}",
        )
    log_msg(
        "BINARY_DIAG android_arch "
        f"machine={machine or 'unknown'} abi={abi or 'unknown'} "
        f"abilist={abilist or 'unknown'} bits={bits} "
        f"family={family or 'unknown'} source={source or 'none'}",
        LOGINFO,
    )
    return family


def _android_candidates(role: str):
    if role == "playback":
        candidates = (
            ("aarch64", "arm-android", "spotty-playback-aarch64",
             "spotty2-playback-aarch64", "/system/bin/linker64"),
            ("armv7", "arm-android", "spotty-playback",
             "spotty2-playback-armv7", "/system/bin/linker"),
            # Reserved names for future separately built x86 Android payloads.
            # Never fall back to the legacy auth/token binaries for playback.
            ("x86_64", "x86-android", "spotty-playback-x86_64",
             "spotty2-playback-x86_64", "/system/bin/linker64"),
            ("x86", "x86-android", "spotty-playback-x86",
             "spotty2-playback-x86", "/system/bin/linker"),
        )
    else:
        candidates = (
            ("aarch64", "arm-android", "spotty-aarch64",
             "spotty2-auth-legacy-aarch64", "/system/bin/linker64"),
            ("armv7", "arm-android", "spotty",
             "spotty2-auth-legacy-armv7", "/system/bin/linker"),
            ("x86_64", "x86-android", "spotty-x86_64",
             "spotty2-auth-legacy-x86_64", "/system/bin/linker64"),
            ("x86", "x86-android", "spotty",
             "spotty2-auth-legacy-x86", "/system/bin/linker"),
        )
    machine_family = _android_machine_family()
    if machine_family:
        return tuple(item for item in candidates if item[0] == machine_family)
    return candidates


def get_android_auth_spotty_command():
    """Deploy and select the matching legacy Android auth/token command."""
    global _cached_android_auth_command
    cached = _cached_android_auth_command
    if cached and os.path.isfile(cached[-1]):
        return list(cached)

    for architecture, folder, binary_name, runtime_name, linker in _android_candidates("auth"):
        if not os.path.isfile(linker):
            continue
        source_path = os.path.join(
            os.path.dirname(__file__), SPOTTY_SUBDIR, folder, binary_name
        )
        if not os.path.isfile(source_path):
            continue
        try:
            cached_command = _get_cached_android_command(
                "auth", architecture, source_path, linker
            )
            if cached_command:
                _cached_android_auth_command = cached_command
                return list(cached_command)
            runtime_path = _deploy_android_binary(source_path, runtime_name)
            commands = ([runtime_path], [linker, runtime_path])
            for launch_mode, command in (("direct", commands[0]), ("linker", commands[1])):
                if SpottyHelper.test_spotty_binary(runtime_path, command):
                    _cached_android_auth_command = command
                    _set_cached_android_command(
                        "auth", architecture, source_path, launch_mode, runtime_path
                    )
                    log_msg(
                        f"AUTH_DIAG android_launcher architecture={architecture} "
                        f"mode={launch_mode} linker={os.path.basename(linker)} binary={runtime_name}",
                        LOGINFO,
                    )
                    return list(command)
        except Exception as exc:
            log_exception(exc, f"Android auth Spotty test failed for {binary_name}")
    return None


def get_android_auth_spotty_path() -> Union[str, None]:
    command = get_android_auth_spotty_command()
    return command[-1] if command else None


def get_playback_payload_status() -> dict:
    """Describe the platform playback payload expected by this installation.

    Windows and ARM-Android playback binaries are optional package payloads so
    ARM-Linux development ZIPs can stay compact.  Legacy binaries are never
    reported as playback substitutes.
    """
    base = os.path.join(os.path.dirname(__file__), SPOTTY_SUBDIR)
    if xbmc.getCondVisibility("System.Platform.Windows"):
        expected = os.path.join(base, "windows", SPOTTY_WINDOWS_PLAYBACK_BINARY)
        return {
            "platform": "Windows x64",
            "expected": expected,
            "optional_payload": True,
            "present": os.path.isfile(expected),
        }
    if _is_android_runtime():
        family = _android_machine_family()
        names = {
            "aarch64": "spotty-playback-aarch64",
            "armv7": "spotty-playback",
            "x86_64": "spotty-playback-x86_64",
            "x86": "spotty-playback-x86",
        }
        binary_name = names.get(family, "")
        expected = os.path.join(base, "arm-android" if family in ("aarch64", "armv7") else "x86-android", binary_name) if binary_name else ""
        return {
            "platform": f"Android {family or 'unknown'}",
            "expected": expected,
            "optional_payload": True,
            "present": bool(expected and os.path.isfile(expected)),
        }
    return {
        "platform": platform.system() or "unknown",
        "expected": "",
        "optional_payload": False,
        "present": True,
    }


class SpottyHelper:

    _cached_binary_path = None
    _cached_version = None

    def __init__(self):
        cached = SpottyHelper._cached_binary_path
        if cached and os.path.exists(cached):
            self.spotty_binary_path = cached
        else:
            self.spotty_binary_path = self.__get_spotty_path()
            SpottyHelper._cached_binary_path = self.spotty_binary_path

        if _is_android_runtime():
            self.spotty_launch_command = list(
                _cached_android_playback_command or [self.spotty_binary_path]
            )
        else:
            self.spotty_launch_command = [self.spotty_binary_path]

        self.spotty_rust_env = os.environ.copy()

        if _is_android_runtime():
            self.spotty_rust_env["TMPDIR"] = _android_runtime_dir()

        if self.spotty_binary_path and SpottyHelper._cached_version is None:
            self.__log_spotty_version()

    @staticmethod
    def _windows_binary_running(binary_name: str) -> bool:
        """Return True while a Windows process with the exact image name exists."""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {binary_name}", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=False,
                check=False,
                timeout=2,
                **_windows_process_kwargs(),
            )
            output = (result.stdout or b"").decode("ascii", errors="ignore").lower()
            return f'"{binary_name.lower()}"' in output
        except (OSError, subprocess.TimeoutExpired):
            return False

    def kill_all_spotties(self) -> None:

        if not self.spotty_binary_path:
            return

        if platform.system() == "Windows":

            # Auth/token and playback are separate Windows roles.  Use a
            # synchronous forced shutdown because a still-running executable
            # keeps the addon directory locked and makes Kodi updates fail.
            for binary_name in dict.fromkeys((
                SPOTTY_WINDOWS_AUTH_BINARY,
                SPOTTY_WINDOWS_PLAYBACK_BINARY,
            )):
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/IM", binary_name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                        check=False,
                        timeout=3,
                        **_windows_process_kwargs(),
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    log_exception(exc, f"Unable to terminate Windows Spotty process {binary_name}")

                # Do not return to Kodi until Windows has released the image.
                deadline = time.perf_counter() + 2.0
                while self._windows_binary_running(binary_name):
                    if time.perf_counter() >= deadline:
                        log_msg(
                            f"SHUTDOWN_DIAG binary={binary_name} status=still_running",
                            LOGWARNING,
                        )
                        break
                    time.sleep(0.05)
                else:
                    log_msg(
                        f"SHUTDOWN_DIAG binary={binary_name} status=terminated",
                        LOGINFO,
                    )

            remaining = [
                name for name in (SPOTTY_WINDOWS_AUTH_BINARY, SPOTTY_WINDOWS_PLAYBACK_BINARY)
                if self._windows_binary_running(name)
            ]
            log_msg(
                "SHUTDOWN_DIAG windows_spotty_processes_remaining="
                + str(len(remaining))
                + (" names=" + ",".join(remaining) if remaining else ""),
                LOGINFO if not remaining else LOGWARNING,
            )

        else:

            sp_binary_file = os.path.basename(
                self.spotty_binary_path
            )

            try:
                subprocess.run(
                    ["killall", "--quiet", sp_binary_file],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=3,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                log_exception(exc, "Unable to terminate remaining Spotty processes")

    @staticmethod
    def __get_spotty_path() -> Union[str, None]:

        spotty_path = None

        log_msg(
            "BINARY_DIAG platform_probe "
            f"kodi_android={bool(xbmc.getCondVisibility('System.Platform.Android'))} "
            f"android_runtime={_is_android_runtime()} machine={platform.machine() or 'unknown'}",
            LOGINFO,
        )

        if xbmc.getCondVisibility("System.Platform.Windows"):

            spotty_path = os.path.join(
                os.path.dirname(__file__),
                SPOTTY_SUBDIR,
                "windows",
                SPOTTY_WINDOWS_PLAYBACK_BINARY,
            )

        elif xbmc.getCondVisibility("System.Platform.OSX"):

            spotty_path = os.path.join(
                os.path.dirname(__file__),
                SPOTTY_SUBDIR,
                "macos",
                "spotty",
            )

        elif _is_android_runtime():

            spotty_path = SpottyHelper.__get_android_spotty_path()

        elif xbmc.getCondVisibility("System.Platform.Linux"):

            spotty_path = SpottyHelper.__get_linux_spotty_path()

        if not spotty_path:

            log_msg(
                "Spotty: failed to detect architecture.",
                loglevel=LOGERROR,
            )

            return None

        try:

            # Kodi/ZIP extraction can normalize executable bits on some
            # LibreELEC installations.  Native Spotty binaries are shipped as
            # 0755 and are normalized again here before validation/launch.
            os.chmod(spotty_path, 0o755)

        except Exception as exc:

            log_exception(
                exc,
                "Unable to set executable permission",
            )

            return None

        log_msg(
            f"Spotty binary selected: '{spotty_path}'"
        )

        return spotty_path

    @staticmethod
    def __get_android_spotty_path() -> Union[str, None]:
        global _cached_android_playback_command
        for architecture, folder, binary_name, runtime_name, linker in _android_candidates("playback"):

            if not os.path.isfile(linker):
                continue

            binary = os.path.join(
                os.path.dirname(__file__),
                SPOTTY_SUBDIR,
                folder,
                binary_name,
            )

            if not os.path.exists(binary):
                continue

            try:
                cached_command = _get_cached_android_command(
                    "playback", architecture, binary, linker
                )
                if cached_command:
                    _cached_android_playback_command = cached_command
                    return cached_command[-1]
                runtime_binary = _deploy_android_binary(binary, runtime_name)
                commands = ([runtime_binary], [linker, runtime_binary])
                for launch_mode, command in (("direct", commands[0]), ("linker", commands[1])):
                    if SpottyHelper.__test_spotty(runtime_binary, command):
                        _cached_android_playback_command = command
                        _set_cached_android_command(
                            "playback", architecture, binary, launch_mode, runtime_binary
                        )
                        log_msg(
                            f"PLAYBACK_DIAG android_launcher architecture={architecture} "
                            f"mode={launch_mode} linker={os.path.basename(linker)} binary={runtime_name}",
                            LOGINFO,
                        )
                        return runtime_binary

            except Exception as exc:

                log_exception(
                    exc,
                    "Android spotty test failed",
                )

        return None

    @classmethod
    def test_spotty_binary(cls, binary_path: str, command=None) -> bool:
        """Public validator used for separately deployed Android auth binaries."""
        return cls.__test_spotty(binary_path, command)

    @staticmethod
    def __normalize_linux_arm_permissions() -> None:
        """Force all bundled Linux ARM Spotty binaries to mode 0755.

        Kodi/LibreELEC may normalize ZIP permissions during addon installation.
        Do not rely on the archive mode bits: repair the complete ARM binary
        set before selecting or launching any one of them.
        """
        arm_dir = os.path.join(
            os.path.dirname(__file__),
            SPOTTY_SUBDIR,
            "arm-linux",
        )
        binary_names = (
            "spotty",
            "spotty-aarch64",
            "spotty-armhf",
            "spotty-muslhf",
        )
        normalized = []
        for binary_name in binary_names:
            binary_path = os.path.join(arm_dir, binary_name)
            if not os.path.isfile(binary_path):
                continue
            try:
                os.chmod(binary_path, 0o755)
                normalized.append(binary_name)
            except Exception as exc:
                log_exception(
                    exc,
                    f"Unable to set executable permission on {binary_name}",
                )

        if normalized:
            log_msg(
                "Normalized Linux ARM Spotty permissions to 0755: "
                + ", ".join(normalized)
            )

    @staticmethod
    def __get_linux_spotty_path() -> Union[str, None]:

        SpottyHelper.__normalize_linux_arm_permissions()
        architecture = platform.machine()

        log_msg(
            f"Reported architecture: '{architecture}'."
        )

        if architecture in (
            "aarch64",
            "arm64",
        ):

            candidates = [
                ("arm-linux", "spotty-aarch64"),
                ("arm-linux", "spotty"),
                ("arm-linux", "spotty-muslhf"),
            ]

        elif architecture in (
            "armv7l",
            "armv6l",
            "armhf",
        ):

            candidates = [
                ("arm-linux", "spotty-armhf"),
                ("arm-linux", "spotty"),
                ("arm-linux", "spotty-muslhf"),
            ]

        elif architecture in (
            "x86_64",
            "AMD64",
        ):

            candidates = [
                ("x86-linux", "spotty-x86_64"),
                ("x86-linux", "spotty"),
            ]

        elif architecture in (
            "x86",
            "i386",
            "i486",
            "i586",
            "i686",
        ):

            candidates = [
                ("x86-linux", "spotty"),
            ]

        else:

            candidates = [
                ("arm-linux", "spotty"),
                ("arm-linux", "spotty-muslhf"),
            ]

        for folder, binary_name in candidates:

            binary = os.path.join(
                os.path.dirname(__file__),
                SPOTTY_SUBDIR,
                folder,
                binary_name,
            )

            if not os.path.exists(binary):
                continue

            if SpottyHelper.__test_spotty(binary):
                return binary

        return None

    @classmethod
    def __test_spotty(
        cls,
        binary_path: str,
        command=None,
    ) -> bool:

        try:

            # Normalize all POSIX playback candidates to rwxr-xr-x.
            os.chmod(binary_path, 0o755)

            launch_command = list(command or [binary_path])
            version = subprocess.run(
                launch_command + ["--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
                **_windows_process_kwargs(),
            )

            output = version.stdout.strip()

            if _is_android_runtime():
                log_msg(
                    "BINARY_DIAG android_selftest "
                    f"command={os.path.basename(launch_command[0])} "
                    f"rc={version.returncode} output={(output or 'empty')[:240]!r}",
                    LOGINFO,
                )

            if output:

                log_msg(output)

                if (
                    "spotty v" in output
                    or "librespot" in output
                ):
                    return True

            args = launch_command + [
                "--name",
                "selftest",
                "--disable-discovery",
                "-x",
                "-v",
            ]

            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                **_windows_process_kwargs(),
            )

            try:

                stdout, _ = process.communicate(
                    timeout=5
                )

            except subprocess.TimeoutExpired:

                process.kill()
                stdout, _ = process.communicate()

            output = stdout.decode(
                "UTF-8",
                errors="replace",
            )

            if output:
                log_msg(output)

            if (
                "ok spotty" in output
                or "spotty v" in output
                or "librespot" in output
            ):
                return True

        except PermissionError as exc:

            if _is_android_runtime() and list(command or [binary_path]) == [binary_path]:
                # Android commonly mounts Kodi's private files as noexec. The
                # immediately following linker/linker64 attempt is the normal,
                # supported launcher path rather than an add-on failure.
                log_msg(
                    "BINARY_DIAG android_direct_exec_unavailable "
                    f"binary={os.path.basename(binary_path)} "
                    f"errno={getattr(exc, 'errno', 'unknown')} "
                    "fallback=linker",
                    LOGINFO,
                )
            else:
                log_exception(exc, "Test spotty binary error")

        except Exception as exc:

            log_exception(
                exc,
                "Test spotty binary error",
            )

        return False

    def __log_spotty_version(self):

        try:

            result = subprocess.run(
                self.spotty_launch_command + ["--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
                **_windows_process_kwargs(),
            )

            version = result.stdout.strip()

            if version:
                SpottyHelper._cached_version = version
                log_msg(f"Spotty version: {version}")

        except Exception as exc:

            log_exception(
                exc,
                "Unable to read spotty version",
            )
