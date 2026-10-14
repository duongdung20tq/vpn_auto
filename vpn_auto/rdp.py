"""Retry an RDP (mstsc) connection to a host until the remote desktop actually appears.

The host often drops the first handshakes (packet loss) even when ping works, so Connect has to
be pressed many times. This engine just opens mstsc and waits for the real session window; on
failure (error dialog, mstsc closing itself, or a timeout) it closes mstsc and retries - no TCP
pre-check, since that would get stuck "waiting" on a host that only answers every few tries.

Password auto-fill: when a password is given, the engine writes a temporary .rdp file with the
password encrypted by Windows DPAPI (the standard "password 51:b:" field) and every resource
redirection requested (clipboard, drives, printers, smart cards, COM ports, audio, POS devices).
Windows still shows a one-time "security warning" per launch with its own on/off toggle for each
resource (they default to OFF regardless of the .rdp file), so the engine ticks every toggle on
before clicking Connect. The temp .rdp file is deleted right after launch.
A plain `cmdkey` credential is NOT enough here because the target forces a credential prompt.
"connected" means the remote desktop window is shown, not merely that TCP 3389 is established.
Only mstsc instances this engine launched are ever closed.
"""
import binascii
import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
import time

log = logging.getLogger(__name__)
NO_WINDOW = 0x08000000
BM_CLICK = 0x00F5
RDP_PORT = 3389

BM_GETCHECK = 0x00F0
BS_TYPE_MASK = 0x0F
BS_AUTOCHECKBOX = 0x03

try:
    import win32con
    import win32crypt
    import win32gui
    import win32process
    _HAVE_WIN32 = True
except ImportError:
    _HAVE_WIN32 = False


# --- network -----------------------------------------------------------------

def split_host_port(target, default=RDP_PORT):
    """'10.0.0.5' -> ('10.0.0.5', 3389); '10.0.0.5:3390' -> ('10.0.0.5', 3390)."""
    target = target.strip()
    if ":" in target and target.rsplit(":", 1)[1].isdigit():
        host, port = target.rsplit(":", 1)
        return host, int(port)
    return target, default


