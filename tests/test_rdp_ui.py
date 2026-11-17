"""Switching between saved machines must use each machine's own stored password - a password
typed for one IP must never leak into, or be required again for, another IP."""
import tkinter as tk

import pytest

from vpn_auto import rdp, rdp_store
from vpn_auto.rdp_ui import RdpWindow


class FakeConnector:
    instances = []
    sessions = []  # sessions requested by each connect, in order

    def __init__(self, ip, username, password, sessions=1, on_update=None):
        self.ip, self.username, self.password = ip, username, password
        FakeConnector.instances.append((ip, username, password))
        FakeConnector.sessions.append(sessions)

    def start(self):
        pass

    def stop(self, close_session=False):
        pass


@pytest.fixture(scope="module")
def tk_root():
    # One Tk() for the whole file: creating/destroying the Tcl interpreter per test is flaky
    # on this machine (intermittent "couldn't read ttk/panedwindow.tcl" from rapid re-init).
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def window(tmp_path, monkeypatch, tk_root):
    monkeypatch.setenv("VPN_AUTO_HOME", str(tmp_path))
    monkeypatch.setattr(rdp, "RdpConnector", FakeConnector)
    FakeConnector.instances.clear()
    FakeConnector.sessions.clear()
    win = RdpWindow(tk_root)
    yield win
    for child in tk_root.winfo_children():
        child.destroy()


def last_connect():
    return FakeConnector.instances[-1]


def test_switching_ip_clears_username_and_password_fields(window):
    window.ip.set("1.1.1.1")
    window.username.set("userA")
    window.password.set("passA")
    window.ip.set("2.2.2.2")  # a different, never-saved machine
    assert window.password.get() == ""
    assert window.username.get() == ""


def test_each_machine_connects_with_its_own_credentials(window):
    window.remember.set(True)

    window.ip.set("1.1.1.1")
    window.username.set("userA")
    window.password.set("passA")
    window._connect()
    assert last_connect() == ("1.1.1.1", "userA", "passA")

    window.ip.set("2.2.2.2")
    window.username.set("userB")
    window.password.set("passB")
    window._connect()
    assert last_connect() == ("2.2.2.2", "userB", "passB")  # not A's leftover creds


def test_reconnecting_a_saved_machine_needs_no_retyping(window):
    window.remember.set(True)
    window.ip.set("1.1.1.1")
    window.username.set("userA")
    window.password.set("passA")
    window._connect()

    window.ip.set("2.2.2.2")
    window.username.set("userB")
    window.password.set("passB")
    window._connect()

    window.ip.set("1.1.1.1")  # switch back, do NOT retype anything
    assert window.username.get() == "userA"
    assert window.password.get() == "passA"  # A's saved password is filled back in
    window._connect()
    assert last_connect() == ("1.1.1.1", "userA", "passA")  # A's password, not B's


def test_store_keeps_both_machines_independently(window):
    window.remember.set(True)
    for ip, user, pw in (("1.1.1.1", "userA", "passA"), ("2.2.2.2", "userB", "passB")):
        window.ip.set(ip)
        window.username.set(user)
        window.password.set(pw)
        window._connect()

    targets, _ = rdp_store.load()
    by_ip = {t.ip: t for t in targets}
    assert by_ip["1.1.1.1"].password == "passA"
    assert by_ip["2.2.2.2"].password == "passB"


# --- multi remote: the session count belongs to each machine, never shared ----------------

def connect_as(window, ip, user, pw, multi=False, count="1"):
    window.remember.set(True)
    window.ip.set(ip)
    window.username.set(user)
    window.password.set(pw)
    window.multi.set(multi)
    window.count.set(count)
    window._connect()


def test_multi_count_is_saved_per_machine(window):
    connect_as(window, "1.1.1.1", "userA", "passA", multi=True, count="3")
    connect_as(window, "2.2.2.2", "userB", "passB", multi=False)
    assert FakeConnector.sessions == [3, 1]

    window.ip.set("1.1.1.1")
    assert window.multi.get() is True and window.count.get() == "3"
    window.ip.set("2.2.2.2")
    assert window.multi.get() is False  # A's multi remote did not leak into B

    window.ip.set("1.1.1.1")  # reconnect A without touching anything
    window._connect()
    assert FakeConnector.sessions[-1] == 3


def test_unsaved_machine_starts_with_multi_off(window):
    connect_as(window, "1.1.1.1", "userA", "passA", multi=True, count="4")
    window.ip.set("3.3.3.3")  # never saved
    assert window.multi.get() is False and window.count.get() == "1"


def test_ticking_multi_with_count_one_bumps_it_to_two(window):
    window.ip.set("1.1.1.1")
    window.multi.set(True)
    window._on_multi_toggle()
    assert window.count.get() == "2"
    assert str(window.count_box.cget("state")) == "normal"


def test_multi_without_password_is_refused(window, monkeypatch):
    errors = []
    monkeypatch.setattr("vpn_auto.rdp_ui.messagebox.showerror", lambda *a, **k: errors.append(a))
    connect_as(window, "4.4.4.4", "userD", "", multi=True, count="2")
    assert errors and FakeConnector.instances == []


def test_store_reads_old_file_without_multi_fields(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("VPN_AUTO_HOME", str(tmp_path))
    rdp_store.store_path().write_text(json.dumps(
        {"last_ip": "5.5.5.5", "targets": [{"ip": "5.5.5.5", "username": "u", "password_dpapi": ""}]}),
        encoding="utf-8")
    t = rdp_store.get("5.5.5.5")
    assert (t.multi, t.count, t.sessions) == (False, 1, 1)


def test_store_clamps_session_count():
    assert rdp_store.clamp_count("abc") == 1
    assert rdp_store.clamp_count(0) == 1
    assert rdp_store.clamp_count(99) == rdp_store.MAX_SESSIONS
    assert rdp_store.Target("x", multi=False, count=5).sessions == 1
    assert rdp_store.Target("x", multi=True, count=5).sessions == 5


def test_selecting_saved_machine_shows_its_password_masked_with_toggle(window):
    connect_as(window, "1.1.1.1", "userA", "passA")
    connect_as(window, "2.2.2.2", "userB", "passB")

    window.ip.set("1.1.1.1")
    assert window.password.get() == "passA"
    assert window.pw_entry.cget("show") == "•"  # hidden by default

    window.show_pw.set(True)
    window.pw_entry.configure(show="")  # what the "Hiện" checkbox command does
    window.ip.set("2.2.2.2")
    assert window.password.get() == "passB"  # B's own password, not A's
