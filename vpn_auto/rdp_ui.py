"""Small window: type an IP, auto-retry the RDP connection, auto-fill the password."""
import logging
import queue
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import rdp, rdp_store

POLL_MS = 400
MASK = "•"

STATE_TEXT = {
    "idle": "Sẵn sàng",
    "connecting": "Đang kết nối lại...",
    "connected": "Đã kết nối (vào desktop)",
    "ready": "Đã hiện ô nhập mật khẩu - mời nhập",
    "stopped": "Đã dừng",
    "error": "Lỗi",
}
STATE_COLOR = {"connected": "#2ea043", "ready": "#2ea043", "connecting": "#e6a000",
               "error": "#d23232", "stopped": "#808080", "idle": "#808080"}
TERMINAL = ("connected", "ready", "stopped", "error")


class RdpWindow:
    def __init__(self, root, initial_ip=""):
        self.root = root
        self.connector = None
        self.events = queue.Queue()
        root.title("RDP Auto - tự kết nối lại Remote Desktop")
        root.resizable(False, False)
        self._build()
        self._load_targets(initial_ip)
        root.after(POLL_MS, self._drain)

    def _build(self):
        pad = {"padx": 10, "pady": 6}
        frame = ttk.Frame(self.root, padding=14)
        frame.grid(sticky="nsew")
        frame.columnconfigure(1, weight=1)

        self.ip = tk.StringVar()
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.remember = tk.BooleanVar(value=True)

        ttk.Label(frame, text="IP máy cần kết nối").grid(row=0, column=0, sticky="w", **pad)
        self.ip_box = ttk.Combobox(frame, textvariable=self.ip, width=28)
        self.ip_box.grid(row=0, column=1, columnspan=2, sticky="we", pady=6)
        # Any change to the IP - picked from the list or typed by hand - must re-look-up that
        # machine's own saved account, otherwise a password typed for one IP keeps sitting in the
        # field and gets used (or expected) for the next IP, which is the bug being fixed here.
        self.ip.trace_add("write", lambda *_: self._fill_from_saved())

        ttk.Label(frame, text="Tài khoản").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.username).grid(row=1, column=1, columnspan=2, sticky="we", pady=6)

        ttk.Label(frame, text="Mật khẩu").grid(row=2, column=0, sticky="w", **pad)
        self.pw_entry = ttk.Entry(frame, textvariable=self.password, show=MASK)
        self.pw_entry.grid(row=2, column=1, sticky="we", pady=6)
        shown = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Hiện", variable=shown,
                        command=lambda: self.pw_entry.configure(show="" if shown.get() else MASK)
                        ).grid(row=2, column=2, sticky="w", padx=(6, 0))
        self.pw_hint = ttk.Label(frame, text="", foreground="#777")
        self.pw_hint.grid(row=3, column=1, sticky="w")

        ttk.Checkbutton(frame, text="Nhớ máy này (mật khẩu mã hóa DPAPI)", variable=self.remember
                        ).grid(row=4, column=1, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(frame, foreground="#777", wraplength=360,
                  text="Để trống mật khẩu: tool thử lại tới khi vào được màn hình đăng nhập để bạn tự nhập. "
                       "Có mật khẩu: tool tự điền và vào thẳng.").grid(row=5, column=0, columnspan=3, sticky="w")

        status = ttk.Frame(frame)
        status.grid(row=6, column=0, columnspan=3, sticky="we", pady=(10, 4))
        self.dot = tk.Canvas(status, width=16, height=16, highlightthickness=0)
        self.dot.pack(side="left")
        self.dot_item = self.dot.create_oval(2, 2, 14, 14, fill="#808080", outline="")
        self.status_label = ttk.Label(status, text="Sẵn sàng", font=("Segoe UI", 10, "bold"))
        self.status_label.pack(side="left", padx=8)

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=3, sticky="we", pady=(4, 0))
        self.connect_btn = ttk.Button(buttons, text="Kết nối", command=self._connect)
        self.connect_btn.pack(side="left")
        self.stop_btn = ttk.Button(buttons, text="Dừng", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(buttons, text="Xóa máy đã lưu", command=self._forget).pack(side="right")

        self.log = tk.Text(frame, height=7, width=52, state="disabled", font=("Consolas", 9))
        self.log.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(10, 0))

    # --- data --------------------------------------------------------------

    def _load_targets(self, initial_ip):
        targets, last = rdp_store.load()
        self.ip_box.configure(values=[t.ip for t in targets])
        self.ip.set(initial_ip or last or (targets[0].ip if targets else ""))
        self._fill_from_saved()  # also runs via the trace above, but .set() may be a no-op

    def _fill_from_saved(self):
        """Load the account saved for the current IP and nothing else - never a leftover
        username/password typed for whatever machine was selected before this one."""
        t = rdp_store.get(self.ip.get().strip())
        self.password.set("")  # a saved password is never shown back; it's used from the store
        if t:
            self.username.set(t.username)
            self.pw_hint.configure(text="đã lưu mật khẩu" if t.password else "chưa lưu mật khẩu")
        else:
            self.username.set("")
            self.pw_hint.configure(text="")

    def _forget(self):
        ip = self.ip.get().strip()
        if ip and messagebox.askyesno("RDP Auto", f"Xóa máy {ip} đã lưu?", parent=self.root):
            rdp_store.remove(ip)
            rdp.clear_credential(ip)
            self._load_targets("")
            self._append(f"Đã xóa {ip}")

    # --- actions -----------------------------------------------------------

    def _connect(self):
        ip = self.ip.get().strip()
        if not ip:
            messagebox.showerror("RDP Auto", "Cần nhập IP.", parent=self.root)
            return
        saved = rdp_store.get(ip)
        password = self.password.get() or (saved.password if saved else "")
        username = self.username.get().strip()
        if self.remember.get():
            rdp_store.save(rdp_store.Target(ip, username, self.password.get()))
        self._load_targets(ip)
        self.connector = rdp.RdpConnector(
            ip, username, password,
            on_update=lambda s, d, a: self.events.put((s, d, a)))
        self.connector.start()
        self.connect_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._append(f"Bắt đầu kết nối {ip}...")

    def _stop(self):
        if self.connector:
            self.connector.stop()
        self.connect_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    # --- status ------------------------------------------------------------

    def _append(self, line):
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"{stamp}  {line}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain(self):
        try:
            while True:
                state, detail, attempt = self.events.get_nowait()
                self.status_label.configure(text=STATE_TEXT.get(state, state))
                self.dot.itemconfigure(self.dot_item, fill=STATE_COLOR.get(state, "#808080"))
                self._append(detail or STATE_TEXT.get(state, state))
                if state in TERMINAL:
                    self.connect_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._drain)


def run(initial_ip=""):
    logging.basicConfig(level=logging.INFO)
    root = tk.Tk()
    RdpWindow(root, initial_ip)
    root.mainloop()
