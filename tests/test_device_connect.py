"""Device Connect receiver checks that do not need a Kodi session.

The receiver runs as a long lived child process.  These tests cover the
parts that are easy to break and hard to see on a real box:

* the Kodi dialog keywords, which must match the Kodi 21 Python API;
* the receiver output, which must not go into a pipe nobody reads;
* the start backoff and the stale receiver cleanup;
* the fallback from the dedicated ``--authenticate`` mode to the plain
  librespot discovery mode.
"""
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


def _kodi_stub(**attrs):
    module = types.ModuleType("stub")
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


sys.modules.setdefault(
    "xbmc",
    _kodi_stub(
        LOGDEBUG=0,
        LOGINFO=0,
        LOGERROR=0,
        LOGWARNING=0,
        log=lambda *a, **k: None,
        getCondVisibility=lambda *a, **k: False,
        executebuiltin=lambda *a, **k: None,
    ),
)
sys.modules.setdefault("xbmcaddon", _kodi_stub(Addon=lambda *a, **k: types.SimpleNamespace(
    getAddonInfo=lambda key: "/tmp/resonance-test-profile",
    getSetting=lambda key: "",
    setSetting=lambda key, value: None,
)))
sys.modules.setdefault(
    "xbmcgui",
    _kodi_stub(
        Window=lambda *a, **k: types.SimpleNamespace(
            getProperty=lambda *a, **k: "",
            setProperty=lambda *a, **k: None,
            clearProperty=lambda *a, **k: None,
        ),
    ),
)
sys.modules.setdefault(
    "xbmcvfs",
    _kodi_stub(translatePath=lambda path: path),
)

LIB = pathlib.Path(__file__).parents[1] / "resources/lib"
sys.path.insert(0, str(LIB))

import spotty  # noqa: E402
import spotty_auth_impl  # noqa: E402


class _FakeProcess:
    """Minimal stand-in for a started receiver."""

    def __init__(self, pid=4242, returncode=None):
        self.pid = pid
        self.returncode = returncode
        self.stdout = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


class _FakeSpotty:
    """Spotty stand-in that records how a receiver was started."""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.calls = []
        self.process = _FakeProcess()

    def _path(self, name):
        return os.path.join(self.cache_dir, name)

    def get_spotty_credentials_file(self):
        return self._path("credentials.json")

    def get_spotty_credentials_backup_file(self):
        return self._path("credentials.json.bak")

    def get_spotty_token_file(self):
        return self._path("spotty-token")

    def get_spotty_token_backup_file(self):
        return self._path("spotty-token.bak")

    def get_spotty_device_connect_log_file(self):
        return self._path("device-connect.log")

    def get_spotty_connect_receiver_log_file(self):
        return self._path("connect-receiver.log")

    def get_device_connect_zeroconf_args(self):
        return []

    def uses_native_device_connect_binary(self):
        return True

    def run_device_connect_spotty(self, extra_args=None, log_file=None, authenticate=True):
        self.calls.append({
            "extra_args": list(extra_args or []),
            "log_file": log_file,
            "authenticate": authenticate,
        })
        return self.process


class DialogKeywordTests(unittest.TestCase):
    """Kodi 21 rejects camelCase dialog keywords with a TypeError."""

    def test_plugin_uses_lowercase_yesno_keywords(self):
        source = (pathlib.Path(__file__).parents[1] / "plugin.py").read_text(encoding="utf-8")
        self.assertNotIn("noLabel=", source)
        self.assertNotIn("yesLabel=", source)
        self.assertIn("nolabel=", source)
        self.assertIn("yeslabel=", source)


