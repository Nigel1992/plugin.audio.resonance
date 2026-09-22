import importlib.util
import pathlib
import sys
import tempfile
import types
import unittest


def _kodi_stub(**attrs):
    module = types.ModuleType("stub")
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


class FakeAction:
    def __init__(self, identity):
        self.identity = identity

    def getId(self):
        return self.identity


class FakeWindowDialog:
    def __init__(self):
        self.closed = False

    def getWidth(self):
        return 1920

    def getHeight(self):
        return 1080

    def addControl(self, control):
        pass

    def setFocus(self, control):
        pass

    def close(self):
        self.closed = True

    def doModal(self):
        pass

    def show(self):
        self.shown = True


class FakeWindowXmlDialog(FakeWindowDialog):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.args = args
        self.kwargs = kwargs


class FakeControl:
    def __init__(self, *args, **kwargs):
        self.identity = id(self)

    def getId(self):
        return self.identity


class FakeDialog:
    calls = []

    def ok(self, *args):
        self.calls.append(args)


sys.modules["xbmc"] = _kodi_stub(LOGDEBUG=0, log=lambda *a, **k: None)
sys.modules["xbmcgui"] = _kodi_stub(
    WindowDialog=FakeWindowDialog,
    WindowXMLDialog=FakeWindowXmlDialog,
    ControlImage=FakeControl,
    ControlLabel=FakeControl,
    ControlButton=FakeControl,
    Dialog=FakeDialog,
)
sys.modules["xbmcvfs"] = _kodi_stub()

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "resources/lib"))
PATH = ROOT / "resources/lib/qr_dialog.py"
spec = importlib.util.spec_from_file_location("qr_dialog", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class QrDialogTests(unittest.TestCase):
    def test_write_qr_png_uses_kodi_temp_and_writes_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            module.xbmcvfs.translatePath = lambda path: tmp if path == "special://temp/" else path
            path = module._write_qr_png("https://zip1.io/abc123")
            try:
                data = pathlib.Path(path).read_bytes()
            finally:
                pathlib.Path(path).unlink(missing_ok=True)
            self.assertTrue(path.startswith(tmp))
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertIn(b"IHDR", data[:32])

    def test_empty_url_fails_gracefully(self):
        FakeDialog.calls = []
        self.assertFalse(module.show_qr_dialog(""))
        self.assertTrue(FakeDialog.calls)

    def test_back_actions_close_dialog(self):
        dialog = module._QrCodeDialog("/tmp/qr.png", "Title", "Message")
        dialog.onAction(FakeAction(module.ACTION_NAV_BACK))
        self.assertTrue(dialog.closed)

    def test_prefers_xml_dialog(self):
        dialog = module._create_dialog("/tmp/qr.png", "Title", "Message", "https://zip1.io/example")
        self.assertIsInstance(dialog, module._QrCodeXmlDialog)
        self.assertEqual(dialog.image_path, "/tmp/qr.png")
        self.assertEqual(dialog.link, "https://zip1.io/example")

    def test_non_modal_returns_closable_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            module.xbmcvfs.translatePath = lambda path: tmp if path == "special://temp/" else path
            deleted = []
            module.xbmcvfs.delete = lambda path: deleted.append(path) or pathlib.Path(path).unlink(missing_ok=True)
            handle = module.show_qr_dialog("https://zip1.io/example", modal=False, timeout_seconds=0)
            self.assertTrue(hasattr(handle, "close"))
            self.assertTrue(pathlib.Path(handle.path).exists())
            handle.close()
            self.assertTrue(deleted)

    def test_qr_content_keeps_supplied_text(self):
        captured = []
        original = module.QrCode.encode_text
        module.QrCode.encode_text = lambda text, ecc: captured.append(text) or original(text, ecc)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                module.xbmcvfs.translatePath = lambda path: tmp if path == "special://temp/" else path
                path = module._write_qr_png(" https://zip1.io/exact ")
                pathlib.Path(path).unlink(missing_ok=True)
        finally:
            module.QrCode.encode_text = original
        self.assertEqual(captured, [" https://zip1.io/exact "])

    def test_plugin_progress_dialog_does_not_repeat_login_url(self):
        source = (ROOT / "plugin.py").read_text()
        self.assertNotIn('"Open this address in any browser', source)
        self.assertNotIn('progress.create("Resonance", "")', source)
        self.assertIn("modal=False", source)


if __name__ == "__main__":
    unittest.main()
