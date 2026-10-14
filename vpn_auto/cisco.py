"""Adapter over Cisco Secure Client 5.x: vpncli for connect/disconnect/state, Win32 for the token PIN.

The official non-interactive path (`vpncli -s`) drives the connect; the FPT-CA
"Xác nhận PIN" dialog is a normal Win32 #32770 the watchdog fills. The desktop UI
(csc_ui.exe) holds the connect capability, so it is closed before a CLI connect.
Verified live against btcvpn.mof.gov.vn, tunnel group Cert-kyso (2026-10-09).
"""
import ctypes
import enum
import os
import re
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

from . import winui

_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_crypt32.CertOpenSystemStoreW.restype = ctypes.c_void_p
_crypt32.CertOpenSystemStoreW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_crypt32.CertEnumCertificatesInStore.restype = ctypes.c_void_p
_crypt32.CertEnumCertificatesInStore.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_crypt32.CertGetNameStringW.restype = wintypes.DWORD
_crypt32.CertGetNameStringW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
_crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_CERT_NAME_SIMPLE_DISPLAY_TYPE = 4
_CERT_NAME_ISSUER_FLAG = 0x1
TOKEN_ISSUER = "FPT Certification Authority"


def token_cert_present(issuer_substr=TOKEN_ISSUER):
    """True when a certificate issued by FPT-CA is in the user store (i.e. the token is plugged in)."""
    store = _crypt32.CertOpenSystemStoreW(None, "MY")
    if not store:
        return False
    try:
        ctx = _crypt32.CertEnumCertificatesInStore(store, None)
        while ctx:
            size = _crypt32.CertGetNameStringW(ctx, _CERT_NAME_SIMPLE_DISPLAY_TYPE, _CERT_NAME_ISSUER_FLAG,
                                               None, None, 0)
            buf = ctypes.create_unicode_buffer(size)
            _crypt32.CertGetNameStringW(ctx, _CERT_NAME_SIMPLE_DISPLAY_TYPE, _CERT_NAME_ISSUER_FLAG,
                                        None, buf, size)
            if issuer_substr.lower() in buf.value.lower():
                return True
            ctx = _crypt32.CertEnumCertificatesInStore(store, ctx)
        return False
    finally:
        _crypt32.CertCloseStore(store, 0)

CISCO_DIR = r"C:\Program Files (x86)\Cisco\Cisco Secure Client"
CISCO_CLI = CISCO_DIR + r"\vpncli.exe"
CISCO_UI = CISCO_DIR + r"\UI\csc_ui.exe"
UI_IMAGE = "csc_ui.exe"

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

# FPT-CA token PIN dialog (same component as Pulse, Vietnamese title here).
PIN_DIALOG_CLASS = "#32770"
ID_PIN_EDIT, ID_PIN_LOGIN, ID_PIN_CANCEL = 1000, 2000, 2001
LOGIN_LABELS = ("đăng nhập", "login")

TOKEN_ERROR = re.compile(r"pin is wrong|has been blocked|token has been removed|"
                         r"mã pin sai|đã bị khóa|đã được rút ra", re.IGNORECASE)
AUTH_FAILURE = re.compile(r"login failed|authentication failed|invalid (username|password|credential)|"
                          r"logon denied|access denied", re.IGNORECASE)
NO_CERT = re.compile(r"no valid certificates|no client certificate|certificate validation failure", re.IGNORECASE)
CAPABILITY_BUSY = re.compile(r"capability is unavailable|another cisco secure client|acquired it", re.IGNORECASE)
CONNECT_FAILURE = re.compile(r"\bconnect(ion)? (attempt has )?(timed out|failed)|unable to (establish|contact)|"
                             r"no valid certificates|certificate validation failure|the vpn connection failed",
                             re.IGNORECASE)
NO_CERT_HINT = "Không thấy chứng thư token — kiểm tra token FPT-CA đã cắm chưa"


class State(enum.Enum):
    CONNECTED = "connected"
    CONNECTING = "connecting"
    DISCONNECTING = "disconnecting"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


_STATE_WORDS = {
    "connected": State.CONNECTED,
    "connecting": State.CONNECTING,
    "authenticating": State.CONNECTING,
    "reconnecting": State.CONNECTING,
    "ssopollingstarted": State.CONNECTING,
    "disconnecting": State.DISCONNECTING,
    "disconnected": State.DISCONNECTED,
    "unknown": State.UNKNOWN,
}


