"""Command line: `run` (tray, default), `watch` (console), `status`, `setup`, `settings`."""
import argparse
import getpass
import logging
import logging.handlers
import os
import sys
import threading
import time

from . import config as config_mod
from .pulse import Pulse

MUTEX_NAME = "Local\\vpn_auto_pulse_watchdog"
LOG_FORMAT = "%(asctime)s  %(levelname)-7s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _setup_logging(console):
    handlers = [logging.handlers.RotatingFileHandler(config_mod.log_path(), maxBytes=1_000_000,
                                                     backupCount=3, encoding="utf-8")]
    if console and sys.stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format=LOG_FORMAT, datefmt=LOG_DATEFMT)


def _single_instance():
    """Keep one watchdog per user; two would race on the same prompts."""
    import win32api
    import win32event
    import winerror

    handle = win32event.CreateMutex(None, False, MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        return None
    return handle


def _show_running_instance():
    import pywintypes
    import win32event

    from .app import SHOW_EVENT

    try:
        win32event.SetEvent(win32event.OpenEvent(win32event.EVENT_MODIFY_STATE, False, SHOW_EVENT))
    except pywintypes.error:
        pass


def cmd_run(args):
    from . import app

    mutex = _single_instance()
    if mutex is None:
        logging.getLogger(__name__).info("already running; showing its window")
        _show_running_instance()
        return 0
    _enable_dpi_awareness()
    app.run(start_paused=args.paused, minimized=args.minimized)
    return 0


def _enable_dpi_awareness():
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def cmd_watch(_args):
    from .app import _build_watchdog

    mutex = _single_instance()
    if mutex is None:
        print("VPN Auto đang chạy ở tiến trình khác (icon khay). Thoát nó trước.")
        return 1
    cfg = config_mod.load()
    if not cfg.is_complete():
        print("Chưa cấu hình. Chạy: python -m vpn_auto setup")
        return 1
    dog = _build_watchdog(cfg, notify=lambda t, m: None)
    print("Đang giám sát, Ctrl+C để dừng.")
    try:
        while True:
            dog.tick()
            time.sleep(cfg.check_interval)
    except KeyboardInterrupt:
        return 0


def cmd_status(_args):
    cfg = config_mod.load()
    print(f"Config : {config_mod.config_path()}")
    print(f"Backend: {cfg.backend}   Server: {cfg.server or '-'}   Group: {cfg.group or '-'}   "
          f"Profile: {cfg.profile or '-'}")
    print(f"Tài khoản: {cfg.username or '-'}   Mật khẩu: {'đã lưu' if cfg.password else 'chưa'}   "
          f"PIN: {'đã lưu' if cfg.pin else 'chưa'}   Tự kết nối: {'bật' if cfg.auto_connect else 'tắt'}")
    print(f"Log    : {config_mod.log_path()}")
    if cfg.backend == config_mod.BACKEND_CISCO:
        from .cisco import Cisco, token_cert_present
        cisco = Cisco(cfg.server, cfg.group, cfg.username, cfg.password, cfg.cisco_cli)
        print(f"Token FPT-CA: {'đã cắm (thấy chứng thư)' if token_cert_present() else 'KHÔNG thấy - kiểm tra token'}")
        print(f"Cisco state : {cisco.state().value}")
        return 0
    pulse = Pulse(cfg.pulse_exe)
    if not pulse.main_window():
        print("Pulse Secure: không chạy")
        return 1
    chosen = pulse.resolve_profile(cfg.profile, cfg.server)
    for name in pulse.connections():
        status = pulse.status(name)
        marker = "*" if name == chosen else " "
        print(f" {marker} {name:<28} {status.state.value:<13} {status.server:<22} {status.detail}")
    return 0


def cmd_setup(args):
    cfg = config_mod.load()
    try:
        profiles = Pulse(cfg.pulse_exe).connections()
    except Exception:
        profiles = []
    if profiles:
        print("Kết nối trong Pulse:", ", ".join(profiles))
    cfg.profile = args.profile or input(f"Kết nối Pulse [{cfg.profile or (profiles[0] if profiles else '')}]: ").strip() \
        or cfg.profile or (profiles[0] if profiles else "")
    cfg.username = args.username or input(f"Tài khoản VPN [{cfg.username}]: ").strip() or cfg.username
    if args.from_env:
        cfg.password = os.environ.get("VPN_AUTO_PASSWORD", cfg.password)
        cfg.pin = os.environ.get("VPN_AUTO_PIN", cfg.pin)
    else:
        cfg.password = getpass.getpass("Mật khẩu VPN (Enter = giữ nguyên): ") or cfg.password
        cfg.pin = getpass.getpass("PIN token FPT-CA (Enter = giữ nguyên): ") or cfg.pin
    if args.no_save_settings:
        cfg.save_settings = False
    if args.server:
        cfg.server = args.server
    if profiles and cfg.profile not in profiles:
        print(f"Cảnh báo: không thấy kết nối '{cfg.profile}' trong Pulse.")
    if not cfg.is_complete():
        print("Thiếu thông tin, chưa lưu.")
        return 1
    config_mod.save(cfg)
    print(f"Đã lưu (PIN/mật khẩu mã hóa DPAPI): {config_mod.config_path()}")
    return 0


def cmd_rdp(args):
    if not args.console:
        from . import rdp_ui
        _enable_dpi_awareness()
        rdp_ui.run(initial_ip=args.ip)
        return 0
    from . import rdp, rdp_store

    ip = args.ip or rdp_store.load()[1]
    if not ip:
        print("Cần IP: python -m vpn_auto rdp --console <ip>")
        return 1
    saved = rdp_store.get(ip)
    username = args.username or (saved.username if saved else "")
    password = os.environ.get("RDP_PASSWORD", "") if args.from_env else ""
    password = password or (saved.password if saved else "")
    done = threading.Event()

    def on_update(state, detail, attempt):
        print(f"{time.strftime('%H:%M:%S')}  [{state}] {detail}")
        if state in ("connected", "ready", "stopped", "error"):
            done.set()

    conn = rdp.RdpConnector(ip, username, password, on_update=on_update)
    print(f"Thử kết nối RDP tới {ip}, Ctrl+C để dừng.")
    conn.start()
    try:
        while not done.wait(0.5):
            pass
    except KeyboardInterrupt:
        conn.stop()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vpn_auto", description="Tự kết nối Pulse Secure + nhập PIN token FPT-CA")
    parser.set_defaults(paused=False, minimized=False)
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="mở giao diện + icon khay (mặc định)")
    run.add_argument("--paused", action="store_true", help="không tự kết nối lần này")
    run.add_argument("--minimized", action="store_true", help="khởi động thu nhỏ dưới khay")
    sub.add_parser("watch", help="giám sát trong console")
    sub.add_parser("status", help="xem trạng thái Pulse và cấu hình")
    setup = sub.add_parser("setup", help="nhập tài khoản/PIN trong console")
    setup.add_argument("--profile")
    setup.add_argument("--server")
    setup.add_argument("--username")
    setup.add_argument("--from-env", action="store_true", help="đọc VPN_AUTO_PASSWORD và VPN_AUTO_PIN từ biến môi trường")
    setup.add_argument("--no-save-settings", action="store_true", help="không tích 'Save settings' trong Pulse")
    rdp = sub.add_parser("rdp", help="tự kết nối lại Remote Desktop tới một IP")
    rdp.add_argument("ip", nargs="?", default="", help="IP máy cần kết nối")
    rdp.add_argument("--console", action="store_true", help="chạy trong console thay vì giao diện")
    rdp.add_argument("--username", default="")
    rdp.add_argument("--from-env", action="store_true", help="đọc RDP_PASSWORD từ biến môi trường")
    args = parser.parse_args(argv)
    command = args.command or "run"
    _setup_logging(console=command in ("watch", "status", "setup") or (command == "rdp" and args.console))
    handler = {"run": cmd_run, "watch": cmd_watch, "status": cmd_status, "setup": cmd_setup, "rdp": cmd_rdp}[command]
    return handler(args)
