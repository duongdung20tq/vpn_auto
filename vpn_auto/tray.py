"""Tray icon: coloured by connection state, menu to open the window, connect, toggle auto, quit."""
import logging
import threading

import pystray
from PIL import Image, ImageDraw

from .watchdog import STATE_LABELS as LABELS

log = logging.getLogger(__name__)

COLORS = {
    "connected": "#2ea043",
    "connecting": "#e6a000",
    "disconnecting": "#e6a000",
    "disconnected": "#d23232",
    "waiting-network": "#d23232",
    "locked": "#8c28a0",
    "paused": "#808080",
}


def color_for(state, paused):
    if paused and state != "connected":
        return COLORS["paused"]
    return COLORS.get(state, COLORS["disconnected"])


def _image(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=color)
    draw.line([(18, 18), (32, 46), (46, 18)], fill="white", width=8)
    return img


class Tray:
    def __init__(self, controller, on_show, on_quit):
        self.controller = controller
        item = pystray.MenuItem
        menu = pystray.Menu(
            item(lambda _: LABELS.get(controller.snapshot.state, controller.snapshot.state), None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Mở VPN Auto", lambda: on_show(), default=True),
            item("Kết nối ngay", lambda: controller.connect_now()),
            item("Tự động kết nối khi có mạng", lambda: controller.set_auto(controller.watchdog.paused),
                 checked=lambda _: not controller.watchdog.paused),
            pystray.Menu.SEPARATOR,
            item("Thoát", lambda: on_quit()),
        )
        self.icon = pystray.Icon("vpn_auto", _image(COLORS["connecting"]), "VPN Auto", menu=menu)

    def update(self):
        snapshot = self.controller.snapshot
        self.icon.icon = _image(color_for(snapshot.state, self.controller.watchdog.paused))
        self.icon.title = f"VPN Auto - {LABELS.get(snapshot.state, snapshot.state)}"[:127]
        self.icon.update_menu()

    def notify(self, title, message):
        try:
            self.icon.notify(message or " ", title)
        except Exception:
            log.debug("notification failed", exc_info=True)

    def start(self):
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    def stop(self):
        self.icon.stop()