@dataclass(frozen=True)
class ConnectResult:
    ok: bool
    rejected: bool  # credentials/token refused -> do not retry blindly
    detail: str


@dataclass(frozen=True)
class Status:
    name: str
    server: str
    state: State
    detail: str


def parse_state(output):
    """Last '>> state: X' in vpncli output is the VPN tunnel state."""
    matches = re.findall(r">>\s*state:\s*([A-Za-z ]+)", output)
    if not matches:
        return State.UNKNOWN
    word = matches[-1].strip().lower().replace(" ", "")
    return _STATE_WORDS.get(word, State.UNKNOWN)


def is_pin_dialog(hwnd):
    if winui.class_name(hwnd) != PIN_DIALOG_CLASS:
        return False
    edit = winui.child_by_id(hwnd, ID_PIN_EDIT)
    login = winui.child_by_id(hwnd, ID_PIN_LOGIN)
    if not (edit and login and winui.child_by_id(hwnd, ID_PIN_CANCEL)):
        return False
    if winui.class_name(edit).lower() != "edit":
        return False
    return winui.safe_text(login).replace("&", "").strip().lower() in LOGIN_LABELS


class Cisco:
    def __init__(self, server, group="", username="", password="", cli=CISCO_CLI):
        self.server = server
        self.group = group
        self.username = username
        self.password = password
        self.cli = cli
        self._worker = None
        self._result = None
        self._phase = ""

    def update(self, server, group, username, password):
        self.server, self.group, self.username, self.password = server, group, username, password

    # --- UI that holds the connect capability -----------------------------

    def token_present(self):
        return token_cert_present()

    def ui_running(self):
        return bool(winui.process_ids(UI_IMAGE))

    def close_ui(self):
        """Close csc_ui and wait for it to release the connect capability before vpncli connects."""
        if not self.ui_running():
            return
        subprocess.run(["taskkill", "/F", "/IM", UI_IMAGE], capture_output=True, creationflags=NO_WINDOW)
        for _ in range(30):
            if not self.ui_running():
                break
            time.sleep(0.2)
        time.sleep(1.0)  # grace for vpnagent to hand the capability back

    def launch_ui(self, minimized=True):
        args = [CISCO_UI] + (["-minimized"] if minimized else [])
        subprocess.Popen(args, creationflags=NO_WINDOW)

    # --- state ------------------------------------------------------------

    def _run(self, args, timeout=30):
        try:
            out = subprocess.run([self.cli, *args], stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=timeout, creationflags=NO_WINDOW)
            return out.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired as exc:
            return (exc.stdout or b"").decode("utf-8", "replace")

    def state(self):
        return parse_state(self._run(["state"]))

    def status(self):
        return Status(self.server, self.server, self.state(), "")

    def disconnect(self):
        self._run(["disconnect"])

    # --- PIN dialog -------------------------------------------------------

    def find_pin_dialog(self):
        for hwnd in winui.top_windows_all():
            if winui.is_visible(hwnd) and is_pin_dialog(hwnd):
                return hwnd
        return 0

    def token_errors(self):
        errors = []
        for hwnd in winui.top_windows_all():
            if winui.class_name(hwnd) == PIN_DIALOG_CLASS and winui.is_visible(hwnd) and not is_pin_dialog(hwnd):
                texts = " | ".join(t for t in (winui.safe_text(c) for c in winui.children(hwnd)) if t)
                if TOKEN_ERROR.search(texts):
                    errors.append(texts)
        return errors

    def fill_pin(self, dialog, pin):
        edit = winui.child_by_id(dialog, ID_PIN_EDIT)
        login = winui.child_by_id(dialog, ID_PIN_LOGIN)
        winui.type_text(edit, pin)
        time.sleep(0.2)
        if not winui.is_enabled(login):
            return False
        winui.press_dialog_button(dialog, ID_PIN_LOGIN)
        return True

    # --- connect worker ---------------------------------------------------

    @property
    def phase(self):
        return self._phase

    def connecting(self):
        return self._worker is not None and self._worker.is_alive()

    def take_result(self):
        result, self._result = self._result, None
        return result

    def start_connect(self):
        if self.connecting():
            return
        self._result = None
        self._phase = "Bắt đầu kết nối"
        self.close_ui()
        self._worker = threading.Thread(target=self._connect_worker, name="cisco-connect", daemon=True)
        self._worker.start()

    def _connect_worker(self):
        proc = subprocess.Popen([self.cli, "-s"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        acc = []
        lock = threading.Lock()

        def reader():
            buf = b""
            for ch in iter(lambda: proc.stdout.read(1), b""):
                buf += ch
                if ch in (b"\n", b":"):
                    with lock:
                        acc.append(buf.decode("utf-8", "replace"))
                    buf = b""

        threading.Thread(target=reader, daemon=True).start()
        proc.stdin.write(f"connect {self.server}\n".encode())
        proc.stdin.flush()

        sent = set()
        deadline = time.time() + 180
        result = None
        while time.time() < deadline and proc.poll() is None:
            with lock:
                whole = "".join(acc)
            tail = whole[-400:]
            self._phase = self._phase_from(tail)
            if "group" not in sent and re.search(r"Group:\s*$", tail):
                groups = {name.lower(): idx for idx, name in re.findall(r"(\d+)\)\s*(\S+)", whole)}
                idx = groups.get(self.group.lower(), "0")
                self._feed(proc, f"{idx}\n")
                sent.add("group")
            elif "user" not in sent and re.search(r"Username:\s*$", tail):
                self._feed(proc, f"{self.username}\n")
                sent.add("user")
            elif "pw" not in sent and re.search(r"Password:\s*$", tail):
                self._feed(proc, f"{self.password}\n")
                sent.add("pw")
            elif "banner" not in sent and re.search(r"accept\?\s*\[y/n\]|respond to banner", tail, re.I):
                self._feed(proc, "y\n")
                sent.add("banner")
            if re.search(r">>\s*state:\s*Connected|Connected to", whole):
                result = ConnectResult(True, False, "Đã kết nối")
                break
            if AUTH_FAILURE.search(tail):
                result = ConnectResult(False, True, "Sai tài khoản hoặc mật khẩu")
                break
            if CAPABILITY_BUSY.search(tail):
                result = ConnectResult(False, False, "Cisco đang bận (ứng dụng khác giữ kết nối) — thử lại")
                break
            if CONNECT_FAILURE.search(tail):
                detail = NO_CERT_HINT if NO_CERT.search(tail) else (_last_notice(whole) or "Kết nối thất bại")
                result = ConnectResult(False, False, detail)
                break
            time.sleep(0.4)
        if result is None:
            result = ConnectResult(False, False, "Hết thời gian chờ kết nối")
        with lock:
            final = "".join(acc)
        self._dump_debug(final)
        self._close_cli(proc)
        self._result = result

    @staticmethod
    def _dump_debug(transcript):
        path = os.environ.get("VPN_AUTO_DEBUG")
        if path:
            try:
                open(path, "w", encoding="utf-8", errors="replace").write(transcript)
            except OSError:
                pass

    @staticmethod
    def _feed(proc, text):
        try:
            proc.stdin.write(text.encode())
            proc.stdin.flush()
        except OSError:
            pass

    @staticmethod
    def _close_cli(proc):
        """End the vpncli session without dropping the tunnel (the agent service keeps it)."""
        try:
            proc.stdin.write(b"quit\n")
            proc.stdin.flush()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    @staticmethod
    def _phase_from(tail):
        if re.search(r"Posture Assessment", tail):
            return "Đang kiểm tra máy (Posture)"
        if re.search(r"Hostscan", tail):
            return "Đang quét hệ thống (Hostscan)"
        if re.search(r"Group:", tail):
            return "Đang chọn nhóm kết nối"
        if re.search(r"Username:|Password:", tail):
            return "Đang đăng nhập"
        if re.search(r"banner", tail, re.I):
            return "Đang xác nhận banner"
        if re.search(r"Establishing VPN", tail):
            return "Đang thiết lập phiên VPN"
        return "Đang kết nối"


def _last_notice(output):
    notices = re.findall(r">>\s*(?:notice|error):\s*(.+)", output)
    return notices[-1].strip() if notices else ""