class ReceiverOutputTests(unittest.TestCase):
    """The receiver output must not land in a pipe nobody reads."""

    def test_device_connect_receiver_writes_to_a_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_file = os.path.join(tmp, "device-connect.log")
            helper = spotty.Spotty(cache_directory=tmp)
            with mock.patch.object(spotty, "get_spotify_device_name", return_value="Kodi-Test"), \
                    mock.patch.object(spotty, "_ensure_executable"), \
                    mock.patch.object(helper, "_Spotty__spotty_command", ["spotty"]), \
                    mock.patch.object(helper, "_Spotty__spotty_binary", "spotty"), \
                    mock.patch.object(spotty.subprocess, "Popen") as popen:
                popen.return_value = _FakeProcess()
                helper.run_device_connect_spotty(
                    extra_args=["--zeroconf-port", "10002"],
                    log_file=log_file,
                )
            kwargs = popen.call_args[1]
            self.assertIsNot(kwargs["stdout"], subprocess.PIPE)
            self.assertIsNot(kwargs["stderr"], subprocess.PIPE)
            self.assertTrue(os.path.isfile(log_file))

    def test_receiver_environment_asks_for_full_logging(self):
        helper = spotty.Spotty(cache_directory=tempfile.mkdtemp())
        env = helper._receiver_env()
        self.assertIn("RUST_LOG", env)
        self.assertIn("info", env["RUST_LOG"])

    def test_the_native_receiver_uses_the_authenticate_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = spotty.Spotty(cache_directory=tmp)
            with mock.patch.object(spotty, "get_spotify_device_name", return_value="Kodi-Test"), \
                    mock.patch.object(spotty, "_ensure_executable"), \
                    mock.patch.object(helper, "_Spotty__spotty_command", ["spotty"]), \
                    mock.patch.object(helper, "_Spotty__spotty_binary", "spotty"), \
                    mock.patch.object(spotty.subprocess, "Popen") as popen:
                popen.return_value = _FakeProcess()
                helper.run_device_connect_spotty(
                    extra_args=["--zeroconf-port", "10002"],
                    log_file=os.path.join(tmp, "device-connect.log"),
                )
            args = popen.call_args[0][0]
            self.assertIn("--authenticate", args)
            self.assertIn("--zeroconf-port", args)

    def test_the_discovery_mode_omits_the_authenticate_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = spotty.Spotty(cache_directory=tmp)
            with mock.patch.object(spotty, "get_spotify_device_name", return_value="Kodi-Test"), \
                    mock.patch.object(spotty, "_ensure_executable"), \
                    mock.patch.object(helper, "_Spotty__spotty_command", ["spotty"]), \
                    mock.patch.object(helper, "_Spotty__spotty_binary", "spotty"), \
                    mock.patch.object(spotty.subprocess, "Popen") as popen:
                popen.return_value = _FakeProcess()
                helper.run_device_connect_spotty(
                    log_file=os.path.join(tmp, "device-connect.log"),
                    authenticate=False,
                )
            args = popen.call_args[0][0]
            self.assertNotIn("--authenticate", args)

    def test_android_receiver_does_not_force_the_alsa_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = spotty.Spotty(cache_directory=tmp)
            with mock.patch.object(spotty, "_is_android_runtime", return_value=True), \
                    mock.patch.object(helper, "_Spotty__spotty_command", ["spotty"]), \
                    mock.patch.object(helper, "_Spotty__spotty_binary", "spotty"), \
                    mock.patch.object(spotty, "_ensure_executable"), \
                    mock.patch.object(subprocess, "Popen") as popen:
                popen.return_value = _FakeProcess()
                helper.run_spotty(
                    extra_args=["--name", "Kodi-Test"],
                    disable_discovery=False,
                    use_audio_backend=True,
                    capture_output=False,
                )
            args = popen.call_args[0][0]
            self.assertNotIn("--backend", args)


