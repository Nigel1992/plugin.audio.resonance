import os
import struct
import threading
import time
import zlib

import xbmc
import xbmcgui
import xbmcvfs

from deps.qrcodegen import QrCode


ACTION_PREVIOUS_MENU = 10
ACTION_NAV_BACK = 92


class QrDialogError(Exception):
    pass


def show_qr_dialog(
    url,
    title="Scan QR Code",
    message="Scan this QR code to sign in, or use the URL below manually on another device.",
    timeout_seconds=300,
    modal=True,
):
    """Generate and show a local QR-code popup for ``url``.

    The QR image is written to Kodi's temporary directory and removed after the
    dialog closes. No authentication URL or token-bearing value is logged.
    """
    text = "" if url is None else str(url)
    if not text:
        xbmcgui.Dialog().ok(title or "Scan QR Code", "No QR code could be shown because the link was empty.")
        return False

    try:
        path = _write_qr_png(text)
        dialog = _create_dialog(path, title or "Scan QR Code", message or "", text)
        handle = _QrDialogHandle(dialog, path, timeout_seconds)
        handle.start(modal=modal)
        if not modal:
            return handle
        dialog.doModal()
        handle.close(cleanup=True)
        return True
    except Exception:
        xbmcgui.Dialog().ok(title or "Scan QR Code", "The QR code could not be shown. Please use the link displayed on screen.")
        return False


class _QrDialogHandle:
    def __init__(self, dialog, path, timeout_seconds=300):
        self.dialog = dialog
        self.path = path
        self.timer = None
        self.timeout_seconds = timeout_seconds
        self.closed = False

    def start(self, modal=True):
        if self.timeout_seconds:
            self.timer = threading.Timer(max(1, int(self.timeout_seconds)), self.close)
            self.timer.daemon = True
            self.timer.start()
        if not modal:
            try:
                self.dialog.show()
            except AttributeError:
                self.dialog.doModal()
        return self

    def close(self, cleanup=True):
        if self.closed:
            return
        self.closed = True
        try:
            if self.timer:
                self.timer.cancel()
        except Exception:
            pass
        try:
            self.dialog.close()
        except Exception:
            pass
        if cleanup and self.path:
            try:
                xbmcvfs.delete(self.path)
            except Exception:
                pass


def _write_qr_png(text):
    try:
        qr = QrCode.encode_text(text, QrCode.Ecc.QUARTILE)
    except Exception as exc:
        raise QrDialogError("QR generation failed") from exc

    border = 4
    module_count = qr.get_size() + border * 2
    scale = max(4, min(18, 900 // module_count))
    size = module_count * scale

    rows = []
    white = b"\xff\xff\xff"
    black = b"\x00\x00\x00"
    for y in range(size):
        module_y = y // scale - border
        scanline = bytearray()
        for x in range(size):
            module_x = x // scale - border
            dark = 0 <= module_x < qr.get_size() and 0 <= module_y < qr.get_size() and qr.get_module(module_x, module_y)
            scanline += black if dark else white
        rows.append(b"\x00" + bytes(scanline))

    png = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
    png += _png_chunk(b"IEND", b"")

    temp_dir = xbmcvfs.translatePath("special://temp/")
    if not temp_dir:
        raise QrDialogError("Kodi temporary directory unavailable")
    try:
        os.makedirs(temp_dir, exist_ok=True)
    except OSError:
        pass
    path = os.path.join(temp_dir, "resonance-qr-{}.png".format(int(time.time() * 1000)))
    try:
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n" + png)
    except OSError as exc:
        raise QrDialogError("Could not write QR image") from exc
    return path


def _png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def _addon_path():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _create_dialog(image_path, title, message, link=""):
    try:
        return _QrCodeXmlDialog(
            "script-resonance-qr.xml",
            _addon_path(),
            "default",
            "720p",
            image_path=image_path,
            title=title,
            message=message,
            link=link,
        )
    except Exception:
        return _QrCodeDialog(image_path, title, message, link)


class _QrCodeXmlDialog(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        self.image_path = kwargs.pop("image_path", "")
        self.title = kwargs.pop("title", "")
        self.message = kwargs.pop("message", "")
        self.link = kwargs.pop("link", "")
        super().__init__(*args, **kwargs)

    def onInit(self):
        try:
            self.getControl(100).setLabel(self.title)
            self.getControl(200).setImage("")
            xbmc.sleep(50)
            self.getControl(200).setImage(self.image_path, False)
            self.getControl(300).setText(self.message)
            self.getControl(301).setText(self.link)
            self.setFocusId(400)
        except Exception:
            pass

    def onAction(self, action):
        if action.getId() in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self.close()

    def onClick(self, control_id):
        if control_id == 400:
            self.close()


class _QrCodeDialog(xbmcgui.WindowDialog):
    def __init__(self, image_path, title, message, link=""):
        super().__init__()
        self.image_path = image_path
        self.title = title
        self.message = message
        self.link = link
        self.close_button = None

    def onInit(self):
        width = self.getWidth() or 1920
        height = self.getHeight() or 1080
        dialog_w = min(int(width * 0.60), 760)
        dialog_h = min(int(height * 0.72), 760)
        left = int((width - dialog_w) / 2)
        top = int((height - dialog_h) / 2)
        qr_size = min(int(dialog_w * 0.42), int(dialog_h * 0.42), 420)
        qr_left = left + int((dialog_w - qr_size) / 2)
        qr_top = top + 112

        self.addControl(xbmcgui.ControlImage(left, top, dialog_w, dialog_h, "dialogs/dialog-bg.png", colorDiffuse="F214171C"))
        self.addControl(xbmcgui.ControlLabel(left + 40, top + 28, dialog_w - 80, 54, self.title, alignment=2, font="font16"))
        self.addControl(xbmcgui.ControlImage(qr_left, qr_top, qr_size, qr_size, self.image_path))
        self.addControl(xbmcgui.ControlLabel(left + 40, qr_top + qr_size + 24, dialog_w - 80, 84, self.message, alignment=2, font="font13"))
        self.addControl(xbmcgui.ControlLabel(left + 56, qr_top + qr_size + 88, dialog_w - 112, 64, self.link, alignment=2, font="font12"))
        self.close_button = xbmcgui.ControlButton(left + int((dialog_w - 260) / 2), top + dialog_h - 86, 260, 56, "Close")
        self.addControl(self.close_button)
        self.setFocus(self.close_button)

    def onAction(self, action):
        if action.getId() in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self.close()

    def onClick(self, control_id):
        if self.close_button and control_id == self.close_button.getId():
            self.close()
