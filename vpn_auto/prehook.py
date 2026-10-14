"""Before each connect attempt: log off other Windows sessions, then turn Remote Desktop off.

Replaces the old logout_all.bat: every session listed by `quser` is logged off
except Administrator and the account running the tool. Both steps need the app
to run as Administrator; failures are logged but never stop the connect.
"""
import logging
import os
import subprocess

log = logging.getLogger(__name__)
NO_WINDOW = 0x08000000


def _run(args, timeout=30.0):
    """(returncode, output) of a console tool, without flashing a window."""
    result = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    out = (result.stdout or b"") + (result.stderr or b"")
    return result.returncode, out.decode("oem", "replace")


def parse_sessions(output):
    """[(username, session id)] from `quser` output; the header line is skipped."""
    sessions = []
    for line in output.splitlines()[1:]:
        tokens = line.replace(">", " ").split()
        if len(tokens) < 3:
            continue
        # SESSIONNAME is empty for disconnected sessions, so the ID is token 2 or 3.
        sid = tokens[1] if tokens[1].isdigit() else tokens[2]
        if sid.isdigit():
            sessions.append((tokens[0], int(sid)))
    return sessions


def default_keep():
    return {"administrator", os.environ.get("USERNAME", "").lower()} - {""}


def logoff_others(keep=None, run=_run):
    """Log off every session whose user is not in `keep` (case-insensitive). Returns the count."""
    keep = {name.lower() for name in (keep or default_keep())}
    try:
        _, out = run(["quser"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Không đọc được danh sách phiên (quser): %s", exc)
        return 0
    count = 0
    for user, sid in parse_sessions(out):
        if user.lower() in keep:
            continue
        try:
            code, msg = run(["logoff", str(sid)])
        except (OSError, subprocess.TimeoutExpired) as exc:
            code, msg = -1, str(exc)
        if code == 0:
            count += 1
            log.info("Đã đăng xuất phiên %s (ID=%d)", user, sid)
        else:
            log.warning("Không đăng xuất được %s (ID=%d, cần quyền Administrator?): %s", user, sid, msg.strip())
    return count


# --- Remote Desktop off while connecting -----------------------------------
# After the other sessions are logged off, new RDP connections are refused
# (fDenyTSConnections=1) until the VPN is connected, then allowed again.
# Writing HKLM needs the app to run as Administrator; failures are logged only.

RDP_KEY = r"SYSTEM\CurrentControlSet\Control\Terminal Server"
RDP_VALUE = "fDenyTSConnections"
RESTORE_STATES = ("connected", "paused", "locked")


def _read_deny():
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, RDP_KEY) as key:
        return int(winreg.QueryValueEx(key, RDP_VALUE)[0])


def _write_deny(deny):
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, RDP_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RDP_VALUE, 0, winreg.REG_DWORD, 1 if deny else 0)


class RdpGuard:
    """Turns RDP off for a connect attempt and back on afterwards, only if it was on before."""

    def __init__(self, read=_read_deny, write=_write_deny):
        self._read = read
        self._write = write
        self.blocked = False

    def block(self):
        if self.blocked:
            return
        try:
            if self._read():
                return  # already off: not ours to turn back on
            self._write(True)
        except OSError as exc:
            log.warning("Không tắt được Remote Desktop (cần quyền Administrator?): %s", exc)
            return
        self.blocked = True
        log.info("Đã tắt Remote Desktop trong lúc kết nối VPN")

    def restore(self):
        if not self.blocked:
            return
        try:
            self._write(False)
        except OSError as exc:
            log.warning("Không bật lại được Remote Desktop: %s", exc)
            return
        self.blocked = False
        log.info("Đã bật lại Remote Desktop")

    def follow(self, state):
        """Called with each watchdog state: turn RDP back on once the attempt is over."""
        if state in RESTORE_STATES:
            self.restore()


rdp_guard = RdpGuard()


def before_connect(cfg):
    """Log off other sessions, then RDP off (each when enabled in the config)."""
    if cfg.logoff_others:
        logoff_others()
    if cfg.block_rdp:
        rdp_guard.block()
