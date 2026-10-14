from vpn_auto.cisco import ConnectResult, State, parse_state
from vpn_auto.cisco_watchdog import CiscoWatchdog
from vpn_auto.config import BACKEND_CISCO, Config

SERVER = "vpn.example.test"


def test_parse_state_takes_last():
    out = "  >> state: Unknown\n >> state: Disconnected\n >> state: Connecting\n >> state: Connected\n"
    assert parse_state(out) is State.CONNECTED
    assert parse_state("  >> state: Disconnected\n") is State.DISCONNECTED
    assert parse_state("  >> state: Disconnecting\n") is State.DISCONNECTING
    assert parse_state("no state here") is State.UNKNOWN


class FakeCisco:
    def __init__(self):
        self._state = State.DISCONNECTED
        self._connecting = False
        self._result = None
        self.phase = "idle"
        self.pin_dialog = 0
        self.pin_login_ok = True
        self.errors = []
        self.token = True
        self.calls = []

    def token_present(self):
        return self.token

    def update(self, server, group, username, password):
        self.calls.append(("update", server, group))

    def token_errors(self):
        return self.errors

    def find_pin_dialog(self):
        return self.pin_dialog

    def fill_pin(self, dialog, pin):
        self.calls.append(("pin", pin))
        return self.pin_login_ok

    def connecting(self):
        return self._connecting

    def take_result(self):
        result, self._result = self._result, None
        return result

    def start_connect(self):
        self.calls.append(("start_connect",))
        self._connecting = True

    def state(self):
        return self._state

    def disconnect(self):
        self.calls.append(("disconnect",))
        self._state = State.DISCONNECTED

    def finish(self, result, state):
        self._connecting = False
        self._result = result
        self._state = state

    def count(self, kind):
        return sum(1 for c in self.calls if c[0] == kind)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make(reachable=True, **overrides):
    values = dict(backend=BACKEND_CISCO, server=SERVER, group="Cert-kyso",
                  username="vpn-user", password="fake-password", pin="0000", state_poll=0.0)
    values.update(overrides)
    cfg = Config(**values)
    cisco, clock = FakeCisco(), Clock()
    notes = []
    dog = CiscoWatchdog(cisco, cfg, clock=clock, notify=lambda t, m: notes.append(t),
                        reachable=lambda s: reachable)
    return dog, cisco, clock, notes


def test_connects_when_disconnected():
    dog, cisco, _, _ = make()
    assert dog.tick().state == "connecting"
    assert cisco.count("start_connect") == 1


def test_connecting_then_connected():
    dog, cisco, clock, notes = make()
    dog.tick()
    assert dog.tick().state == "connecting"  # worker still running
    cisco.finish(ConnectResult(True, False, "Đã kết nối"), State.CONNECTED)
    clock.advance(1)
    assert dog.tick().state == "connected"
    assert "VPN đã kết nối" in notes


def test_reconnects_after_drop():
    dog, cisco, clock, notes = make()
    dog.tick()  # starts first connect
    cisco.finish(ConnectResult(True, False, "ok"), State.CONNECTED)
    clock.advance(1)
    assert dog.tick().state == "connected"
    assert cisco.count("start_connect") == 1
    cisco._state = State.DISCONNECTED  # tunnel drops (network, idle timeout, etc.)
    clock.advance(1)
    snap = dog.tick()
    assert snap.state == "connecting"
    assert cisco.count("start_connect") == 2  # auto-reconnect kicked in


def test_rejected_credentials_lock():
    dog, cisco, clock, _ = make()
    dog.tick()
    cisco.finish(ConnectResult(False, True, "Sai mật khẩu"), State.DISCONNECTED)
    clock.advance(1)
    assert dog.tick().state == "locked"


def test_connect_failure_backs_off_then_retries():
    dog, cisco, clock, _ = make(backoff_base=10, backoff_max=40)
    dog.tick()
    cisco.finish(ConnectResult(False, False, "Hết thời gian"), State.DISCONNECTED)
    clock.advance(1)
    dog.tick()
    assert cisco.count("start_connect") == 1  # inside backoff
    clock.advance(11)
    dog.tick()
    assert cisco.count("start_connect") == 2


