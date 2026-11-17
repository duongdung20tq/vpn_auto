import threading
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
    kw.setdefault("stagger", 0.05)
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


# --- mstsc error box detection --------------------------------------------------------------

def fake_windows(monkeypatch, tops, children):
    """tops: {hwnd: (class, title)}; children: {hwnd: [(class, text), ...]}."""
    kid_info = {}
    kid_map = {}
    next_id = [10_000]
    for top, kids in children.items():
        ids = []
        for cls, text in kids:
            next_id[0] += 1
            kid_info[next_id[0]] = (cls, text)
            ids.append(next_id[0])
        kid_map[top] = ids
    info = {**tops, **kid_info}
    monkeypatch.setattr(rdp, "_tops", lambda pid=None: list(tops))
    monkeypatch.setattr(rdp.win32gui, "GetClassName", lambda h: info[h][0])
    monkeypatch.setattr(rdp.win32gui, "GetWindowText", lambda h: info[h][1])
    monkeypatch.setattr(rdp.win32gui, "EnumChildWindows",
                        lambda h, cb, acc: [cb(c, acc) for c in kid_map.get(h, [])])


def test_error_dialog_detects_taskdialog_error_box(monkeypatch):
    # what mstsc shows for "Remote Desktop can't connect..." (dumped live): text lives in DirectUIHWND
    fake_windows(monkeypatch, {1: ("#32770", "Remote Desktop Connection")},
                 {1: [("DirectUIHWND", ""), ("CtrlNotifySink", ""), ("SysLink", ""), ("Button", "OK")]})
    assert rdp.error_dialog(4321) is True


def test_error_dialog_ignores_connecting_progress_box(monkeypatch):
    fake_windows(monkeypatch, {1: ("#32770", "Remote Desktop Connection")},
                 {1: [("Static", "Connecting to:"), ("Static", "10.0.0.9"), ("msctls_progress32", ""),
                      ("Button", "Cancel"), ("Static", "Initiating remote connection...")]})
    assert rdp.error_dialog(4321) is False


def test_error_dialog_ignores_security_warning(monkeypatch):
    fake_windows(monkeypatch, {1: ("#32770", "Remote Desktop Connection security warning")},
                 {1: [("DirectUIHWND", ""), ("Button", "Co&nnect")]})
    assert rdp.error_dialog(4321) is False


def test_error_dialog_still_reads_classic_error_text(monkeypatch):
    fake_windows(monkeypatch, {1: ("#32770", "Remote Desktop Connection")},
                 {1: [("Static", "Remote Desktop can't connect to the remote computer"), ("Button", "OK")]})
    assert rdp.error_dialog(4321) is True


# --- multi remote: several windows race, the first one in wins -----------------------------

def multi_mocks(monkeypatch, need):
    """need[session] = the attempt on which that session's desktop shows (None = never).
    Each launch is tagged with its session from the worker thread name ('rdp-<ip>-<n>')."""
    procs, killed = {}, []
    lock = threading.Lock()
    monkeypatch.setattr(rdp, "accept_security_warning", lambda pid: None)
    monkeypatch.setattr(rdp, "credential_prompt_present", lambda: False)
    monkeypatch.setattr(rdp, "_safe_remove", lambda p: None)

    def launch(ip, u="", p=""):
        session = int(threading.current_thread().name.rsplit("-", 1)[1])
        with lock:
            proc = FakeProc()
            proc.pid = 1000 + len(procs)
            proc.session = session
            proc.attempt = 1 + sum(1 for x in procs.values() if x.session == session)
            proc.started = time.time()
            procs[proc.pid] = proc
        return proc, None

    def ok(pid):
        proc = procs[pid]
        return need[proc.session] is not None and proc.attempt >= need[proc.session]

    monkeypatch.setattr(rdp, "launch", launch)
    monkeypatch.setattr(rdp, "desktop_shown", ok)
    monkeypatch.setattr(rdp, "error_dialog", lambda pid: not ok(pid))
    monkeypatch.setattr(rdp, "kill", lambda proc: killed.append(proc.pid) if proc else None)
    return procs, killed


def first_launch(procs, session):
    return min(p.started for p in procs.values() if p.session == session)


def sessions_launched(procs):
    return {p.session for p in procs.values()}


