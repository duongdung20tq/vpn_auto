"""Keeps the Cisco Secure Client tunnel up: reconnects, answers the token PIN, stops on rejected secrets.

Mirrors the Pulse watchdog interface (tick/pause/resume/disconnect/update_config,
.paused/.locked_reason/.current/.config) so the window, tray, and app controller
are shared. The connect itself runs in a Cisco worker thread; this loop fills the
PIN dialog and decides when to (re)connect.
"""
import logging
import time

from . import network, prehook
from .cisco import State, Status
from .watchdog import STATE_LABELS, PromptTracker, Snapshot

log = logging.getLogger(__name__)


class CiscoWatchdog:
    def __init__(self, cisco, config, clock=time.monotonic, notify=None, reachable=network.reachable):
        self.cisco = cisco
        self.clock = clock
        self.notify = notify or (lambda title, message: None)
        self.reachable = reachable
        self.paused = False
        self.locked_reason = ""
        self.current = Status(config.server, config.server, State.DISCONNECTED, "")
        self._last_state = None
        self._attempts = 0
        self._next_connect_at = 0.0
        self._offline = False
        self._state_cache = State.UNKNOWN
        self._state_time = 0.0
        self.update_config(config)

    def update_config(self, config):
        self.config = config
        self.cisco.update(config.server, config.group, config.username, config.password)
        self.paused = not config.auto_connect
        self._pin = PromptTracker(config.max_pin_attempts, config.prompt_close_timeout, config.confirm_after)

    # --- user actions ------------------------------------------------------

    def resume(self, unpause=True):
        self.locked_reason = ""
        if unpause:
            self.paused = False
        self._pin.reset()
        self._attempts = 0
        self._next_connect_at = 0.0
        self._state_time = 0.0
        log.info("Bật lại tự động kết nối")

    def pause(self):
        self.paused = True
        log.info("Tạm dừng tự động kết nối")

    def disconnect(self):
        self.pause()
        if self.cisco.state() in (State.CONNECTED, State.CONNECTING):
            log.info("Ngắt kết nối theo yêu cầu")
            self.cisco.disconnect()
        self._state_time = 0.0

    # --- loop --------------------------------------------------------------

    def tick(self):
        try:
            snapshot = self._tick(self.clock())
        except Exception as exc:
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
            self.notify("VPN đã kết nối", self.config.server)
        elif snapshot.state in ("disconnected", "waiting-network") and previous == "connected":
            self.notify("VPN mất kết nối", "Đang tự kết nối lại...")
        elif snapshot.state == "locked":
            self.notify("VPN Auto đã dừng tự động", snapshot.detail)

    def _lock(self, reason):
        self.locked_reason = reason
        log.error("Dừng tự điền: %s", reason)
        return Snapshot("locked", reason)

    def _backoff(self):
        return min(self.config.backoff_base * 2 ** max(self._attempts - 1, 0), self.config.backoff_max)

    def _poll_state(self, now):
        if now - self._state_time >= self.config.state_poll:
            self._state_cache = self.cisco.state()
            self._state_time = now
        return self._state_cache

    def _tick(self, now):
        cfg = self.config
        if self.locked_reason:
            return Snapshot("locked", self.locked_reason)
        if not cfg.is_complete():
            return Snapshot("error", "Chưa cấu hình tài khoản/PIN")

        for message in self.cisco.token_errors():
            return self._lock(f"Token báo lỗi: {message}")

        pin_snapshot = self._handle_pin(now)
        if pin_snapshot:
            return pin_snapshot

        if self.cisco.connecting():
            self._state_time = 0.0
            return Snapshot("connecting", self.cisco.phase)

        result = self.cisco.take_result()
        if result is not None:
            if result.ok:
                self._attempts = 0
                self._next_connect_at = 0.0
            elif result.rejected:
                return self._lock(f"Cisco từ chối đăng nhập: {result.detail}")
            else:
                log.warning("Kết nối thất bại: %s", result.detail)
                self._next_connect_at = now + self._backoff()
            self._state_time = 0.0

        state = self._poll_state(now)
        self.current = Status(cfg.server, cfg.server, state, "")

        if state == State.CONNECTED:
            self._attempts = 0
            self._next_connect_at = 0.0
            self._pin.connected(now)
            return Snapshot("connected", "Đã kết nối")
        if state == State.DISCONNECTING:
            return Snapshot("disconnecting", "Đang ngắt phiên cũ")

        if self.paused:
            return Snapshot("paused", "Tạm dừng tự động kết nối")
        if not self.cisco.token_present():
            return Snapshot("waiting-token", "Chưa thấy chứng thư token — kiểm tra token FPT-CA đã cắm chưa")
        if cfg.network_check and not self.reachable(cfg.server):
            if not self._offline:
                log.info("Không tới được %s; chờ có mạng", cfg.server)
            self._offline = True
            return Snapshot("waiting-network", f"Chờ có mạng tới {cfg.server}")
        if self._offline:
            log.info("Đã có mạng; kết nối ngay")
            self._offline = False
            self._attempts = 0
            self._next_connect_at = 0.0

        if now >= self._next_connect_at:
            self._attempts += 1
            log.info("Bắt đầu kết nối '%s' nhóm '%s' (lần %d)", cfg.server, cfg.group or "-", self._attempts)
            self._pin.reset()  # fresh PIN budget for this attempt
            prehook.before_connect(cfg)
            self.cisco.start_connect()
            self._next_connect_at = now + self._backoff()
            return Snapshot("connecting", "Bắt đầu kết nối")
        return Snapshot("disconnected", "Chờ thử lại")

    def _handle_pin(self, now):
        dialog = self.cisco.find_pin_dialog()
        if not dialog:
            self._pin.not_visible(now)
            return None
        decision = self._pin.decide(dialog, now)
        if decision == "reject":
            return self._lock("Token hỏi lại PIN ngay sau khi đã nhập - có thể PIN sai. "
                              "Kiểm tra PIN rồi bấm 'Bỏ khóa'.")
        if decision == "answer":
            if self.cisco.fill_pin(dialog, self.config.pin):
                self._pin.answered(dialog, now)
                log.info("Đã nhập PIN token")
            else:
                log.warning("Đã gõ PIN nhưng nút Đăng nhập chưa bật; sẽ thử lại")
        return Snapshot("connecting", "Đang nhập PIN token")
