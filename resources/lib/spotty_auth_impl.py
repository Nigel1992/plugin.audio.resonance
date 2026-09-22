import csv
import io
import json
import os
import subprocess
import time
from typing import Dict, Union

import xbmcaddon
from xbmc import LOGDEBUG, LOGINFO, LOGERROR, LOGWARNING

import utils
from spotty import (
    Spotty,
    SPOTTY_CACHE_DIR_NAME,
    SPOTTY_CREDENTIALS_FILENAME,
)
from string_ids import (
    AUTHENTICATE_FAILED_STR_ID,
    AUTHENTICATION_PROGRAM_FAILED_STR_ID,
)
from process_utils import is_process_running, terminate_process
from utils import log_msg, log_exception, ADDON_ID


# Device-Connect authentication uses the same Spotify Connect/Zeroconf
# device name as normal playback. The main service deliberately does not
# start its Connect receiver until credentials exist, so this port is free.
ZEROCONF_AUTH_PORT = 10002

CONNECT_PID_FILENAME = "device-connect.pid"
CONNECT_START_LOCK_FILENAME = "device-connect.start.lock"
PERSISTENT_CREDENTIALS_FILENAME = "spotify-credentials.json"


class SpottyAuth:

    def __init__(self, spotty: Spotty):
        self.__spotty = spotty

    def _connect_start_lock_file(self) -> str:
        return os.path.join(
            os.path.dirname(self.__spotty.get_spotty_credentials_file()),
            CONNECT_START_LOCK_FILENAME,
        )

    def _persistent_credentials_file(self) -> str:
        return os.path.join(utils.ADDON_DATA_PATH, PERSISTENT_CREDENTIALS_FILENAME)

    def _restore_persistent_credentials(self, credentials_file: str) -> None:
        """Restore credentials when cache cleaners remove spotty-cache."""
        if os.path.exists(credentials_file):
            return
        persistent_file = self._persistent_credentials_file()
        try:
            if not os.path.exists(persistent_file):
                return
            os.makedirs(os.path.dirname(credentials_file), exist_ok=True)
            with open(persistent_file, "rb") as source:
                payload = source.read()
            with open(credentials_file, "wb") as target:
                target.write(payload)
            os.chmod(credentials_file, 0o600)
            log_msg(
                "AUTH_DIAG credentials_restored=true source=persistent_vault",
                LOGINFO,
            )
        except OSError as exc:
            log_msg(f"Could not restore persistent Spotify credentials: {exc}", LOGWARNING)

    def _persist_credentials(self, credentials_file: str) -> None:
        persistent_file = self._persistent_credentials_file()
        temp_file = persistent_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(persistent_file), exist_ok=True)
            with open(credentials_file, "rb") as source:
                payload = source.read()
            with open(temp_file, "wb") as target:
                target.write(payload)
            os.chmod(temp_file, 0o600)
            os.replace(temp_file, persistent_file)
        except OSError as exc:
            log_msg(f"Could not persist Spotify credentials: {exc}", LOGWARNING)

    def _acquire_connect_start_lock(self, timeout: float = 1.5) -> bool:
        lock_file = self._connect_start_lock_file()
        # A clean installation has no spotty-cache directory until the first
        # credential or token is written.  The start lock must be able to be
        # the first file created there.
        try:
            os.makedirs(os.path.dirname(lock_file), exist_ok=True)
        except OSError as exc:
            log_msg(
                "AUTH_DIAG operation=device-connect "
                f"result=start_lock_directory_failed error='{exc}'",
                LOGWARNING,
            )
            return False
        deadline = time.perf_counter() + max(0.1, timeout)
        while True:
            try:
                fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
                finally:
                    os.close(fd)
                return True
            except FileExistsError:
                # Recover a stale startup lock left behind by an abnormal Kodi
                # or Python process termination. A normal launch holds the lock
                # for well under one second.
                try:
                    if time.time() - os.path.getmtime(lock_file) > 5.0:
                        os.remove(lock_file)
                        log_msg(
                            "AUTH_DIAG operation=device-connect stale_start_lock_removed=true",
                            LOGWARNING,
                        )
                        continue
                except OSError:
                    pass
                if time.perf_counter() >= deadline:
                    return False
                time.sleep(0.05)
            except OSError:
                return False

    def _release_connect_start_lock(self) -> None:
        try:
            os.remove(self._connect_start_lock_file())
        except OSError:
            pass

    def _remove_connect_pid_if_matches(self, pid: int) -> None:
        try:
            if self._read_connect_pid() == pid:
                os.remove(self._connect_pid_file())
        except OSError:
            pass

    def _connect_pid_file(self) -> str:
        return os.path.join(
            os.path.dirname(self.__spotty.get_spotty_credentials_file()),
            CONNECT_PID_FILENAME,
        )

    def _read_connect_pid(self) -> int:
        try:
            with open(self._connect_pid_file(), "r", encoding="utf-8") as file:
                return int(file.read().strip())
        except (OSError, ValueError):
            return 0

    def is_connect_process_running(self) -> bool:
        pid = self._read_connect_pid()
        if not pid:
            return False
        if os.name == "nt":
            return is_process_running(pid, ("spotty.exe",))
        try:
            cmdline_path = f"/proc/{pid}/cmdline"
            if not os.path.exists(cmdline_path):
                return False
            with open(cmdline_path, "rb") as file:
                cmdline = file.read().replace(b"\0", b" ").decode("utf-8", errors="replace")
            cache_dir = os.path.dirname(self.__spotty.get_spotty_credentials_file())
            return "spotty" in cmdline and cache_dir in cmdline and "Kodi-Resonance" in cmdline
        except OSError:
            return False

    def _windows_spotty_auth_pids(self):
        """Return running Windows legacy auth spotty.exe PIDs.

        The Windows split configuration reserves spotty.exe exclusively for
        Device Connect/token work. Playback uses spotty-playback.exe, so an
        untracked spotty.exe can safely be treated as a stale auth receiver.
        """
        if os.name != "nt":
            return []
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(
                [
                    "tasklist",
                    "/FI", "IMAGENAME eq spotty.exe",
                    "/FO", "CSV",
                    "/NH",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                timeout=4,
                creationflags=creationflags,
            )
            output = (result.stdout or b"").decode("ascii", errors="ignore")
            pids = []
            for row in csv.reader(io.StringIO(output)):
                if len(row) < 2 or row[0].strip().lower() != "spotty.exe":
                    continue
                try:
                    pid = int(row[1].strip())
                except (TypeError, ValueError):
                    continue
                if pid > 0 and is_process_running(pid, ("spotty.exe",)):
                    pids.append(pid)
            return pids
        except Exception as exc:
            log_msg(
                f"AUTH_DIAG operation=device-connect orphan_scan_failed error='{exc}'",
                LOGWARNING,
            )
            return []

    def _cleanup_orphaned_windows_connect_processes(self) -> int:
        """Terminate untracked Windows legacy Device Connect processes.

        A prior Kodi/add-on shutdown can leave spotty.exe alive without a
        usable device-connect.pid. That stale receiver owns Zeroconf port
        10002, causing every subsequent spotty.exe launch to exit with rc=1.
        This cleanup runs only while the cross-process start lock is held.
        """
        if os.name != "nt":
            return 0

        tracked_pid = self._read_connect_pid()
        tracked_is_valid = bool(
            tracked_pid and is_process_running(tracked_pid, ("spotty.exe",))
        )
        if tracked_is_valid:
            return 0

        # Remove a stale PID file before discovering untracked receivers.
        if tracked_pid:
            try:
                os.remove(self._connect_pid_file())
            except OSError:
                pass

        cleaned = 0
        for pid in self._windows_spotty_auth_pids():
            if terminate_process(pid, ("spotty.exe",)):
                cleaned += 1
                log_msg(
                    "AUTH_DIAG operation=device-connect "
                    f"orphan_process_terminated=true pid={pid}",
                    LOGWARNING,
                )

        if cleaned:
            # Give Windows enough time to release TCP/Zeroconf resources.
            deadline = time.perf_counter() + 1.0
            while time.perf_counter() < deadline:
                if not self._windows_spotty_auth_pids():
                    break
                time.sleep(0.05)
            log_msg(
                "AUTH_DIAG operation=device-connect "
                f"orphan_cleanup_complete=true count={cleaned}",
                LOGINFO,
            )
        return cleaned

    def _stop_previous_connect_process(self) -> None:
        """Stop only the previous Resonance Device Connect process, if any."""
        pid_file = self._connect_pid_file()
        if not os.path.exists(pid_file):
            return

        try:
            with open(pid_file, "r", encoding="utf-8") as file:
                pid = int(file.read().strip())

            if os.name == "nt":
                terminate_process(pid, ("spotty.exe",))
                time.sleep(0.2)
            else:
                cmdline_path = f"/proc/{pid}/cmdline"
                if not os.path.exists(cmdline_path):
                    return
                try:
                    with open(cmdline_path, "rb") as file:
                        cmdline = file.read().replace(b"\0", b" ").decode("utf-8", errors="replace")
                    cache_dir = os.path.dirname(self.__spotty.get_spotty_credentials_file())
                    if "spotty" in cmdline and cache_dir in cmdline:
                        terminate_process(pid)
                        time.sleep(0.2)
                except OSError:
                    pass
        except (OSError, ValueError, SystemError):
            pass
        finally:
            try:
                os.remove(pid_file)
            except OSError:
                pass

    def keep_connect_process(self, process: subprocess.Popen) -> None:
        """Keep the authenticated Kodi-Resonance receiver alive after the GUI closes."""
        try:
            with open(self._connect_pid_file(), "w", encoding="utf-8") as file:
                file.write(str(process.pid))
            log_msg(f"Keeping Kodi-Resonance Device Connect process alive (PID={process.pid}).")
        except OSError as exc:
            log_msg(f"Could not persist Device Connect PID: {exc}", LOGWARNING)

    def stop_device_connect_process(self) -> None:
        """Stop the temporary v1.1.57-compatible Device Connect receiver."""
        was_running = self.is_connect_process_running()
        self._stop_previous_connect_process()
        if was_running:
            log_msg(
                "AUTH_DIAG legacy_device_connect_stopped credentials_complete=true",
                LOGINFO,
            )


    def start_zeroconf_authenticate(self, fresh: bool = True) -> Union[None, subprocess.Popen]:
        """Start a fresh Spotify Device Connect authentication session.

        An explicit GUI authentication request is treated as an account
        switch/re-login.  Existing Resonance credentials/token files are
        backed up first, so an old credentials.json can never make the GUI
        report success before the new Device Connect handshake completed.
        """
        lock_acquired = self._acquire_connect_start_lock()
        if not lock_acquired:
            if not fresh and self.is_connect_process_running():
                log_msg(
                    "AUTH_DIAG operation=device-connect result=reused_existing reason=start_race",
                    LOGINFO,
                )
                return None
            log_msg(
                "AUTH_DIAG operation=device-connect result=start_lock_timeout",
                LOGWARNING,
            )
            return None

        try:
            # Re-check after acquiring the cross-process start lock. Another
            # Kodi Python context may have started the receiver milliseconds
            # earlier while this caller was waiting for the lock.
            if not fresh and self.is_connect_process_running():
                log_msg(
                    "AUTH_DIAG operation=device-connect result=reused_existing reason=start_race",
                    LOGINFO,
                )
                return None

            # Windows may retain a legacy spotty.exe after Kodi/add-on
            # shutdown even though the PID file is missing/stale. Since
            # spotty.exe is auth-only in the split configuration, remove such
            # orphan receivers before binding Zeroconf port 10002 again.
            self._cleanup_orphaned_windows_connect_processes()
            self._stop_previous_connect_process()

            credentials_file = self.__spotty.get_spotty_credentials_file()
            credentials_backup = self.__spotty.get_spotty_credentials_backup_file()
            token_file = self.__spotty.get_spotty_token_file()
            token_backup = self.__spotty.get_spotty_token_backup_file()

            os.makedirs(os.path.dirname(credentials_file), exist_ok=True)

            if fresh:
                for source, backup, label in (
                    (credentials_file, credentials_backup, "credentials.json"),
                    (token_file, token_backup, "spotty-token"),
                ):
                    if os.path.exists(source):
                        try:
                            if os.path.exists(backup):
                                os.remove(backup)
                            os.replace(source, backup)
                            log_msg(f"Backed up previous {label} to '{backup}'.")
                        except OSError as exc:
                            log_msg(f"Could not back up {label}: {exc}", LOGWARNING)
                            try:
                                os.remove(source)
                            except OSError:
                                pass

                # Clear the in-memory token as soon as re-authentication starts.
                utils.cache_auth_token("")
                utils.cache_auth_token_expires_at("")

            # v1.1.57 compatibility flow: Device Connect and the later
            # --save-token operation use the same legacy auth binary. On
            # Windows this is windows/spotty.exe; POSIX keeps auth-compat.
            args = [
                "--zeroconf-port", str(ZEROCONF_AUTH_PORT),
            ]
            args.extend(self.__spotty.get_device_connect_zeroconf_args())

            log_msg(f"Device Connect credentials target: {credentials_file}")
            log_msg(f"Device Connect token target: {token_file}")
            log_msg("Device Connect Spotty args: " + " ".join(args))

            process = self.__spotty.run_device_connect_spotty(extra_args=args)

            # Publish the PID immediately. The previous implementation wrote
            # it only after the 400 ms startup probe, leaving a race window in
            # which the service and GUI could both launch spotty.exe on port
            # 10002.
            self.keep_connect_process(process)

            startup_deadline = time.perf_counter() + 0.40
            while process.poll() is None and time.perf_counter() < startup_deadline:
                time.sleep(0.05)

            if process.poll() is not None:
                output = ""
                try:
                    if process.stdout is not None:
                        output = process.stdout.read() or ""
                except Exception as exc:
                    output = f"<could not read Spotty output: {exc}>"
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                output = " ".join(str(output).replace("\r", " ").replace("\n", " ").split())
                self._remove_connect_pid_if_matches(process.pid)
                log_msg(
                    "AUTH_DIAG operation=device-connect result=immediate_exit "
                    f"rc={process.returncode} output='{output[:1200]}'",
                    LOGERROR,
                )
                return None

            log_msg(
                f'Spotify Device Connect authentication started as "{utils.get_spotify_device_name()}" '
                f"on port {ZEROCONF_AUTH_PORT}.",
                LOGINFO,
            )
            return process

        except Exception as exc:
            log_exception(exc, "Zeroconf authentication error")
            return None
        finally:
            if lock_acquired:
                self._release_connect_start_lock()


    def ensure_device_connect_running(self, fresh: bool = False) -> bool:
        """Ensure Kodi-Resonance is advertised for automatic Device Connect.

        Used when Resonance has no usable authentication state. Repeated Kodi
        directory invocations reuse the same process instead of restarting it.
        """
        if self.is_connect_process_running():
            return True
        process = self.start_zeroconf_authenticate(fresh=fresh)
        if process is None:
            # A parallel Kodi Python context may have won the atomic start
            # race. Treat the already-running shared receiver as success.
            return self.is_connect_process_running()
        return True

    def ensure_spotify_connect_receiver_running(self) -> bool:
        """Keep an authenticated Spotify Connect receiver visible to Spotify apps."""
        if self.is_connect_process_running():
            return True
        lock_acquired = self._acquire_connect_start_lock()
        if not lock_acquired:
            return self.is_connect_process_running()
        try:
            if self.is_connect_process_running():
                return True
            self._stop_previous_connect_process()
            args = [
                "--system-cache", os.path.dirname(self.__spotty.get_spotty_credentials_file()),
                "--verbose",
                "--name", utils.get_spotify_device_name(),
                "--zeroconf-port", str(ZEROCONF_AUTH_PORT),
            ]
            args.extend(self.__spotty.get_device_connect_zeroconf_args())
            process = self.__spotty.run_spotty(
                extra_args=args,
                disable_discovery=False,
                use_audio_backend=True,
                capture_output=False,
            )
            self.keep_connect_process(process)
            startup_deadline = time.perf_counter() + 0.60
            while process.poll() is None and time.perf_counter() < startup_deadline:
                time.sleep(0.05)
            if process.poll() is not None:
                output = ""
                try:
                    if process.stdout is not None:
                        output = process.stdout.read() or ""
                except Exception as exc:
                    output = f"<could not read Spotty output: {exc}>"
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                output = " ".join(str(output).replace("\r", " ").replace("\n", " ").split())
                self._remove_connect_pid_if_matches(process.pid)
                utils.log_msg(
                    "AUTH_DIAG operation=spotify-connect-receiver result=immediate_exit "
                    f"rc={process.returncode} output='{output[:1200]}'",
                    LOGWARNING,
                )
                return False
            utils.log_msg(
                "AUTH_DIAG operation=spotify-connect-receiver result=started "
                f"pid={process.pid}",
                LOGINFO,
            )
            return True
        except Exception as exc:
            utils.log_msg(
                "AUTH_DIAG operation=spotify-connect-receiver result=failed "
                f"error={type(exc).__name__}: {exc}",
                LOGWARNING,
            )
            return False
        finally:
            if lock_acquired:
                self._release_connect_start_lock()

    def zeroconf_authenticated_ok(self) -> bool:
        """Return True only for a complete, newly written credentials file."""
        credentials_file = self.__spotty.get_spotty_credentials_file()

        self._restore_persistent_credentials(credentials_file)

        if not os.path.exists(credentials_file):
            return False

        try:
            if os.path.getsize(credentials_file) < 32:
                return False

            with open(credentials_file, "r", encoding="utf-8") as file:
                credentials = json.load(file)

            username = (credentials.get("username") or "").strip()
            auth_data = credentials.get("auth_data") or credentials.get("authData") or ""

            if not username or not auth_data:
                return False

            self._persist_credentials(credentials_file)
            return True

        except (OSError, ValueError, TypeError) as exc:
            log_msg(f"Credentials file exists but is not ready yet: {exc}", LOGDEBUG)
            return False


    @staticmethod
    def get_zeroconf_program_failed_msg() -> str:

        return xbmcaddon.Addon(
            id=ADDON_ID
        ).getLocalizedString(
            AUTHENTICATION_PROGRAM_FAILED_STR_ID
        )



    @staticmethod
    def get_zeroconf_authentication_failed_msg() -> str:

        msg = xbmcaddon.Addon(
            id=ADDON_ID
        ).getLocalizedString(
            AUTHENTICATE_FAILED_STR_ID
        )


        cred_file = (
            f"<ADDON_DATA_DIR>/{SPOTTY_CACHE_DIR_NAME}/"
            f"{SPOTTY_CREDENTIALS_FILENAME}"
        )


        return f'{msg}\n\n"{cred_file}".'




    def has_credentials(self) -> bool:
        """Return True only for complete reusable librespot credentials.

        Merely finding a stale, truncated or malformed ``credentials.json``
        must not mark Resonance as authenticated. The same strict check is
        used after Device Connect, so restarts and first pairing agree.
        """
        return self.zeroconf_authenticated_ok()



    def renew_token(self, quiet: bool = False) -> str:
        """Create/refresh spotty-token from persistent credentials.json.

        Returns the Web API access token on success, otherwise an empty
        string.  The token is also cached in Kodi window properties so all
        plugin/service invocations can immediately reuse it.
        """
        if not self.has_credentials():
            log_msg(
                "Cannot renew Spotify token: credentials.json is missing.",
                LOGWARNING,
            )
            return ""

        log_msg(
            "AUTH_DIAG state=token_restore_started "
            "source=credentials.json credentials_preserved=true",
            LOGINFO,
        )

        token = self.__spotty.get_access_token(quiet=quiet)
        if not token:
            log_msg(
                "Could not create Spotify Web API token from Spotty credentials.",
                LOGWARNING if quiet else LOGERROR,
            )
            return ""

        log_msg(
            f"Spotify Web API token created/refreshed successfully; "
            f"expires at {utils.get_time_str(int(time.time()) + 3600)}.",
            LOGINFO,
        )
        log_msg(
            "AUTH_DIAG state=token_available "
            "source=credentials.json credentials_preserved=true",
            LOGINFO,
        )
        return token