def test_multi_first_session_in_wins_and_closes_the_rest(monkeypatch):
    # session 1 keeps failing; session 2 gets in on its first try -> 1 is closed, 3 never starts
    procs, killed = multi_mocks(monkeypatch, need={1: None, 2: 1, 3: 1})
    states = []
    c = connector(sessions=3, stagger=0.3, on_update=lambda s, d, a: states.append((s, d)))
    c.start()
    assert wait_state(c, "connected")
    time.sleep(0.4)  # past the time session 3 would have started
    assert sessions_launched(procs) == {1, 2}
    winner = [p.pid for p in procs.values() if p.session == 2]
    losers = [p.pid for p in procs.values() if p.session == 1]
    assert not set(winner) & set(killed)  # the session that got in stays open
    assert set(losers) <= set(killed)     # every other window was closed
    assert states[-1][0] == "connected"
    assert states[-1][1].startswith("Phiên 2/3: đã vào desktop 10.0.0.9")


def test_multi_sessions_start_one_stagger_apart(monkeypatch):
    procs, killed = multi_mocks(monkeypatch, need={1: None, 2: None, 3: None})
    c = connector(sessions=3, stagger=0.3)
    c.start()
    t = time.time()
    while sessions_launched(procs) != {1, 2, 3} and time.time() - t < 3:
        time.sleep(0.02)
    c.stop()
    assert wait_state(c, "stopped")
    gap12 = first_launch(procs, 2) - first_launch(procs, 1)
    gap23 = first_launch(procs, 3) - first_launch(procs, 2)
    assert 0.25 <= gap12 < 0.6 and 0.25 <= gap23 < 0.6
    assert set(procs) <= set(killed)  # nothing got in, so stopping closed every window


def test_multi_retries_each_session_on_its_own(monkeypatch):
    # sessions 1 and 2 never get in and keep retrying; session 3 gets in on its 2nd try
    procs, killed = multi_mocks(monkeypatch, need={1: None, 2: None, 3: 2})
    c = connector(sessions=3)
    c.start()
    assert wait_state(c, "connected")
    per_session = {s: sum(1 for p in procs.values() if p.session == s) for s in (1, 2, 3)}
    assert per_session[3] == 2 and per_session[1] >= 2 and per_session[2] >= 2
    winner = max(p.pid for p in procs.values() if p.session == 3)
    assert set(procs) - set(killed) == {winner}  # only the winning window is left open


def test_multi_keeps_one_session_when_two_get_in_together(monkeypatch):
    procs, killed = multi_mocks(monkeypatch, need={1: 1, 2: 1})
    t0 = time.time()
    both_in = lambda pid: time.time() - t0 > 0.3  # sessions 1 and 2 reach the desktop at once
    monkeypatch.setattr(rdp, "desktop_shown", both_in)
    monkeypatch.setattr(rdp, "error_dialog", lambda pid: False)
    c = connector(sessions=2)
    c.start()
    assert wait_state(c, "connected")
    assert sessions_launched(procs) == {1, 2}
    assert len(set(procs) - set(killed)) == 1  # exactly one session left open


def test_single_session_keeps_the_old_messages(monkeypatch):
    procs, killed = multi_mocks(monkeypatch, need={1: 1})
    states = []
    c = connector(on_update=lambda s, d, a: states.append((s, d)))
    c.start()
    assert wait_state(c, "connected")
    assert len(procs) == 1
    assert states[-1] == ("connected", "Đã vào desktop 10.0.0.9 sau 1 lần")
    assert not any("Phiên" in d for _, d in states)


def test_multi_needs_username_and_password(monkeypatch):
    calls = mocks(monkeypatch, desktop=lambda: True)
    c = connector(sessions=2, password="")
    c.start()
    assert wait_state(c, "error")
    assert not any(x[0] == "launch" for x in calls)  # never opened a window


def test_multi_wrong_password_stops_every_session(monkeypatch):
    calls = mocks(monkeypatch, cred=lambda: True)
    c = connector(sessions=3, stagger=0.3)
    c.start()
    assert wait_state(c, "error")
    time.sleep(0.4)  # past the next session's start time
    assert sum(1 for x in calls if x[0] == "launch") == 1  # no more windows after a refusal


def test_stop_with_close_session_closes_the_open_session(monkeypatch):
    procs, killed = multi_mocks(monkeypatch, need={1: 1, 2: 1})
    c = connector(sessions=2)
    c.start()
    assert wait_state(c, "connected")
    c.stop(close_session=True)
    assert set(procs) <= set(killed)
