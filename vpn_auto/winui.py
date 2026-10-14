"""Win32 helpers that drive another process's controls without moving the mouse.

Every message that expects a reply goes through SendMessageTimeout so a hung
target window can never freeze the watchdog.
"""
import ctypes
from ctypes import wintypes

import pywintypes
import win32api
import win32con
import win32gui
import win32process

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_SendMessageTimeoutW = _user32.SendMessageTimeoutW
_SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]
_SendMessageTimeoutW.restype = wintypes.LPARAM

SMTO_ABORTIFHUNG = 0x0002
SEND_TIMEOUT_MS = 2000
ES_PASSWORD = 0x0020
BST_CHECKED = 1


class WindowUnavailable(Exception):
    """The target window vanished or did not answer in time."""


def send(hwnd, msg, wparam=0, lparam=0):
    result = ctypes.c_size_t()
    ok = _SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG,
                              SEND_TIMEOUT_MS, ctypes.byref(result))
    if not ok:
        raise WindowUnavailable(f"hwnd {hwnd} did not answer message {msg:#x}")
    return result.value


def post(hwnd, msg, wparam=0, lparam=0):
    try:
        win32gui.PostMessage(hwnd, msg, wparam, lparam)
    except pywintypes.error as exc:
        raise WindowUnavailable(f"cannot post to hwnd {hwnd}: {exc}") from exc


def find_window(cls, title):
    try:
        return win32gui.FindWindow(cls, title) or 0
    except pywintypes.error:
        return 0


def exists(hwnd):
    return bool(hwnd) and bool(win32gui.IsWindow(hwnd))


def is_visible(hwnd):
    return exists(hwnd) and bool(win32gui.IsWindowVisible(hwnd))


def is_enabled(hwnd):
    return exists(hwnd) and bool(win32gui.IsWindowEnabled(hwnd))


def class_name(hwnd):
    try:
        return win32gui.GetClassName(hwnd)
    except pywintypes.error:
        return ""


def ctrl_id(hwnd):
    try:
        return win32gui.GetDlgCtrlID(hwnd)
    except pywintypes.error:
        return 0


def style(hwnd):
    try:
        return win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    except pywintypes.error:
        return 0


def pid_of(hwnd):
    try:
        return win32process.GetWindowThreadProcessId(hwnd)[1]
    except pywintypes.error:
        return 0


def text(hwnd):
    length = send(hwnd, win32con.WM_GETTEXTLENGTH)
    buf = ctypes.create_unicode_buffer(length + 1)
    send(hwnd, win32con.WM_GETTEXT, length + 1, ctypes.addressof(buf))
    return buf.value


def safe_text(hwnd):
    """Control text for logs: masked for password fields, empty on failure."""
    if style(hwnd) & ES_PASSWORD:
        return "<hidden>"
    try:
        return text(hwnd)
    except WindowUnavailable:
        return ""


def children(hwnd):
    found = []
    try:
        win32gui.EnumChildWindows(hwnd, lambda h, acc: acc.append(h) or True, found)
    except pywintypes.error:
        pass  # parent destroyed mid-enumeration
    return found


def child_by_id(hwnd, control_id):
    for child in children(hwnd):
        if ctrl_id(child) == control_id:
            return child
    return 0


def top_windows(pid):
    found = []

    def collect(h, _):
        if pid_of(h) == pid:
            found.append(h)
        return True

    win32gui.EnumWindows(collect, None)
    return found


def top_windows_all():
    found = []
    win32gui.EnumWindows(lambda h, _: found.append(h) or True, None)
    return found


def click(hwnd):
    """Post a left click at the centre of a control (works while its window is hidden)."""
    try:
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
    except pywintypes.error as exc:
        raise WindowUnavailable(str(exc)) from exc
    point = win32api.MAKELONG((right - left) // 2, (bottom - top) // 2)
    post(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, point)
    post(hwnd, win32con.WM_LBUTTONUP, 0, point)


def press_dialog_button(dialog, control_id):
    """Notify a dialog that one of its buttons was clicked, even if the dialog is not active."""
    button = child_by_id(dialog, control_id)
    post(dialog, win32con.WM_COMMAND, win32api.MAKELONG(control_id, win32con.BN_CLICKED), button)


def type_text(edit, value):
    """Replace an edit's content with keystrokes; some dialogs ignore WM_SETTEXT."""
    empty = ctypes.create_unicode_buffer("")
    send(edit, win32con.WM_SETTEXT, 0, ctypes.addressof(empty))
    for char in value:
        send(edit, win32con.WM_CHAR, ord(char), 1)


def is_checked(hwnd):
    return send(hwnd, win32con.BM_GETCHECK) == BST_CHECKED


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
    ]


_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_INVALID_HANDLE = wintypes.HANDLE(-1).value


def _session_of(pid):
    session = wintypes.DWORD()
    if _kernel32.ProcessIdToSessionId(pid, ctypes.byref(session)):
        return session.value
    return None


def process_ids(image_name):
    """PIDs of processes with this image name in the current logon session."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot == _INVALID_HANDLE:
        return []
    my_session = _session_of(win32api.GetCurrentProcessId())
    pids = []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        more = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() == image_name.lower() and _session_of(entry.th32ProcessID) == my_session:
                pids.append(entry.th32ProcessID)
            more = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return pids
