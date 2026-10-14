"""Desktop app: watchdog thread + main window + tray icon, wired through one Controller."""
import logging
import queue
import threading
import tkinter as tk

from . import config as config_mod
from . import deploy, prehook
from .watchdog import Snapshot

log = logging.getLogger(__name__)
SHOW_EVENT = "Local\\vpn_auto_show_window"


def _build_watchdog(config, notify):
    """Pick the Pulse or Cisco engine from config.backend."""
    if config.backend == config_mod.BACKEND_CISCO:
        from .cisco import Cisco
        from .cisco_watchdog import CiscoWatchdog
        engine = Cisco(config.server, config.group, config.username, config.password, config.cisco_cli)
        return CiscoWatchdog(engine, config, notify=notify)
    from .pulse import Pulse
    from .watchdog import Watchdog
    return Watchdog(Pulse(config.pulse_exe), config, notify=notify)


class Controller:
    """Thread-safe facade over the watchdog for the UI and the tray."""

    def __init__(self, start_paused=False):
        self.lock = threading.RLock()
        self.config = config_mod.load()
        self._backend = self.config.backend
        self.watchdog = _build_watchdog(self.config, self.notify)
        if start_paused:
            self.watchdog.pause()
        self.snapshot = Snapshot("connecting")
        self.notifier = None
        self.on_change = None
        self._stop = threading.Event()
        self._mtime = self._config_mtime()

    # --- loop --------------------------------------------------------------

    @staticmethod
    def _config_mtime():
        path = config_mod.config_path()
        return path.stat().st_mtime if path.exists() else None

    def notify(self, title, message):
        if self.notifier:
            self.notifier(title, message)

    def _changed(self):
        if self.on_change:
            self.on_change()

    def _loop(self):
        while not self._stop.is_set():
            if self._config_mtime() != self._mtime:  # edited by `setup` or another tool
                self._apply(config_mod.load())
                log.info("Nạp lại cấu hình từ file")
            with self.lock:
                snapshot = self.watchdog.tick()
            if snapshot != self.snapshot:
                self.snapshot = snapshot
                self._changed()
            self._stop.wait(self.watchdog.config.check_interval)

    def start(self):
        threading.Thread(target=self._loop, name="watchdog", daemon=True).start()

    def stop(self):
        self._stop.set()

    # --- actions (any thread) ---------------------------------------------

    def _apply(self, cfg):
        with self.lock:
            self.config = cfg
            self._mtime = self._config_mtime()
            if cfg.backend != self._backend:  # switched Pulse <-> Cisco: rebuild the engine
                self._backend = cfg.backend
                self.watchdog = _build_watchdog(cfg, self.notify)
            else:
                self.watchdog.update_config(cfg)
            self.watchdog.resume(unpause=cfg.auto_connect)
        self._changed()

    def _save_auto(self, on):
        self.config.auto_connect = on
        config_mod.save(self.config)
        self._mtime = self._config_mtime()

    def connect_now(self):
        with self.lock:
            self.watchdog.resume()
            self._save_auto(True)
        self._changed()

    def set_auto(self, on):
        with self.lock:
            if on:
                self.watchdog.resume()
            else:
                self.watchdog.pause()
            self._save_auto(on)
        self._changed()

    def disconnect(self):
        with self.lock:
            self.watchdog.disconnect()
            self._save_auto(False)
        self._changed()

    def unlock(self):
        with self.lock:
            self.watchdog.resume(unpause=False)
        self._changed()

    def save_config(self, cfg):
        config_mod.save(cfg)
        self._apply(cfg)
        log.info("Đã lưu cấu hình")

    def import_ini(self, path):
        cfg, keys = deploy.import_file(path, self.config)
        self._apply(cfg)
        return keys

    def open_client(self):
        """Open the native VPN client window for the active backend."""
        with self.lock:
            pulse = getattr(self.watchdog, "pulse", None)
            cisco = getattr(self.watchdog, "cisco", None)
        if pulse is not None:
            pulse.launch(show=True)
        elif cisco is not None:
            cisco.launch_ui(minimized=False)

    def profiles(self):
        """Pulse connection names for the combobox; empty for the Cisco backend."""
        with self.lock:
            pulse = getattr(self.watchdog, "pulse", None)
            if pulse is None:
                return []
            try:
                return pulse.connections() if pulse.main_window() else []
            except Exception:
                log.debug("cannot list Pulse connections", exc_info=True)
                return []


def _watch_show_requests(commands, show):
    """A second launch of the exe signals this event instead of starting another watchdog."""
    import win32event

    event = win32event.CreateEvent(None, False, False, SHOW_EVENT)
    while True:
        win32event.WaitForSingleObject(event, win32event.INFINITE)
        commands.put(show)


def run(start_paused=False, minimized=False):
    from .tray import Tray
    from .ui import MainWindow

    cfg = config_mod.load()
    try:
        deploy.import_if_changed(cfg)
    except Exception:
        log.exception("Không nhập được %s", deploy.default_path())

    controller = Controller(start_paused)
    commands = queue.Queue()  # callables to run on the Tk thread
    root = tk.Tk()
    window = MainWindow(root, controller, commands)
    tray = Tray(controller, on_show=lambda: commands.put(window.show), on_quit=lambda: commands.put(window.quit))
    controller.notifier = tray.notify
    controller.on_change = tray.update
    threading.Thread(target=_watch_show_requests, args=(commands, window.show), daemon=True).start()

    if minimized and controller.config.is_complete():
        root.withdraw()
    controller.start()
    tray.start()
    log.info("VPN Auto đã khởi động")
    try:
        root.mainloop()
    finally:
        controller.stop()
        prehook.rdp_guard.restore()  # never leave RDP off after exit
        tray.stop()
        log.info("VPN Auto đã thoát")
