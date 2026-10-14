"""Adapter over the Pulse Secure 9.x desktop UI and the FPT-CA PIN dialog it hosts.

Control IDs were read from Pulse Secure 9.1.4 and FPT-CA Token Manager v4.
Prompts are only looked for inside the Pulse.exe process, so a PIN dialog
raised by another program (a browser, a signing tool) is never answered.
"""
import enum
import re
import subprocess
import time
from dataclasses import dataclass

from . import winui

PULSE_EXE = r"C:\Program Files (x86)\Common Files\Pulse Secure\JamUI\Pulse.exe"
PULSE_IMAGE = "Pulse.exe"

MAIN_CLASS = "JamShadowClass"
MAIN_TITLE = "Pulse Secure"
LIST_ITEM_CLASS = "JAM_LISTITEM"
ID_CONNECTION_BUTTON = 10104
ID_MAIN_CLOSE = 1021  # hides the window to the tray; the window keeps existing

# "Connect to: <profile>" prompt
ID_USERNAME = 10111
ID_PASSWORD = 10115
ID_SAVE_SETTINGS = 10145
ID_PROMPT_CONNECT = 10201

# FPT-CA "Verify User PIN" dialog (loaded in-process by Pulse.exe)
PIN_DIALOG_CLASS = "#32770"
ID_PIN_EDIT = 1000
ID_PIN_LOGIN = 2000

PROMPT_CLASSES = ("#32770", "JamShadowClass")

# English and Vietnamese messages from the FPT-CA CSP language files.
TOKEN_ERROR = re.compile(
    r"pin is wrong|has been blocked|token has been removed|"
    r"mã pin sai|đã bị khóa|đã được rút ra",
    re.IGNORECASE,
)
ACCOUNT_ERROR = re.compile(
    r"invalid (user|password|credential)|incorrect|password|credential|"
    r"account (is )?(locked|disabled)|mật khẩu|bị khóa",
    re.IGNORECASE,
)


class State(enum.Enum):
    CONNECTED = "connected"
    CONNECTING = "connecting"
    # The button already reads "Connect" while the old session is torn down
    # (about a minute); a click then only queues "Connect requested".
    DISCONNECTING = "disconnecting"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


_BUTTON_STATES = {
    "disconnect": State.CONNECTED,
    "cancel": State.CONNECTING,
    "connect": State.DISCONNECTED,
}


@dataclass(frozen=True)
class ConnectionStatus:
    name: str
    state: State
    detail: str
    server: str = ""


def state_from_button(label, detail=""):
    state = _BUTTON_STATES.get(label.replace("&", "").strip().lower(), State.UNKNOWN)
    if state is State.DISCONNECTED and detail.lower().startswith("disconnecting"):
        return State.DISCONNECTING
    return state


def parse_item_texts(texts):
    """Return the human status line of a connection list item, e.g. 'Connecting; Failed; User input timeout.'"""
    parts = []
    if "Status:" in texts:
        index = texts.index("Status:")
        if index + 1 < len(texts):
            parts.append(texts[index + 1])
    for value in texts:
        if value.startswith("<a>"):
            parts.append(re.sub(r"</?a>", "", value))
    return "; ".join(p for p in parts if p)


def parse_server(texts):
    if "Server URL:" in texts:
        index = texts.index("Server URL:")
        if index + 1 < len(texts):
            return texts[index + 1]
    return ""


