"""Stall and pairing watchdogs, checked without Kodi's interpreter."""
import ast
import os
import pathlib
import sys
import tempfile
import time
import types
import unittest

ROOT = pathlib.Path(__file__).parents[1]


def _kodi_stub(**attrs):
    module = types.ModuleType("kodi_stub")
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


class _Dialog:
    def notification(self, *args, **kwargs):
        pass


sys.modules.setdefault("xbmc", _kodi_stub(LOGDEBUG=0, LOGINFO=1, LOGWARNING=2, LOGERROR=3, log=lambda *a, **k: None))
sys.modules.setdefault("xbmcgui", _kodi_stub(Dialog=_Dialog, NOTIFICATION_WARNING="warning"))
sys.path.insert(0, str(ROOT / "resources" / "lib"))

import connect_bridge  # noqa: E402
connect_bridge.xbmc = _kodi_stub(LOGDEBUG=0, LOGINFO=1, LOGWARNING=2, LOGERROR=3, log=lambda *a, **k: None)
_props = {}
connect_bridge.xbmcgui = _kodi_stub(
    Dialog=_Dialog,
    NOTIFICATION_WARNING="warning",
    Window=lambda *a, **k: types.SimpleNamespace(getProperty=lambda key: _props.get(key, "")),
)


def _service_functions(*names):
    """Compile the named top-level functions of service.py without importing it."""
    source = ROOT / "service.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"os": os}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return [namespace[name] for name in names]


_file_size, _new_pairing_failures = _service_functions("_file_size", "_new_pairing_failures")


class _FakeProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


class _FakePlayer:
    def isPlaying(self):
        return False


class ConnectStallTests(unittest.TestCase):
    def bridge(self):
        spotty = types.SimpleNamespace(supports_connect_bridge=lambda: True)
        bridge = connect_bridge.ConnectBridge(spotty, None, _FakePlayer(), tempfile.mkdtemp())
        bridge.active = True
        bridge.remote_state = "playing"
        bridge.process = _FakeProcess()
        return bridge

    def test_stall_detected_only_while_playing_without_audio(self):
        bridge = self.bridge()
        bridge.last_audio = time.monotonic() - (connect_bridge.CONNECT_STALL_SECONDS + 5)
        self.assertTrue(bridge._stalled())
        bridge.last_audio = time.monotonic()
        self.assertFalse(bridge._stalled())

    def test_paused_or_switching_is_not_a_stall(self):
        bridge = self.bridge()
        bridge.last_audio = time.monotonic() - (connect_bridge.CONNECT_STALL_SECONDS + 5)
        bridge.remote_state = "paused"
        self.assertFalse(bridge._stalled())
        bridge.remote_state = "playing"
        bridge.switching = True
        self.assertFalse(bridge._stalled())

    def test_pairing_during_setup_does_not_start_playback(self):
        bridge = self.bridge()
        bridge.enabled = True
        bridge.active = False
        bridge.remote_state = "playing"
        bridge.metadata = {"uri": "spotify:track:1"}
        bridge.uri = "spotify:track:1"
        started, commands = [], []
        bridge._play = lambda: started.append(True)
        bridge.command = lambda name, **values: commands.append(name)
        _props["resonance.setup-active"] = "1"
        try:
            bridge._tick()
        finally:
            _props.pop("resonance.setup-active", None)
        self.assertEqual(started, [])
        self.assertEqual(commands, ["pause"])
        self.assertEqual(bridge.remote_state, "paused")

    def test_transferred_paused_track_does_not_open_the_stream(self):
        bridge = self.bridge()
        bridge.enabled = True
        bridge.active = False
        bridge.remote_state = "paused"
        bridge.metadata = {"uri": "spotify:track:1"}
        bridge.uri = "spotify:track:1"
        bridge.restart = True
        started = []
        bridge._play = lambda: started.append(True)
        bridge._tick()
        self.assertEqual(started, [])
        bridge.remote_state = "playing"
        bridge._tick()
        self.assertEqual(started, [True])

    def test_restart_replaces_the_stalled_process(self):
        bridge = self.bridge()
        process = bridge.process
        bridge._restart_stalled_receiver()
        self.assertTrue(process.terminated)
        self.assertFalse(bridge.active)
        self.assertEqual(bridge.next_start, 0)
        self.assertEqual(bridge.stall_restarts, 1)


class PairingWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "device-connect.log")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, text):
        with open(self.path, "ab") as stream:
            stream.write(text.encode())

    def test_counts_only_rejections_after_offset(self):
        self.write("old MAC mismatch\n")
        offset = _file_size(self.path)
        self.write("WARN MAC mismatch\nWARN MAC mismatch\n")
        failures, offset = _new_pairing_failures(self.path, offset)
        self.assertEqual(failures, 2)
        failures, _ = _new_pairing_failures(self.path, offset)
        self.assertEqual(failures, 0)

    def test_truncated_log_is_read_from_the_start(self):
        self.write("x" * 500)
        failures, offset = _new_pairing_failures(self.path, 0)
        with open(self.path, "wb") as stream:
            stream.write(b"MAC mismatch\n")
        failures, offset = _new_pairing_failures(self.path, offset)
        self.assertEqual(failures, 1)

    def test_missing_log_counts_nothing(self):
        self.assertEqual(_new_pairing_failures(os.path.join(self.temp.name, "missing.log"), 7), (0, 7))


if __name__ == "__main__":
    unittest.main()