def reachable(ip, port=RDP_PORT, timeout=2.0):
    host, port = split_host_port(ip, port)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def established(ip, port=RDP_PORT):
    host, port = split_host_port(ip, port)
    out = subprocess.run(["netstat", "-n"], capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    return bool(re.search(rf"\b{re.escape(host)}:{port}\b\s+ESTABLISHED", out))


# --- credentials / .rdp file -------------------------------------------------

def _password_blob(password):
    data = win32crypt.CryptProtectData(password.encode("utf-16-le"), "psw", None, None, None, 0)
    return binascii.hexlify(data).decode().upper()


def build_rdp_file(ip, username, password):
    """Write a temp .rdp that logs in with the given password and redirects nothing."""
    lines = [
        f"full address:s:{ip}",
        f"username:s:{username}",
        f"password 51:b:{_password_blob(password)}",
        "prompt for credentials:i:0",
        "authentication level:i:0",
        "redirectclipboard:i:1",     # copy/paste giữa máy ngoài và máy trong
        "redirectprinters:i:1",      # dùng máy in của máy ngoài
        "redirectsmartcards:i:1",    # dùng smart card / token của máy ngoài
        "redirectcomports:i:1",      # cổng COM của máy ngoài
        "devicestoredirect:s:*",     # mọi thiết bị cắm thêm (USB...) của máy ngoài
        "drivestoredirect:s:*",      # mọi ổ đĩa của máy ngoài
        "redirectposdevices:i:1",    # máy quét mã vạch / thiết bị POS
        "audiomode:i:0",             # phát âm thanh của máy trong ra loa máy ngoài
        "audiocapturemode:i:1",      # dùng micro của máy ngoài trong phiên remote
    ]
    fd, path = tempfile.mkstemp(suffix=".rdp", prefix="rdpauto_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def clear_credential(ip):
    """Remove any stale cmdkey credential for this host (cleanup only)."""
    subprocess.run(["cmdkey", f"/delete:TERMSRV/{ip}"], capture_output=True, creationflags=NO_WINDOW)


# --- mstsc -------------------------------------------------------------------

def launch(ip, username="", password=""):
    """Start mstsc. Returns (proc, rdp_path); rdp_path is a temp file to delete, or None."""
    if password and username:
        path = build_rdp_file(ip, username, password)
        return subprocess.Popen(["mstsc.exe", path]), path
    return subprocess.Popen(["mstsc.exe", f"/v:{ip}"]), None


def kill(proc):
    if proc and proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)


def _tops(pid=None):
    out = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and (pid is None or win32process.GetWindowThreadProcessId(h)[1] == pid):
            out.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    return out


def _enable_resource_toggles(dialog):
    """Tick on every resource-permission toggle (Clipboard, Drives, Printers, ...).

    The dialog shows one BS_AUTOCHECKBOX per resource (Clipboard, Drives, Printers, Smart cards,
    Ports, Microphones, PnP devices, POS devices...); they default to OFF no matter what the .rdp
    file's redirect*:i:1 asked for, so the user has to approve each one here. Connect/Cancel are
    plain push buttons (a different BS_ type), so filtering by button style finds only the toggles
    without depending on their control IDs, which differ by Windows build.
    """
    acc = []
    win32gui.EnumChildWindows(dialog, lambda c, a: a.append(c) or True, acc)
    for c in acc:
        if win32gui.GetClassName(c) != "Button":
            continue
        style = win32gui.GetWindowLong(c, win32con.GWL_STYLE)
        if style & BS_TYPE_MASK != BS_AUTOCHECKBOX:
            continue
        if win32gui.SendMessage(c, BM_GETCHECK, 0, 0) == 0:  # BST_UNCHECKED
            win32gui.SendMessage(c, BM_CLICK, 0, 0)


def accept_security_warning(pid):
    """Approve every resource toggle, then click Connect on the unsigned-.rdp 'security warning'."""
    if not _HAVE_WIN32:
        return
    for h in _tops(pid):
        if win32gui.GetClassName(h) == "#32770" and "warning" in win32gui.GetWindowText(h).lower():
            _enable_resource_toggles(h)
            btn = win32gui.GetDlgItem(h, 1)  # Connect
            if btn:
                win32gui.SendMessage(btn, BM_CLICK, 0, 0)


_ERROR_TEXT = re.compile(r"can.?t connect|cannot connect|could not|unable|not be found|"
                         r"an internal error|not working|remote session was disconnected|"
                         r"không thể|lỗi|không tìm thấy", re.IGNORECASE)


def error_dialog(pid):
    """A mstsc 'can't connect' error message box (not the security warning) owned by this mstsc."""
    if not _HAVE_WIN32:
        return False
    for h in _tops(pid):
        if win32gui.GetClassName(h) != "#32770":
            continue
        title = win32gui.GetWindowText(h)
        if "warning" in title.lower():
            continue
        texts = []
        win32gui.EnumChildWindows(h, lambda c, a: a.append(win32gui.GetWindowText(c)) or True, texts)
        if _ERROR_TEXT.search(" ".join(texts)):
            return True
    return False


def desktop_shown(pid):
    """True once the real remote session window (TscShellContainerClass) is displayed."""
    if not _HAVE_WIN32:
        return False
    for h in _tops(pid):
        if win32gui.GetClassName(h) == "TscShellContainerClass":
            left, top, right, bottom = win32gui.GetWindowRect(h)
            if right - left > 300 and bottom - top > 200:
                return True
    return False


def credential_prompt_present():
    """A Windows Security / credential prompt is up (password was rejected or not supplied)."""
    if not _HAVE_WIN32:
        return False
    for h in _tops():
        if "Credential" in win32gui.GetClassName(h) or win32gui.GetWindowText(h) == "Windows Security":
            return True
    return False


class RdpConnector:
    """Retry an RDP connection in a background thread until the desktop appears or stopped."""

    STATES = ("idle", "connecting", "connected", "ready", "stopped", "error")

    def __init__(self, ip, username="", password="", *, probe_timeout=2.0, probe_interval=3.0,
                 connect_timeout=25.0, retry_delay=3.0, poll_interval=1.0, on_update=None):
        self.ip = ip.strip()
        self.username = username.strip()
        self.password = password
        self.probe_timeout = probe_timeout
        self.probe_interval = probe_interval
        self.connect_timeout = connect_timeout
        self.retry_delay = retry_delay
        self.poll_interval = poll_interval
        self.on_update = on_update or (lambda state, detail, attempt: None)
        self.state = "idle"
        self.attempts = 0
        self._stop = threading.Event()
        self._thread = None
        self._proc = None

    def _set(self, state, detail=""):
        self.state = state
        log.info("RDP %s: %s %s", self.ip, state, detail)
        self.on_update(state, detail, self.attempts)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"rdp-{self.ip}", daemon=True)
        self._thread.start()

    def stop(self, close_session=False):
        self._stop.set()
        if close_session:
            kill(self._proc)

    def _run(self):
        if not self.ip:
            self._set("error", "Chưa nhập IP")
            return
        # No TCP pre-gate: a lossy host (packet loss on 3389) drops quick probes even though
        # mstsc connects after a few tries. So we just launch mstsc and retry, like pressing
        # Connect repeatedly, until the desktop (or, without a password, the login prompt) shows.
        try:
            while not self._stop.is_set():
                self.attempts += 1
                self._set("connecting", f"Đang kết nối RDP (lần {self.attempts})...")
                self._proc, rdp_path = launch(self.ip, self.username, self.password)
                result = self._wait_connected()
                _safe_remove(rdp_path)
                if result == "connected":
                    self._set("connected", f"Đã vào desktop {self.ip} sau {self.attempts} lần")
                    return
                if result == "ready":
                    self._set("ready", f"Đã hiện ô nhập mật khẩu sau {self.attempts} lần - mời bạn nhập")
                    return
                if result == "wrong-cred":
                    kill(self._proc)
                    self._set("error", "Bị hỏi lại mật khẩu - kiểm tra tài khoản/mật khẩu")
                    return
                kill(self._proc)
                self._proc = None
                self._set("connecting", f"Lần {self.attempts} chưa được, thử lại...")
                self._stop.wait(self.retry_delay)
            self._set("stopped", "Đã dừng")
        except Exception as exc:
            log.exception("RDP loop failed")
            self._set("error", str(exc))

    def _wait_connected(self):
        """One mstsc launch: 'connected' (desktop), 'ready' (login prompt, no password), 'wrong-cred',
        'failed' (error/closed), or 'timeout'."""
        deadline = time.time() + self.connect_timeout
        while time.time() < deadline and not self._stop.is_set():
            accept_security_warning(self._proc.pid)
            if desktop_shown(self._proc.pid):
                return "connected"
            if credential_prompt_present():
                return "wrong-cred" if self.password else "ready"
            if error_dialog(self._proc.pid):
                return "failed"
            if self._proc.poll() is not None:
                return "failed"
            time.sleep(self.poll_interval)
        return "connected" if desktop_shown(self._proc.pid) else "timeout"


def _safe_remove(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass
