"""Keeps one Pulse connection up: reconnects, answers the PIN/account prompts, stops on rejected secrets.

The watchdog never answers the same prompt twice in a row without a successful
connection in between: a prompt that comes back means the secret was rejected,
and retrying would burn token PIN attempts or lock the VPN account.
"""
import logging
import time
from dataclasses import dataclass

from . import network, prehook
from .pulse import ACCOUNT_ERROR, State

log = logging.getLogger(__name__)

STATE_LABELS = {
    "connected": "Đã kết nối",
    "connecting": "Đang kết nối...",
    "disconnecting": "Pulse đang đóng phiên cũ, chờ để kết nối lại",
    "disconnected": "Mất kết nối - đang kết nối lại",
    "waiting-network": "Chờ có mạng",
    "paused": "Không tự kết nối",
    "locked": "Đã dừng: kiểm tra PIN/mật khẩu",
    "waiting-pulse": "Đang chờ Pulse Secure",
    "waiting-client": "Đang chờ Cisco Secure Client",
    "waiting-token": "Chưa thấy token - kiểm tra token đã cắm chưa",
    "no-profile": "Không thấy kết nối trong Pulse",
    "error": "Lỗi",
}


@dataclass(frozen=True)
class Snapshot:
    # connected | connecting | disconnected | waiting-network | paused (and disconnected)
    # | locked | waiting-pulse | no-profile | error
    state: str
    detail: str = ""


class PromptTracker:
    """Counts answers to one prompt kind that a successful connection has not yet confirmed."""

    def __init__(self, max_answers, close_timeout, confirm_after):
        self.max_answers = max_answers
        self.close_timeout = close_timeout
        self.confirm_after = confirm_after
        self.answers = 0
        self.pending = None  # (hwnd, answered_at) while our answer has not closed the prompt
        self.closed_at = None

    def decide(self, hwnd, now):
        """'answer', 'wait' (our answer is still being processed) or 'reject'."""
        if self.pending and self.pending[0] == hwnd:
            return "wait" if now - self.pending[1] < self.close_timeout else "reject"
        return "answer" if self.answers < self.max_answers else "reject"

    def answered(self, hwnd, now):
        self.answers += 1
        self.pending = (hwnd, now)

    def not_visible(self, now):
        if self.pending:
            self.pending = None
            self.closed_at = now

    def connected(self, now):
        """Forget answers once the prompt has stayed closed for a while on a live connection."""
        if self.pending is None and self.answers and (self.closed_at is None or now - self.closed_at >= self.confirm_after):
            self.answers = 0

    def reset(self):
        self.answers = 0
        self.pending = None
        self.closed_at = None


