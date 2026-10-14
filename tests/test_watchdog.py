from vpn_auto.config import Config
from vpn_auto.pulse import ConnectionStatus, State
from vpn_auto.watchdog import Watchdog

PROFILE = "profile-a"
SERVER = "vpn.example.test"


class FakePulse:
    def __init__(self):
        self.running = True
        self.has_window = True
        self.state = State.DISCONNECTED
        self.detail = "Disconnected"
        self.pin_dialog = 0
        self.account_prompt = 0
        self.errors = []
        self.calls = []
        self.pin_login_enabled = True

    def is_running(self):
        return self.running

    def main_window(self):
        return 1 if self.running and self.has_window else 0

    def launch(self, show=False):
        self.calls.append(("launch", show))

    def hide_main_window(self):
        self.calls.append(("hide",))

    def resolve_profile(self, profile, server=""):
        if profile == PROFILE or (not profile and server and server in SERVER):
            return PROFILE
        return None

    def status(self, profile):
        if profile != PROFILE:
            return None
        return ConnectionStatus(PROFILE, self.state, self.detail, SERVER)

    def connect(self, profile):
        self.calls.append(("connect", profile))

    def cancel(self, profile):
        self.calls.append(("cancel", profile))

    def disconnect(self, profile):
        self.calls.append(("disconnect", profile))

    def find_pin_dialog(self):
        return self.pin_dialog

    def find_account_prompt(self):
        return self.account_prompt

    def token_errors(self):
        return self.errors

    def other_windows(self):
        return []

    def describe(self, hwnd):
        return str(hwnd)

    def fill_pin(self, dialog, pin):
        self.calls.append(("pin", pin))
        return self.pin_login_enabled

    def fill_account(self, prompt, username, password, save_settings):
        self.calls.append(("account", username, password, save_settings))
        return save_settings

    def count(self, kind):
        return sum(1 for c in self.calls if c[0] == kind)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Network:
    def __init__(self):
        self.up = True
        self.checked = []

    def __call__(self, server):
        self.checked.append(server)
        return self.up


def make(network=None, **overrides):
    values = dict(profile=PROFILE, username="vpn-user", password="fake-password", pin="0000")
    values.update(overrides)
    cfg = Config(**values)
    pulse, clock = FakePulse(), Clock()
    notes = []
    dog = Watchdog(pulse, cfg, clock=clock, notify=lambda t, m: notes.append(t),
                   reachable=network or (lambda server: True))
    return dog, pulse, clock, notes


def test_waits_for_network_then_connects_immediately():
    net = Network()
    dog, pulse, clock, _ = make(network=net, backoff_base=10, backoff_max=300)
    for _ in range(6):  # several failed attempts push the backoff up
        dog.tick()
        clock.advance(400)
    before = pulse.count("connect")
    net.up = False
    clock.advance(3)
    assert dog.tick().state == "waiting-network"
    clock.advance(600)
    dog.tick()
    assert pulse.count("connect") == before
    net.up = True
    clock.advance(3)
    dog.tick()
    assert pulse.count("connect") == before + 1
    assert net.checked[-1] == SERVER


def test_network_check_can_be_disabled():
    net = Network()
    net.up = False
    dog, pulse, _, _ = make(network=net, network_check=False)
    dog.tick()
    assert pulse.count("connect") == 1
    assert net.checked == []


def test_profile_found_by_server_when_name_differs():
    dog, pulse, _, _ = make(profile="", server="example.test")
    assert dog.tick().state == "disconnected"
    assert pulse.calls == [("connect", PROFILE)]


def test_auto_connect_off_in_config_starts_paused():
    dog, pulse, _, _ = make(auto_connect=False)
    assert dog.tick().state == "paused"
    assert pulse.count("connect") == 0


def test_waits_for_pulse_to_finish_disconnecting():
    dog, pulse, clock, notes = make(busy_timeout=300)
    pulse.state = State.CONNECTED
    dog.tick()
    pulse.state, pulse.detail = State.DISCONNECTING, "Disconnecting"
    clock.advance(3)
    assert dog.tick().state == "disconnecting"
    clock.advance(60)
    dog.tick()
    assert pulse.count("connect") == 0
    assert notes[-1] == "VPN mất kết nối"
    pulse.state, pulse.detail = State.DISCONNECTED, "Disconnected"
    clock.advance(3)
    dog.tick()
    assert pulse.count("connect") == 1