class StartBackoffTests(unittest.TestCase):
    """A receiver that cannot start must not be restarted in a tight loop."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.spotty = _FakeSpotty(self.temp.name)
        self.auth = spotty_auth_impl.SpottyAuth(self.spotty)

    def test_failure_is_retried_with_a_delay(self):
        with mock.patch.object(self.auth, "is_connect_process_running", return_value=False), \
                mock.patch.object(self.auth, "start_zeroconf_authenticate", return_value=None) as start:
            self.assertFalse(self.auth.ensure_device_connect_running())
            self.assertFalse(self.auth.ensure_device_connect_running())
            self.assertEqual(start.call_count, 1)

    def test_force_bypasses_the_delay(self):
        with mock.patch.object(self.auth, "is_connect_process_running", return_value=False), \
                mock.patch.object(self.auth, "start_zeroconf_authenticate", return_value=None) as start:
            self.assertFalse(self.auth.ensure_device_connect_running())
            self.assertFalse(self.auth.ensure_device_connect_running(force=True))
            self.assertEqual(start.call_count, 2)

    def test_a_running_receiver_is_reused(self):
        with mock.patch.object(self.auth, "is_connect_process_running", return_value=True), \
                mock.patch.object(self.auth, "start_zeroconf_authenticate") as start:
            self.assertTrue(self.auth.ensure_device_connect_running())
            start.assert_not_called()


class StaleReceiverCleanupTests(unittest.TestCase):
    """An orphaned receiver keeps port 10002 and blocks every new start."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.spotty = _FakeSpotty(self.temp.name)
        self.auth = spotty_auth_impl.SpottyAuth(self.spotty)

    def test_untracked_receiver_is_terminated(self):
        with mock.patch.object(self.auth, "_posix_connect_process_pids", return_value=[999]), \
                mock.patch.object(spotty_auth_impl, "terminate_process", return_value=True) as terminate:
            self.assertEqual(self.auth._cleanup_orphaned_posix_connect_processes(), 1)
            terminate.assert_called_once_with(999)

    def test_the_tracked_receiver_is_kept(self):
        with open(self.auth._connect_pid_file(), "w", encoding="utf-8") as file:
            file.write("1234")
        with mock.patch.object(self.auth, "_posix_connect_process_pids", return_value=[1234]), \
                mock.patch.object(spotty_auth_impl, "terminate_process", return_value=True) as terminate:
            self.assertEqual(self.auth._cleanup_orphaned_posix_connect_processes(), 0)
            terminate.assert_not_called()

    def test_unrelated_processes_are_never_touched(self):
        cache_dir = self.temp.name
        with mock.patch("os.listdir", return_value=["4242"]), \
                mock.patch("builtins.open", mock.mock_open(read_data=b"/usr/bin/other\x00--name\x00Kodi-Resonance\x00")):
            self.assertEqual(self.auth._posix_connect_process_pids(), [])
        self.assertIn(cache_dir, cache_dir)


class DiscoveryFallbackTests(unittest.TestCase):
    """The dedicated authenticate mode is retried as plain discovery mode."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.spotty = _FakeSpotty(self.temp.name)
        self.auth = spotty_auth_impl.SpottyAuth(self.spotty)

    def test_fallback_starts_the_receiver_without_the_authenticate_flag(self):
        self.spotty.process = _FakeProcess()
        result = self.auth._start_discovery_fallback_receiver(["--zeroconf-port", "10002"], "device-connect.log")
        self.assertIsNotNone(result)
        self.assertFalse(self.spotty.calls[0]["authenticate"])

    def test_fallback_is_skipped_for_the_legacy_binary(self):
        self.spotty.uses_native_device_connect_binary = lambda: False
        result = self.auth._start_discovery_fallback_receiver(["--zeroconf-port", "10002"], "device-connect.log")
        self.assertIsNone(result)
        self.assertEqual(self.spotty.calls, [])

    def test_a_failing_authenticate_mode_falls_back_to_discovery(self):
        self.spotty.process = _FakeProcess(returncode=1)
        with mock.patch.object(self.auth, "is_connect_process_running", return_value=False), \
                mock.patch.object(self.auth, "_acquire_connect_start_lock", return_value=True), \
                mock.patch.object(self.auth, "_release_connect_start_lock"), \
                mock.patch.object(self.auth, "_stop_previous_connect_process"), \
                mock.patch.object(self.auth, "_cleanup_orphaned_posix_connect_processes", return_value=0), \
                mock.patch.object(self.auth, "_cleanup_orphaned_windows_connect_processes", return_value=0):
            first = self.auth.start_zeroconf_authenticate(fresh=False)
        self.assertIsNone(first)
        self.assertEqual(len(self.spotty.calls), 2)
        self.assertTrue(self.spotty.calls[0]["authenticate"])
        self.assertFalse(self.spotty.calls[1]["authenticate"])


class ReceiverLogTests(unittest.TestCase):
    """The receiver log is the only source of the real failure reason."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.spotty = _FakeSpotty(self.temp.name)
        self.auth = spotty_auth_impl.SpottyAuth(self.spotty)

    def test_the_log_tail_is_reported(self):
        log_file = os.path.join(self.temp.name, "device-connect.log")
        with open(log_file, "w", encoding="utf-8") as file:
            file.write("Setting up the HTTP server failed: Address already in use\n")
        self.assertIn("Address already in use", self.auth._read_receiver_log(log_file))

    def test_a_missing_log_is_not_an_error(self):
        self.assertEqual(self.auth._read_receiver_log("/nonexistent/device-connect.log"), "")


if __name__ == "__main__":
    unittest.main()