class Watchdog:
    def __init__(self, pulse, config, clock=time.monotonic, notify=None, reachable=network.reachable):
        self.pulse = pulse
        self.clock = clock
        self.notify = notify or (lambda title, message: None)
        self.reachable = reachable
        self._offline = False
        self.paused = False
        self.locked_reason = ""
        self._last_state = None
        self._attempts = 0
        self._next_connect_at = 0.0
        self._busy_since = None
        self._launched_at = None
        self._shown_at = None
        self._hide_pulse_pending = False
        self._seen_windows = set()
        self.current = None  # last ConnectionStatus seen, for the UI
        self.update_config(config)

    def update_config(self, config):
        self.config = config
        self.paused = not config.auto_connect
        self._pin = PromptTracker(config.max_pin_attempts, config.prompt_close_timeout, config.confirm_after)
        self._account = PromptTracker(config.max_account_attempts, config.prompt_close_timeout, config.confirm_after)

    # --- user actions ------------------------------------------------------

    def resume(self, unpause=True):
        """Clear a lockout (and the pause, unless told not to) and try to connect right away."""
        self.locked_reason = ""
        if unpause:
            self.paused = False
        self._pin.reset()
        self._account.reset()
        self._attempts = 0
        self._next_connect_at = 0.0
        log.info("Bật lại tự động kết nối")

    def pause(self):
        self.paused = True
        log.info("Tạm dừng tự động kết nối")

    def disconnect(self):
        """Pause auto-connect and drop the current connection or attempt."""
        self.pause()
        name = self.pulse.resolve_profile(self.config.profile, self.config.server)
        status = self.pulse.status(name) if name else None
        if status and status.state in (State.CONNECTED, State.CONNECTING):
            log.info("Ngắt kết nối '%s' theo yêu cầu", name)
            self.pulse.disconnect(name)

    # --- loop --------------------------------------------------------------

    def tick(self):
        try:
            snapshot = self._tick(self.clock())
        except Exception as exc:  # a vanished window must not kill the loop
            log.exception("Lỗi trong vòng giám sát")
            snapshot = Snapshot("error", str(exc))
        prehook.rdp_guard.follow(snapshot.state)
        if snapshot.state != self._last_state:
            log.info("Trạng thái: %s %s", STATE_LABELS.get(snapshot.state, snapshot.state), snapshot.detail)
            self._announce(self._last_state, snapshot)
            self._last_state = snapshot.state
        return snapshot

    def _announce(self, previous, snapshot):
        if snapshot.state == "connected":
            self.notify("VPN đã kết nối", self.current.name if self.current else "")
        elif snapshot.state in ("disconnecting", "disconnected", "waiting-network") and previous == "connected":
            self.notify("VPN mất kết nối", "Đang tự kết nối lại...")
        elif snapshot.state == "locked":
            self.notify("VPN Auto đã dừng tự động", snapshot.detail)

    def _lock(self, reason):
        self.locked_reason = reason
        log.error("Dừng tự điền: %s", reason)
        return Snapshot("locked", reason)

    def _tick(self, now):
        cfg = self.config
        if self.locked_reason:
            return Snapshot("locked", self.locked_reason)
        if not cfg.is_complete():
            return Snapshot("error", "Chưa cấu hình tài khoản/PIN")

        # While paused nothing is started automatically, but prompts raised by a
        # connection the user started by hand are still answered.
        if not self.pulse.main_window():
            if not self.paused:
                self._start_pulse(now)
            return Snapshot("waiting-pulse", "Đang chờ Pulse Secure khởi động")
        if self._hide_pulse_pending:
            self._hide_pulse_pending = False
            self.pulse.hide_main_window()

        errors = self.pulse.token_errors()
        if errors:
            return self._lock(f"Token báo lỗi: {errors[0]}")

        snapshot = self._handle_prompts(now)
        if snapshot:
            return snapshot
        self._log_unknown_windows()

        name = self.pulse.resolve_profile(cfg.profile, cfg.server)
        status = self.pulse.status(name) if name else None
        self.current = status
        if status is None:
            return Snapshot("no-profile", f"Không thấy kết nối '{cfg.profile or cfg.server}' trong Pulse")
        return self._drive_connection(status, now)

    def _start_pulse(self, now):
        """Start Pulse in the tray; if it runs without its main window, ask it to create one."""
        if not self.pulse.is_running():
            if self._launched_at is None or now - self._launched_at >= 60:
                log.info("Khởi động Pulse Secure")
                self.pulse.launch(show=False)
                self._launched_at = now
        elif (self._launched_at is None or now - self._launched_at >= 10) and \
                (self._shown_at is None or now - self._shown_at >= 60):
            log.info("Pulse chạy nhưng chưa có cửa sổ chính; mở cửa sổ Pulse")
            self.pulse.launch(show=True)
            self._shown_at = now
            self._hide_pulse_pending = True

    def _handle_prompts(self, now):
        cfg = self.config
        pin_dialog = self.pulse.find_pin_dialog()
        if not pin_dialog:
            self._pin.not_visible(now)
        else:
            decision = self._pin.decide(pin_dialog, now)
            if decision == "reject":
                return self._lock("Token hỏi lại PIN ngay sau khi đã nhập - có thể PIN sai. Kiểm tra PIN rồi bấm 'Tiếp tục'.")
            if decision == "answer":
                if self.pulse.fill_pin(pin_dialog, cfg.pin):
                    self._pin.answered(pin_dialog, now)
                    log.info("Đã nhập PIN token")
                else:
                    log.warning("Đã gõ PIN nhưng nút Login chưa bật; sẽ thử lại")
            return Snapshot("connecting", "Đang nhập PIN token")

        prompt = self.pulse.find_account_prompt()
        if not prompt:
            self._account.not_visible(now)
        else:
            decision = self._account.decide(prompt, now)
            if decision == "reject":
                return self._lock("Pulse hỏi lại tài khoản ngay sau khi đã nhập - có thể sai mật khẩu. Kiểm tra rồi bấm 'Tiếp tục'.")
            if decision == "answer":
                saved = self.pulse.fill_account(prompt, cfg.username, cfg.password, cfg.save_settings)
                self._account.answered(prompt, now)
                log.info("Đã điền tài khoản %s (Save settings: %s)", cfg.username, saved)
            return Snapshot("connecting", "Đang đăng nhập tài khoản VPN")
        return None

    def _log_unknown_windows(self):
        for hwnd in self.pulse.other_windows():
            if hwnd not in self._seen_windows:
                self._seen_windows.add(hwnd)
                log.warning("Cửa sổ Pulse chưa xử lý: %s", self.pulse.describe(hwnd))

    def _backoff(self):
        cfg = self.config
        return min(cfg.backoff_base * 2 ** max(self._attempts - 1, 0), cfg.backoff_max)

    def _drive_connection(self, status, now):
        if status.state == State.CONNECTED:
            self._attempts = 0
            self._next_connect_at = 0.0
            self._busy_since = None
            self._pin.connected(now)
            self._account.connected(now)
            return Snapshot("connected", status.detail)

        if status.state == State.CONNECTING:
            self._busy_since = self._busy_since or now
            if not self.paused and now - self._busy_since >= self.config.busy_timeout:
                log.warning("Kết nối treo %.0fs (%s); hủy để thử lại", now - self._busy_since, status.detail)
                self.pulse.cancel(status.name)
                self._busy_since = None
                self._next_connect_at = now + self._backoff()
            return Snapshot("connecting", status.detail)

        if status.state == State.DISCONNECTING:
            self._busy_since = self._busy_since or now
            if now - self._busy_since < self.config.busy_timeout:
                return Snapshot("disconnecting", status.detail)
            # Still tearing down after the timeout: click Connect anyway, Pulse queues it.
        else:
            self._busy_since = None
        if status.state in (State.DISCONNECTED, State.DISCONNECTING):
            if self._account.answers and ACCOUNT_ERROR.search(status.detail):
                return self._lock(f"Pulse từ chối đăng nhập: {status.detail}")
            if self.paused:
                return Snapshot("paused", "Tạm dừng tự động kết nối")
            if self.config.network_check and status.server and not self.reachable(status.server):
                if not self._offline:
                    log.info("Không tới được server %s; chờ có mạng", status.server)
                self._offline = True
                return Snapshot("waiting-network", f"Chờ có mạng tới {status.server}")
            if self._offline:
                log.info("Đã có mạng; kết nối ngay")
                self._offline = False
                self._attempts = 0
                self._next_connect_at = 0.0
            if now >= self._next_connect_at:
                self._attempts += 1
                log.info("Bấm Connect '%s' (lần %d, trạng thái trước: %s)", status.name, self._attempts, status.detail or "-")
                prehook.before_connect(self.config)
                self.pulse.connect(status.name)
                self._next_connect_at = now + self._backoff()
            return Snapshot("disconnected", status.detail)
        return Snapshot("error", f"Trạng thái Pulse không rõ: {status.detail}")