def test_connects_anyway_when_disconnecting_never_ends():
    dog, pulse, clock, _ = make(busy_timeout=300)
    pulse.state = State.CONNECTED
    dog.tick()
    pulse.state, pulse.detail = State.DISCONNECTING, "Disconnecting"
    dog.tick()
    clock.advance(299)
    dog.tick()
    assert pulse.count("connect") == 0
    clock.advance(2)
    dog.tick()
    assert pulse.count("connect") == 1


def test_disconnect_pauses_and_drops_connection():
    dog, pulse, clock, _ = make()
    pulse.state = State.CONNECTED
    dog.tick()
    dog.disconnect()
    pulse.state = State.DISCONNECTED
    clock.advance(600)
    assert dog.tick().state == "paused"
    assert pulse.calls == [("disconnect", PROFILE)]


def test_connects_immediately_then_backs_off():
    dog, pulse, clock, _ = make(backoff_base=10, backoff_max=40)
    assert dog.tick().state == "disconnected"
    assert pulse.count("connect") == 1
    clock.advance(5)
    dog.tick()
    assert pulse.count("connect") == 1  # still inside backoff
    clock.advance(6)
    dog.tick()
    assert pulse.count("connect") == 2
    clock.advance(15)
    dog.tick()
    assert pulse.count("connect") == 2  # second backoff is 20s
    clock.advance(6)
    dog.tick()
    assert pulse.count("connect") == 3


def test_connected_resets_backoff_and_notifies_drop():
    dog, pulse, clock, notes = make()
    dog.tick()
    pulse.state = State.CONNECTED
    assert dog.tick().state == "connected"
    pulse.state = State.DISCONNECTED
    clock.advance(3)
    dog.tick()
    assert pulse.count("connect") == 2  # reconnects at once after a drop
    assert notes == ["VPN đã kết nối", "VPN mất kết nối"]


def test_pin_dialog_is_answered_once():
    dog, pulse, clock, _ = make()
    pulse.pin_dialog = 42
    assert dog.tick().state == "connecting"
    clock.advance(3)
    dog.tick()  # same dialog still closing
    assert [c for c in pulse.calls if c[0] == "pin"] == [("pin", "0000")]


def test_pin_dialog_that_never_closes_locks():
    dog, pulse, clock, notes = make(prompt_close_timeout=20)
    pulse.pin_dialog = 42
    dog.tick()
    clock.advance(21)
    assert dog.tick().state == "locked"
    clock.advance(100)
    dog.tick()
    assert pulse.count("pin") == 1
    assert "VPN Auto đã dừng tự động" in notes


def test_pin_reprompt_before_connecting_locks_without_retyping():
    dog, pulse, clock, _ = make()
    pulse.pin_dialog = 42
    dog.tick()
    pulse.pin_dialog = 0
    clock.advance(3)
    dog.tick()
    pulse.pin_dialog = 43  # asked again: PIN rejected
    clock.advance(3)
    assert dog.tick().state == "locked"
    assert pulse.count("pin") == 1


def test_pin_reprompt_hours_later_is_answered_again():
    dog, pulse, clock, _ = make(confirm_after=30)
    pulse.pin_dialog = 42
    dog.tick()
    pulse.pin_dialog = 0
    pulse.state = State.CONNECTED
    for _ in range(20):
        clock.advance(3)
        dog.tick()
    clock.advance(5 * 3600)  # token session expires
    pulse.pin_dialog = 77
    dog.tick()
    assert pulse.count("pin") == 2
    assert dog.locked_reason == ""


def test_pin_login_disabled_is_not_counted_as_attempt():
    dog, pulse, clock, _ = make()
    pulse.pin_dialog = 42
    pulse.pin_login_enabled = False
    dog.tick()
    clock.advance(3)
    pulse.pin_login_enabled = True
    dog.tick()
    assert pulse.count("pin") == 2
    assert dog.locked_reason == ""


def test_account_prompt_filled_with_save_settings():
    dog, pulse, _, _ = make()
    pulse.account_prompt = 7
    dog.tick()
    assert ("account", "vpn-user", "fake-password", True) in pulse.calls


