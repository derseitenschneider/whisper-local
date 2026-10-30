# platform/windows/foreground.py
# Identifies the focused window's executable and title, which per-app rules use
# to decide delivery and formatting behaviour.
# macOS mirror: platform/macos/foreground.py.
import ctypes
import ctypes.wintypes as wt
import logging
import os

logger = logging.getLogger(__name__)

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_psapi = ctypes.windll.psapi

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010


def get_foreground_app() -> dict:
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return {}

        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return {}

        handle = _kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid.value
        )
        if not handle:
            return {}

        try:
            buf = ctypes.create_unicode_buffer(520)
            _psapi.GetModuleFileNameExW(handle, None, buf, 520)
            exe_path = buf.value
        finally:
            _kernel32.CloseHandle(handle)

        title_len = _user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(title_len + 1)
        _user32.GetWindowTextW(hwnd, title_buf, title_len + 1)

        return {
            'exe': os.path.basename(exe_path).lower() if exe_path else '',
            'path': exe_path,
            'title': title_buf.value,
        }
    except Exception as e:
        logger.debug(f"Foreground app probe failed: {e}")
        return {}


# --- Focused text input detection -------------------------------------------

# Shell surfaces (desktop, taskbar, Start, lock screen) where a paste lands nowhere.
_NON_TEXT_EXES = {'progman.exe', 'workerw.exe', 'dwm.exe', 'searchhost.exe',
                  'shellexperiencehost.exe', 'startmenuexperiencehost.exe',
                  'lockapp.exe', 'sihost.exe'}


# Mirrors the macOS three-way answer (True / False / None = can't tell).
# Windows only knows the foreground app, so any regular app counts as True.
def focused_text_input():
    try:
        exe = (get_foreground_app() or {}).get('exe', '').lower()
    except Exception:
        return None
    if not exe:
        return False
    return exe not in _NON_TEXT_EXES
