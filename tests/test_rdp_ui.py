"""Switching between saved machines must use each machine's own stored password - a password
typed for one IP must never leak into, or be required again for, another IP."""
import tkinter as tk

import pytest

from vpn_auto import rdp, rdp_store
from vpn_auto.rdp_ui import RdpWindow


class FakeConnector:
    instances = []

    def __init__(self, ip, username, password, on_update=None):
        self.ip, self.username, self.password = ip, username, password
        FakeConnector.instances.append((ip, username, password))

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
    assert window.password.get() == ""  # masked, but still used from the store
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