def test_account_reprompt_allows_one_retry_then_locks():
    dog, pulse, clock, _ = make(max_account_attempts=2)
    for hwnd in (7, 8):
        pulse.account_prompt = hwnd
        dog.tick()
        pulse.account_prompt = 0
        clock.advance(3)
        dog.tick()
    pulse.account_prompt = 9
    clock.advance(3)
    assert dog.tick().state == "locked"
    assert pulse.count("account") == 2


def test_account_error_status_locks():
    dog, pulse, clock, _ = make()
    pulse.account_prompt = 7
    dog.tick()
    pulse.account_prompt = 0
    pulse.detail = "Disconnected; Invalid username or password"
    clock.advance(3)
    assert dog.tick().state == "locked"


def test_token_error_message_locks_and_stops_everything():
    dog, pulse, clock, _ = make()
    pulse.errors = ["User Pin is Wrong! Retry times : 4"]
    assert dog.tick().state == "locked"
    pulse.errors = []
    clock.advance(600)
    dog.tick()
    assert pulse.count("connect") == 0


def test_resume_clears_lock():
    dog, pulse, clock, _ = make()
    pulse.errors = ["Mã PIN sai!"]
    dog.tick()
    pulse.errors = []
    dog.resume()
    dog.tick()
    assert pulse.count("connect") == 1


def test_resume_can_keep_pause():
    dog, pulse, _, _ = make()
    dog.pause()
    pulse.errors = ["Mã PIN sai!"]
    dog.tick()
    pulse.errors = []
    dog.resume(unpause=False)
    assert dog.locked_reason == "" and dog.paused
    assert dog.tick().state == "paused"
    assert pulse.count("connect") == 0


def test_paused_does_not_connect_but_still_answers_prompts():
    dog, pulse, clock, _ = make()
    dog.pause()
    assert dog.tick().state == "paused"
    assert pulse.count("connect") == 0
    pulse.pin_dialog = 5  # user clicked Connect by hand
    dog.tick()
    assert pulse.count("pin") == 1


def test_stuck_connecting_is_cancelled():
    dog, pulse, clock, _ = make(busy_timeout=120)
    pulse.state = State.CONNECTING
    dog.tick()
    clock.advance(119)
    dog.tick()
    assert pulse.count("cancel") == 0
    clock.advance(2)
    dog.tick()
    assert pulse.count("cancel") == 1


def test_stuck_connecting_waits_while_prompt_is_open():
    dog, pulse, clock, _ = make(busy_timeout=120, prompt_close_timeout=1000)
    pulse.state = State.CONNECTING
    pulse.account_prompt = 7
    dog.tick()
    clock.advance(300)
    dog.tick()
    assert pulse.count("cancel") == 0


def test_starts_pulse_when_missing_at_most_once_a_minute():
    dog, pulse, clock, _ = make()
    pulse.running = False
    assert dog.tick().state == "waiting-pulse"
    clock.advance(30)
    dog.tick()
    clock.advance(31)
    dog.tick()
    assert pulse.calls == [("launch", False), ("launch", False)]


def test_pulse_started_in_tray_gets_its_window_then_hidden():
    dog, pulse, clock, _ = make()
    pulse.running = False
    dog.tick()
    pulse.running, pulse.has_window = True, False  # `-tray` start: no main window yet
    clock.advance(5)
    dog.tick()
    assert ("launch", True) not in pulse.calls
    clock.advance(6)
    dog.tick()
    assert pulse.calls[-1] == ("launch", True)
    pulse.has_window = True
    clock.advance(3)
    dog.tick()
    assert ("hide",) in pulse.calls and pulse.count("connect") == 1


def test_paused_never_starts_pulse():
    dog, pulse, clock, _ = make(auto_connect=False)
    pulse.running = False
    assert dog.tick().state == "waiting-pulse"
    clock.advance(600)
    dog.tick()
    assert pulse.calls == []


def test_missing_profile_and_incomplete_config():
    dog, pulse, _, _ = make()
    dog.config.profile = "other"
    assert dog.tick().state == "no-profile"
    dog.config.pin = ""
    assert dog.tick().state == "error"
    assert pulse.calls == []
