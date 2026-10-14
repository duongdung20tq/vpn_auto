import time

import vpn_auto.rdp as rdp


class FakeProc:
    def __init__(self):
        self.pid = 4321
        self.alive = True

    def poll(self):
        return None if self.alive else 0


def mocks(monkeypatch, desktop=lambda: False, cred=lambda: False, error=lambda: False):
    calls = []
    monkeypatch.setattr(rdp, "accept_security_warning", lambda pid: None)
    monkeypatch.setattr(rdp, "kill", lambda proc: calls.append(("kill",)))
    monkeypatch.setattr(rdp, "launch", lambda ip, u="", p="": (calls.append(("launch", ip, u, p)), (FakeProc(), None))[1])
    monkeypatch.setattr(rdp, "desktop_shown", lambda pid: desktop())
    monkeypatch.setattr(rdp, "credential_prompt_present", cred)
    monkeypatch.setattr(rdp, "error_dialog", lambda pid: error())
    monkeypatch.setattr(rdp, "_safe_remove", lambda p: None)
    return calls


def connector(**kw):
    kw.setdefault("retry_delay", 0.02)
    kw.setdefault("poll_interval", 0.02)
    kw.setdefault("connect_timeout", 2.0)
    return rdp.RdpConnector(kw.pop("ip", "10.0.0.9"), kw.pop("username", "u"), kw.pop("password", "p"), **kw)


def wait_state(conn, state, timeout=4):
    t = time.time()
    while time.time() - t < timeout:
        if conn.state == state:
            return True
        time.sleep(0.02)
    return False


def test_reachable_is_pure():
    assert rdp.reachable("203.0.113.1", timeout=0.3) is False  # TEST-NET, unroutable


def test_split_host_port():
    assert rdp.split_host_port("10.0.0.5") == ("10.0.0.5", 3389)
    assert rdp.split_host_port("10.0.0.5:3390") == ("10.0.0.5", 3390)


def test_connects_when_desktop_appears(monkeypatch):
    calls = mocks(monkeypatch, desktop=lambda: True)
    c = connector()
    c.start()
    assert wait_state(c, "connected")
    assert any(x[0] == "launch" and x[3] == "p" for x in calls)  # launched with the password
    assert ("kill",) not in calls


def test_launches_immediately_without_probe_gate(monkeypatch):
    """A lossy host whose port probe fails must still be attempted (the key fix)."""
    monkeypatch.setattr(rdp, "reachable", lambda ip, **k: False)  # probe would say unreachable
    calls = mocks(monkeypatch, desktop=lambda: True)
    c = connector()
    c.start()
    assert wait_state(c, "connected")
    assert any(x[0] == "launch" for x in calls)  # launched anyway, not stuck waiting


def test_retries_until_desktop(monkeypatch):
    launches = {"n": 0}
    desktop = {"ok": False}
    calls = mocks(monkeypatch, desktop=lambda: desktop["ok"], error=lambda: not desktop["ok"])

    def launch(ip, u="", p=""):
        launches["n"] += 1
        calls.append(("launch", ip))
        if launches["n"] >= 3:
            desktop["ok"] = True
        return FakeProc(), None

    monkeypatch.setattr(rdp, "launch", launch)
    c = connector(connect_timeout=1.0)
    c.start()
    assert wait_state(c, "connected")
    assert launches["n"] >= 3
    assert sum(1 for x in calls if x[0] == "kill") >= 2  # failed attempts closed


def test_no_password_stops_at_login_prompt(monkeypatch):
    mocks(monkeypatch, desktop=lambda: False, cred=lambda: True)
    c = connector(password="")
    c.start()
    assert wait_state(c, "ready")  # reaching the password prompt is success when no password given


def test_wrong_password_errors(monkeypatch):
    mocks(monkeypatch, desktop=lambda: False, cred=lambda: True)
    c = connector(password="secret")
    c.start()
    assert wait_state(c, "error")


def test_error_dialog_triggers_retry(monkeypatch):
    calls = mocks(monkeypatch, desktop=lambda: False, error=lambda: True)
    c = connector()
    c.start()
    time.sleep(0.3)
    c.stop()
    assert any(x[0] == "launch" for x in calls)
    assert any(x[0] == "kill" for x in calls)


def test_enable_resource_toggles_ticks_only_unchecked_checkboxes(monkeypatch):
    """Clipboard/Drives/... are BS_AUTOCHECKBOX buttons that default to unchecked; Connect/Cancel
    are plain push buttons and must never be clicked by this helper."""
    import vpn_auto.rdp as rdp_mod

    DIALOG = "dlg"
    CONNECT, CANCEL, CLIPBOARD, DRIVES, ALREADY_ON = "connect", "cancel", "clip", "drives", "on"
    classes = {CONNECT: "Button", CANCEL: "Button", CLIPBOARD: "Button", DRIVES: "Button",
              ALREADY_ON: "Button", "label": "Static"}
    styles = {CONNECT: 0x50010000, CANCEL: 0x50010001,  # push buttons: low nibble 0 / 1
             CLIPBOARD: 0x50014C03, DRIVES: 0x50014C03, ALREADY_ON: 0x50014C03}  # BS_AUTOCHECKBOX
    checked = {CLIPBOARD: 0, DRIVES: 0, ALREADY_ON: 1}  # already-on one must be left alone
    clicked = []

    monkeypatch.setattr(rdp_mod.win32gui, "EnumChildWindows",
                        lambda h, cb, acc: [cb(c, acc) for c in classes] and None)
    monkeypatch.setattr(rdp_mod.win32gui, "GetClassName", lambda c: classes[c])
    monkeypatch.setattr(rdp_mod.win32gui, "GetWindowLong", lambda c, flag: styles.get(c, 0))

    def send(c, msg, w, l):
        if msg == rdp_mod.BM_GETCHECK:
            return checked.get(c, 0)
        if msg == rdp_mod.BM_CLICK:
            clicked.append(c)
        return 0

    monkeypatch.setattr(rdp_mod.win32gui, "SendMessage", send)

    rdp_mod._enable_resource_toggles(DIALOG)

    assert set(clicked) == {CLIPBOARD, DRIVES}  # only the two unchecked toggles were clicked
    assert CONNECT not in clicked and CANCEL not in clicked and ALREADY_ON not in clicked


def test_stop_halts_loop(monkeypatch):
    mocks(monkeypatch, desktop=lambda: False, error=lambda: True)
    c = connector()
    c.start()
    assert wait_state(c, "connecting")
    c.stop()
    assert wait_state(c, "stopped")
