import os
import signal
from typing import Iterable


WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WINDOWS_PROCESS_TERMINATE = 0x0001
WINDOWS_STILL_ACTIVE = 259


def _expected_process_names(names: Iterable[str]) -> set:
    return {
        os.path.basename(name).lower()
        for name in (names or ())
        if name
    }


def _windows_process_state(pid: int, access: int):
    """Return (kernel32, handle, active, image name) for one Windows PID."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        access | WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not handle:
        return kernel32, None, False, ""

    exit_code = wintypes.DWORD()
    active = bool(
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        and exit_code.value == WINDOWS_STILL_ACTIVE
    )

    image_buffer = ctypes.create_unicode_buffer(32768)
    image_length = wintypes.DWORD(len(image_buffer))
    image_name = ""
    if kernel32.QueryFullProcessImageNameW(
        handle,
        0,
        image_buffer,
        ctypes.byref(image_length),
    ):
        image_name = os.path.basename(image_buffer.value).lower()

    return kernel32, handle, active, image_name


def is_process_running(pid: int, expected_names: Iterable[str] = ()) -> bool:
    """Check a PID without sending it a signal on Windows."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    expected = _expected_process_names(expected_names)

    if os.name == "nt":
        kernel32 = None
        handle = None
        try:
            kernel32, handle, active, image_name = _windows_process_state(pid, 0)
            return active and (not expected or image_name in expected)
        except Exception:
            return False
        finally:
            if kernel32 is not None and handle:
                kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, SystemError):
        return False


def terminate_process(pid: int, expected_names: Iterable[str] = ()) -> bool:
    """Terminate only the expected process represented by a stored PID."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    expected = _expected_process_names(expected_names)

    if os.name == "nt":
        kernel32 = None
        handle = None
        try:
            import ctypes
            from ctypes import wintypes

            kernel32, handle, active, image_name = _windows_process_state(
                pid,
                WINDOWS_PROCESS_TERMINATE,
            )
            if not active or (expected and image_name not in expected):
                return False
            kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateProcess.restype = wintypes.BOOL
            return bool(kernel32.TerminateProcess(handle, 1))
        except Exception:
            return False
        finally:
            if kernel32 is not None and handle:
                kernel32.CloseHandle(handle)

    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, SystemError):
        return False

