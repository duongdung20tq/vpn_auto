from vpn_auto import prehook
from vpn_auto.prehook import logoff_others as real_logoff_others  # conftest stubs the module attribute


QUSER = """\
 USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME
>Administrator         console             1  Active      none   10/14/2026 8:00 AM
 dungdt                rdp-tcp#3           2  Active         5   10/14/2026 9:00 AM
 guest                                     3  Disc          1:02  10/14/2026 7:00 AM
"""


def test_parse_sessions_handles_disconnected():
    assert prehook.parse_sessions(QUSER) == [("Administrator", 1), ("dungdt", 2), ("guest", 3)]


def test_logoff_others_keeps_admin_and_me():
    calls = []

    def run(args):
        calls.append(args)
        return (0, QUSER) if args == ["quser"] else (0, "")

    assert real_logoff_others(keep={"ADMINISTRATOR", "dungdt"}, run=run) == 1
    assert calls == [["quser"], ["logoff", "3"]]


def test_logoff_failure_does_not_raise():
    def run(args):
        if args == ["quser"]:
            return 0, QUSER
        return 1, "Access is denied."

    assert real_logoff_others(keep={"administrator"}, run=run) == 0


def test_quser_missing_does_not_raise():
    def run(args):
        raise FileNotFoundError("quser")

    assert real_logoff_others(keep={"administrator"}, run=run) == 0


class FakeReg:
    def __init__(self, deny=0, fail=False):
        self.deny = deny
        self.fail = fail
        self.writes = []

    def read(self):
        return self.deny

    def write(self, deny):
        if self.fail:
            raise PermissionError("access denied")
        self.deny = int(deny)
        self.writes.append(int(deny))


def test_rdp_blocked_until_connected():
    reg = FakeReg(deny=0)
    guard = prehook.RdpGuard(reg.read, reg.write)
    guard.block()
    guard.block()  # second attempt does not write again
    assert reg.deny == 1 and reg.writes == [1]
    guard.follow("connecting")
    guard.follow("disconnected")
    assert reg.deny == 1
    guard.follow("connected")
    assert reg.deny == 0 and reg.writes == [1, 0]


def test_rdp_already_off_is_left_off():
    reg = FakeReg(deny=1)
    guard = prehook.RdpGuard(reg.read, reg.write)
    guard.block()
    guard.follow("connected")
    assert reg.deny == 1 and reg.writes == []


def test_rdp_restored_on_pause_and_lock():
    for state in ("paused", "locked"):
        reg = FakeReg(deny=0)
        guard = prehook.RdpGuard(reg.read, reg.write)
        guard.block()
        guard.follow(state)
        assert reg.deny == 0


def test_rdp_write_failure_does_not_raise():
    reg = FakeReg(deny=0, fail=True)
    guard = prehook.RdpGuard(reg.read, reg.write)
    guard.block()
    assert not guard.blocked


def test_before_connect_respects_flags(monkeypatch):
    from vpn_auto.config import Config
    reg = FakeReg(deny=0)
    logoffs = []
    monkeypatch.setattr(prehook, "rdp_guard", prehook.RdpGuard(reg.read, reg.write))
    monkeypatch.setattr(prehook, "logoff_others", lambda: logoffs.append(1))
    prehook.before_connect(Config(logoff_others=False, block_rdp=False))
    assert reg.writes == [] and logoffs == []
    prehook.before_connect(Config(logoff_others=True, block_rdp=True))
    assert reg.writes == [1] and logoffs == [1]
