import importlib.util
import json
import os
import pathlib
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
    _kodi_stub(LOGDEBUG=0, LOGINFO=0, LOGERROR=0, LOGWARNING=0, log=lambda *a, **k: None),
)
sys.modules.setdefault("xbmcaddon", _kodi_stub())
sys.modules.setdefault("xbmcgui", _kodi_stub())
sys.modules.setdefault(
    "xbmcvfs",
    _kodi_stub(translatePath=lambda path: path),
)

PATH = pathlib.Path(__file__).parents[1] / "resources/lib/utils.py"
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "resources/lib"))
spec = importlib.util.spec_from_file_location("utils", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SpottyCredentialsTests(unittest.TestCase):
    def test_missing_credentials_file(self):
        with mock.patch.object(module, "RUNTIME_PATH", "/nonexistent/resonance-runtime"):
            self.assertFalse(module.spotty_has_credentials())

    def test_truncated_credentials_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "credentials.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write('{"username": "x"')
            with mock.patch.object(module, "RUNTIME_PATH", tmp):
                self.assertFalse(module.spotty_has_credentials())

    def test_partial_credentials_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "credentials.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"username": "somebody"}))
            with mock.patch.object(module, "RUNTIME_PATH", tmp):
                self.assertFalse(module.spotty_has_credentials())

    def test_valid_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "credentials.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"username": "somebody", "auth_data": "x" * 64}))
            with mock.patch.object(module, "RUNTIME_PATH", tmp):
                self.assertTrue(module.spotty_has_credentials())


if __name__ == "__main__":
    unittest.main()