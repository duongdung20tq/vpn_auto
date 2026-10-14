"""Main window: live status, connection controls, configuration and log."""
import logging
import os
import queue
import tkinter as tk
from dataclasses import replace
from tkinter import filedialog, messagebox, ttk

from PIL import ImageTk

from . import config as config_mod
from . import deploy
from .tray import LABELS, _image, color_for

POLL_MS = 500
MAX_LOG_LINES = 400
MASK = "•"

BACKEND_DISPLAY = {config_mod.BACKEND_CISCO: "Cisco Secure Client", config_mod.BACKEND_PULSE: "Pulse Secure"}
DISPLAY_BACKEND = {v: k for k, v in BACKEND_DISPLAY.items()}


class _QueueHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__(logging.INFO)
        self.sink = sink

    def emit(self, record):
        self.sink.put(self.format(record))


class MainWindow:
    def __init__(self, root, controller, commands):
        self.root = root
        self.c = controller
        self.commands = commands
        self._closed = False
        self._hidden_once = False
        self.log_lines = queue.Queue()
        handler = _QueueHandler(self.log_lines)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)

        root.title("VPN Auto - Pulse Secure + token FPT-CA")
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.minsize(620, 600)
        self._icon = ImageTk.PhotoImage(_image("#2ea043"))
        root.iconphoto(True, self._icon)
        self._build()
        self._load_form()
        self._load_log_tail()
        root.after(POLL_MS, self._poll)

    # --- layout ------------------------------------------------------------

    def _build(self):
        pad = {"padx": 10, "pady": 6}
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # status
        status = ttk.LabelFrame(outer, text="Trạng thái", padding=10)
        status.grid(row=0, column=0, sticky="we")
        status.columnconfigure(1, weight=1)
        self.dot = tk.Canvas(status, width=22, height=22, highlightthickness=0)
        self.dot.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 10))
        self.dot_item = self.dot.create_oval(2, 2, 20, 20, fill="#e6a000", outline="")
        self.state_label = ttk.Label(status, text="Đang khởi động...", font=("Segoe UI", 13, "bold"))
        self.state_label.grid(row=0, column=1, sticky="w")
        self.conn_label = ttk.Label(status, text="-", foreground="#555")
        self.conn_label.grid(row=1, column=1, sticky="w")
        self.detail_label = ttk.Label(status, text="", foreground="#555", wraplength=560)
        self.detail_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        actions = ttk.Frame(status)
        actions.grid(row=3, column=0, columnspan=2, sticky="we", pady=(10, 0))
        ttk.Button(actions, text="Kết nối ngay", command=self.c.connect_now).pack(side="left")
        ttk.Button(actions, text="Ngắt kết nối", command=self.c.disconnect).pack(side="left", padx=6)
        self.unlock_button = ttk.Button(actions, text="Bỏ khóa", command=self.c.unlock)
        self.unlock_button.pack(side="left")
        self.auto = tk.BooleanVar(value=not self.c.watchdog.paused)
        ttk.Checkbutton(actions, text="Tự động kết nối khi có mạng", variable=self.auto,
                        command=lambda: self.c.set_auto(self.auto.get())).pack(side="right")

        # configuration
        form = ttk.LabelFrame(outer, text="Cấu hình", padding=10)
        form.grid(row=1, column=0, sticky="we", pady=10)
        form.columnconfigure(1, weight=1)
        self.backend = tk.StringVar()
        self.profile = tk.StringVar()
        self.server = tk.StringVar()
        self.group = tk.StringVar()
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.pin = tk.StringVar()
        self.save_settings = tk.BooleanVar()

        ttk.Label(form, text="Loại VPN").grid(row=0, column=0, sticky="w", **pad)
        self.backend_box = ttk.Combobox(form, textvariable=self.backend, state="readonly",
                                        values=list(BACKEND_DISPLAY.values()))
        self.backend_box.grid(row=0, column=1, sticky="we", pady=6)
        self.backend_box.bind("<<ComboboxSelected>>", lambda _: self._apply_backend())

        # Pulse-only: connection name
        self.profile_label = ttk.Label(form, text="Kết nối Pulse")
        self.profile_label.grid(row=1, column=0, sticky="w", **pad)
        self.profile_box = ttk.Combobox(form, textvariable=self.profile)
        self.profile_box.grid(row=1, column=1, sticky="we", pady=6)
        self.profile_refresh = ttk.Button(form, text="Làm mới", command=self._refresh_profiles)
        self.profile_refresh.grid(row=1, column=2, padx=(6, 0))

        self.server_label = ttk.Label(form, text="Gateway VPN")
        self.server_label.grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(form, textvariable=self.server).grid(row=2, column=1, sticky="we", pady=6)
        self.server_hint = ttk.Label(form, text="vd: btcvpn.mof.gov.vn", foreground="#777")
        self.server_hint.grid(row=2, column=2, sticky="w", padx=(6, 0))

        # Cisco-only: tunnel group
        self.group_label = ttk.Label(form, text="Nhóm (group)")
        self.group_label.grid(row=3, column=0, sticky="w", **pad)
        self.group_entry = ttk.Entry(form, textvariable=self.group)
        self.group_entry.grid(row=3, column=1, sticky="we", pady=6)
        self.group_hint = ttk.Label(form, text="vd: Cert-kyso", foreground="#777")
        self.group_hint.grid(row=3, column=2, sticky="w", padx=(6, 0))

        ttk.Label(form, text="Tài khoản VPN").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(form, textvariable=self.username).grid(row=4, column=1, sticky="we", pady=6)

        self.password_hint = self._secret_row(form, 5, "Mật khẩu VPN", self.password)
        self.pin_hint = self._secret_row(form, 6, "PIN token FPT-CA", self.pin)

        self.save_box = ttk.Checkbutton(form, text="Tích \"Save settings\" trong hộp thoại đăng nhập Pulse",
                                        variable=self.save_settings)
        self.save_box.grid(row=7, column=1, columnspan=2, sticky="w", pady=6)

        self.logoff_others = tk.BooleanVar()
        self.block_rdp = tk.BooleanVar()
        ttk.Label(form, text="Trước khi kết nối").grid(row=8, column=0, sticky="w", **pad)
        ttk.Checkbutton(form, text="Đăng xuất các phiên Windows khác (giữ Administrator và tài khoản này)",
                        variable=self.logoff_others).grid(row=8, column=1, columnspan=2, sticky="w", pady=6)
        ttk.Checkbutton(form, text="Tắt Remote Desktop, bật lại khi VPN kết nối xong",
                        variable=self.block_rdp).grid(row=9, column=1, columnspan=2, sticky="w", pady=6)

        ttk.Label(form, foreground="#777", wraplength=560,
                  text="Để trống mật khẩu/PIN = giữ giá trị đã lưu. PIN và mật khẩu được mã hóa "
                       "bằng Windows DPAPI, chỉ tài khoản Windows này trên máy này đọc được.",
                  ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(2, 8))

        buttons = ttk.Frame(form)
        buttons.grid(row=11, column=0, columnspan=3, sticky="we")
        ttk.Button(buttons, text="Lưu cấu hình", command=self._save).pack(side="left")
        ttk.Button(buttons, text="Nhập từ file .ini...", command=self._import_ini).pack(side="left", padx=6)
        ttk.Button(buttons, text="Xuất file mẫu .ini...", command=self._export_ini).pack(side="left")

        # log
        logs = ttk.LabelFrame(outer, text="Nhật ký", padding=10)
        logs.grid(row=2, column=0, sticky="nsew")
        logs.columnconfigure(0, weight=1)
        logs.rowconfigure(0, weight=1)
        self.log_text = tk.Text(logs, height=10, wrap="none", font=("Consolas", 9), state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(logs, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        log_buttons = ttk.Frame(logs)
        log_buttons.grid(row=1, column=0, columnspan=2, sticky="we", pady=(8, 0))
        ttk.Button(log_buttons, text="Mở ứng dụng VPN", command=self.c.open_client).pack(side="left")
        ttk.Button(log_buttons, text="Mở thư mục dữ liệu",
                   command=lambda: os.startfile(config_mod.app_dir())).pack(side="left", padx=6)

        footer = ttk.Frame(outer)
        footer.grid(row=3, column=0, sticky="we", pady=(8, 0))
        ttk.Label(footer, foreground="#777",
                  text="Đóng cửa sổ = thu xuống khay, tool vẫn chạy.").pack(side="left")
        ttk.Button(footer, text="Thoát hẳn", command=self._confirm_quit).pack(side="right")

    def _secret_row(self, form, row, label, var):
        ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=10, pady=6)
        entry = ttk.Entry(form, textvariable=var, show=MASK)
        entry.grid(row=row, column=1, sticky="we", pady=6)
        side = ttk.Frame(form)
        side.grid(row=row, column=2, sticky="w", padx=(6, 0))
        shown = tk.BooleanVar(value=False)
        ttk.Checkbutton(side, text="Hiện", variable=shown,
                        command=lambda: entry.configure(show="" if shown.get() else MASK)).pack(side="left")
        hint = ttk.Label(side, foreground="#777")
        hint.pack(side="left", padx=(6, 0))
        return hint

    # --- form --------------------------------------------------------------

    def _refresh_profiles(self):
        self.profile_box.configure(values=self.c.profiles())

    def _current_backend(self):
        return DISPLAY_BACKEND.get(self.backend.get(), config_mod.BACKEND_PULSE)

    def _apply_backend(self):
        """Show only the fields the chosen backend needs."""
        cisco = self._current_backend() == config_mod.BACKEND_CISCO
        if cisco:  # gateway/group are fixed defaults for Cisco
            if not self.server.get().strip():
                self.server.set(config_mod.CISCO_DEFAULT_SERVER)
            if not self.group.get().strip():
                self.group.set(config_mod.CISCO_DEFAULT_GROUP)
        pulse_rows = (self.profile_label, self.profile_box, self.profile_refresh, self.save_box)
        cisco_rows = (self.group_label, self.group_entry, self.group_hint)
        for widget in pulse_rows:
            widget.grid() if not cisco else widget.grid_remove()
        for widget in cisco_rows:
            widget.grid() if cisco else widget.grid_remove()
        self.server_label.configure(text="Gateway VPN" if cisco else "Server VPN")
        self.server_hint.configure(text="vd: btcvpn.mof.gov.vn" if cisco else "(tìm kết nối theo server)")

    def _load_form(self):
        cfg = self.c.config
        self._refresh_profiles()
        self.backend.set(BACKEND_DISPLAY.get(cfg.backend, BACKEND_DISPLAY[config_mod.BACKEND_PULSE]))
        self.profile.set(cfg.profile)
        self.server.set(cfg.server)
        self.group.set(cfg.group)
        self.username.set(cfg.username)
        self.password.set("")
        self.pin.set("")
        self.save_settings.set(cfg.save_settings)
        self.logoff_others.set(cfg.logoff_others)
        self.block_rdp.set(cfg.block_rdp)
        self.password_hint.configure(text="đã lưu" if cfg.password else "chưa có")
        self.pin_hint.configure(text="đã lưu" if cfg.pin else "chưa có")
        self._apply_backend()

    def _save(self):
        old = self.c.config
        cfg = replace(old, backend=self._current_backend(), profile=self.profile.get().strip(),
                      server=self.server.get().strip(), group=self.group.get().strip(),
                      username=self.username.get().strip(), password=self.password.get() or old.password,
                      pin=self.pin.get() or old.pin, save_settings=self.save_settings.get(),
                      logoff_others=self.logoff_others.get(), block_rdp=self.block_rdp.get())
        if not cfg.is_complete():
            need = "Gateway, tài khoản, mật khẩu và PIN." if cfg.backend == config_mod.BACKEND_CISCO \
                else "kết nối Pulse hoặc Server, tài khoản, mật khẩu và PIN."
            messagebox.showerror("VPN Auto", f"Cần có {need}", parent=self.root)
            return
        self.c.save_config(cfg)
        self._load_form()
        messagebox.showinfo("VPN Auto", "Đã lưu cấu hình.", parent=self.root)

    def _import_ini(self):
        path = filedialog.askopenfilename(parent=self.root, title="Chọn file cấu hình",
                                          filetypes=[("Cấu hình VPN Auto", "*.ini"), ("Tất cả", "*.*")])
        if not path:
            return
        try:
            keys = self.c.import_ini(path)
        except Exception as exc:
            messagebox.showerror("VPN Auto", f"Không nhập được file:\n{exc}", parent=self.root)
            return
        self._load_form()
        wiped = " Đã xóa mật khẩu/PIN khỏi file." if {"password", "pin"} & set(keys) else ""
        messagebox.showinfo("VPN Auto", f"Đã nhập: {', '.join(keys) or 'không có giá trị'}.{wiped}", parent=self.root)

    def _export_ini(self):
        path = filedialog.asksaveasfilename(parent=self.root, title="Lưu file mẫu", initialfile=deploy.FILE_NAME,
                                            defaultextension=".ini", filetypes=[("INI", "*.ini")])
        if not path:
            return
        deploy.write_template(path, self.c.config)
        messagebox.showinfo("VPN Auto", "Đã xuất file mẫu (không chứa mật khẩu/PIN).\n"
                            "Điền password và pin, rồi đặt file cạnh VpnAuto.exe trên máy cần cài.",
                            parent=self.root)

    # --- status and log ----------------------------------------------------

    def _load_log_tail(self):
        path = config_mod.log_path()
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
            self._append(lines)

    def _append(self, lines):
        self.log_text.configure(state="normal")
        for line in lines:
            self.log_text.insert("end", line + "\n")
        extra = int(self.log_text.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if extra > 0:
            self.log_text.delete("1.0", f"{extra}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _poll(self):
        while not self.commands.empty():
            self.commands.get_nowait()()
        if self._closed:
            return
        snapshot, dog = self.c.snapshot, self.c.watchdog
        self.dot.itemconfigure(self.dot_item, fill=color_for(snapshot.state, dog.paused))
        self.state_label.configure(text=LABELS.get(snapshot.state, snapshot.state))
        current = dog.current
        self.conn_label.configure(text=f"{current.name}  →  {current.server}" if current else "-")
        self.detail_label.configure(text=snapshot.detail)
        if self.auto.get() == dog.paused:
            self.auto.set(not dog.paused)
        self.unlock_button.configure(state="normal" if dog.locked_reason else "disabled")
        lines = []
        while not self.log_lines.empty():
            lines.append(self.log_lines.get_nowait())
        if lines:
            self._append(lines)
        self.root.after(POLL_MS, self._poll)

    # --- window ------------------------------------------------------------

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def hide(self):
        self.root.withdraw()
        if not self._hidden_once:
            self._hidden_once = True
            self.c.notify("VPN Auto vẫn đang chạy", "Mở lại từ icon dưới khay hệ thống.")

    def _confirm_quit(self):
        if messagebox.askyesno("VPN Auto", "Thoát hẳn? VPN sẽ không được tự kết nối lại nữa.", parent=self.root):
            self.quit()

    def quit(self):
        self._closed = True
        self.root.destroy()