class Pulse:
    def __init__(self, exe=PULSE_EXE):
        self.exe = exe

    # --- process -----------------------------------------------------------

    def main_window(self):
        return winui.find_window(MAIN_CLASS, MAIN_TITLE)

    def pid(self):
        main = self.main_window()
        if main:
            return winui.pid_of(main)
        pids = winui.process_ids(PULSE_IMAGE)
        return pids[0] if pids else 0

    def is_running(self):
        return bool(self.pid())

    def launch(self, show=False):
        """`-tray` starts Pulse without its main window; `-show` makes a running Pulse create it."""
        subprocess.Popen([self.exe, "-show" if show else "-tray"], close_fds=True)

    def hide_main_window(self):
        main = self.main_window()
        if winui.is_visible(main):
            winui.click(winui.child_by_id(main, ID_MAIN_CLOSE))

    # --- connections -------------------------------------------------------

    def _items(self):
        main = self.main_window()
        for child in winui.children(main):
            if winui.class_name(child) == LIST_ITEM_CLASS:
                button = winui.child_by_id(child, ID_CONNECTION_BUTTON)
                if button:
                    yield winui.text(child), child, button

    def connections(self):
        return [name for name, _, _ in self._items()]

    def resolve_profile(self, profile, server=""):
        """The configured connection, else the one pointing at `server`, else the only one when no server is set."""
        names = self.connections()
        if profile in names:
            return profile
        if server:
            for name in names:
                status = self.status(name)
                if status and server.lower() in status.server.lower():
                    return name
            return None
        return names[0] if len(names) == 1 else None

    def status(self, profile):
        for name, item, button in self._items():
            if name == profile:
                texts = [t for t in (winui.safe_text(c) for c in winui.children(item)) if t]
                detail = parse_item_texts(texts)
                return ConnectionStatus(name, state_from_button(winui.text(button), detail),
                                        detail, parse_server(texts))
        return None

    def _button(self, profile):
        for name, _, button in self._items():
            if name == profile:
                return button
        raise LookupError(f"Pulse connection '{profile}' not found")

    def press_connection_button(self, profile):
        winui.click(self._button(profile))

    # One button per connection, labelled Connect / Cancel / Disconnect by state.
    connect = cancel = disconnect = press_connection_button

    # --- prompts -----------------------------------------------------------

    def _prompt_windows(self):
        pid = self.pid()
        if not pid:
            return []
        main = self.main_window()
        return [h for h in winui.top_windows(pid)
                if h != main and winui.is_visible(h) and winui.class_name(h) in PROMPT_CLASSES]

    @staticmethod
    def _is_pin_dialog(hwnd):
        edit = winui.child_by_id(hwnd, ID_PIN_EDIT)
        return (winui.class_name(hwnd) == PIN_DIALOG_CLASS and winui.class_name(edit) == "Edit"
                and bool(winui.child_by_id(hwnd, ID_PIN_LOGIN)))

    @staticmethod
    def _is_account_prompt(hwnd):
        return all(winui.child_by_id(hwnd, i) for i in (ID_USERNAME, ID_PASSWORD, ID_PROMPT_CONNECT))

    def find_pin_dialog(self):
        return next((h for h in self._prompt_windows() if self._is_pin_dialog(h)), 0)

    def find_account_prompt(self):
        return next((h for h in self._prompt_windows() if self._is_account_prompt(h)), 0)

    def describe(self, hwnd):
        texts = [t for t in (winui.safe_text(c) for c in winui.children(hwnd)) if t]
        return f"[{winui.class_name(hwnd)}] {winui.safe_text(hwnd)!r}: " + " | ".join(texts)

    def other_windows(self):
        """Visible Pulse dialogs other than the PIN dialog and the account prompt (in any of its steps)."""
        return [h for h in self._prompt_windows()
                if not self._is_pin_dialog(h) and not winui.child_by_id(h, ID_USERNAME)]

    def token_errors(self):
        return [d for d in (self.describe(h) for h in self.other_windows()) if TOKEN_ERROR.search(d)]

    def fill_pin(self, dialog, pin):
        """Type the PIN and press Login. Returns False when Login stayed disabled (nothing submitted)."""
        edit = winui.child_by_id(dialog, ID_PIN_EDIT)
        login = winui.child_by_id(dialog, ID_PIN_LOGIN)
        winui.type_text(edit, pin)
        time.sleep(0.2)
        if not winui.is_enabled(login):
            return False
        winui.press_dialog_button(dialog, ID_PIN_LOGIN)
        return True

    def fill_account(self, prompt, username, password, save_settings):
        """Fill the account prompt, tick 'Save settings' if asked, press Connect. Returns the checkbox state."""
        winui.type_text(winui.child_by_id(prompt, ID_USERNAME), username)
        winui.type_text(winui.child_by_id(prompt, ID_PASSWORD), password)
        saved = None
        box = winui.child_by_id(prompt, ID_SAVE_SETTINGS)
        if save_settings and winui.is_visible(box) and winui.is_enabled(box):
            if not winui.is_checked(box):
                winui.click(box)
                time.sleep(0.3)
            saved = winui.is_checked(box)
        winui.click(winui.child_by_id(prompt, ID_PROMPT_CONNECT))
        return saved