def test_pin_dialog_answered_once():
    dog, cisco, clock, _ = make()
    cisco.pin_dialog = 42
    assert dog.tick().state == "connecting"
    assert cisco.count("pin") == 1
    clock.advance(1)
    dog.tick()
    assert cisco.count("pin") == 1  # same dialog still open, not retyped


def test_pin_reprompt_within_attempt_locks():
    dog, cisco, clock, _ = make()
    cisco._connecting = True  # a connect attempt is running, so no per-attempt reset happens
    cisco.pin_dialog = 42
    dog.tick()
    cisco.pin_dialog = 0
    clock.advance(1)
    dog.tick()
    cisco.pin_dialog = 43  # token asks again -> wrong PIN
    clock.advance(1)
    assert dog.tick().state == "locked"
    assert cisco.count("pin") == 1


def test_stale_pin_dialog_before_attempt_does_not_block_next_connect():
    dog, cisco, clock, _ = make()
    cisco.pin_dialog = 99  # leftover dialog from a previous manual attempt
    dog.tick()             # answered (budget would be 1)
    cisco.pin_dialog = 0
    clock.advance(1)
    dog.tick()             # no dialog -> starts a connect, resets PIN budget
    assert cisco.count("start_connect") == 1
    cisco._connecting = True
    cisco.pin_dialog = 100  # the real PIN dialog for this attempt
    clock.advance(1)
    assert dog.tick().state == "connecting"
    assert dog.locked_reason == ""  # fresh budget, not locked


def test_token_error_locks():
    dog, cisco, _, _ = make()
    cisco.errors = ["Mã PIN sai! Số lần còn lại: 3"]
    assert dog.tick().state == "locked"
    assert cisco.count("start_connect") == 0


def test_waiting_token_when_token_absent():
    dog, cisco, _, _ = make()
    cisco.token = False
    assert dog.tick().state == "waiting-token"
    assert cisco.count("start_connect") == 0


def test_waiting_network():
    dog, cisco, _, _ = make(reachable=False)
    assert dog.tick().state == "waiting-network"
    assert cisco.count("start_connect") == 0


def test_paused_does_not_connect():
    dog, cisco, _, _ = make(auto_connect=False)
    assert dog.tick().state == "paused"
    assert cisco.count("start_connect") == 0


def test_disconnect_pauses_and_drops():
    dog, cisco, _, _ = make()
    cisco._state = State.CONNECTED
    dog.tick()
    dog.disconnect()
    assert cisco.count("disconnect") == 1
    assert dog.tick().state == "paused"


def test_logoff_runs_before_connect(monkeypatch):
    import vpn_auto.prehook as ph
    calls = []
    monkeypatch.setattr(ph, "logoff_others", lambda: calls.append("logoff"))
    dog, cisco, _, _ = make()
    dog.tick()
    assert calls == ["logoff"]
    assert cisco.count("start_connect") == 1


def test_logoff_and_rdp_on_every_attempt_including_reconnect(monkeypatch):
    import vpn_auto.prehook as ph
    calls = []
    monkeypatch.setattr(ph, "logoff_others", lambda: calls.append("logoff"))
    dog, cisco, clock, _ = make()
    dog.tick()  # first connect -> logoff #1, RDP off
    assert ph.rdp_guard.blocked
    cisco.finish(ConnectResult(True, False, "ok"), State.CONNECTED)
    clock.advance(1)
    dog.tick()  # connected -> RDP back on
    assert not ph.rdp_guard.blocked
    cisco._state = State.DISCONNECTED  # drop
    clock.advance(1)
    dog.tick()  # auto-reconnect -> logoff #2, RDP off again
    assert calls == ["logoff", "logoff"]
    assert ph.rdp_guard.blocked


def test_incomplete_config_errors():
    dog, cisco, _, _ = make(pin="")
    assert dog.tick().state == "error"
    assert cisco.count("start_connect") == 0
