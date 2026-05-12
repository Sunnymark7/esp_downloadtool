import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import zipfile
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "ESP32 一键下载工具"
AUTO_PORT_LABEL = "自动识别串口"
FAST_AUTO_BAUD_LABEL = "快速自动"
USER_MODE = "用户模式"
DEVELOPER_MODE = "开发者模式"
BAUD_CANDIDATES = ("921600", "460800", "230400", "115200")
ROLE_BOOTLOADER = "Bootloader"
ROLE_PARTITION = "Partition"
ROLE_APP = "App"
ROLE_OTHER = "Other"
DEFAULT_BOOTLOADER_OFFSET = "0x0"
DEFAULT_PARTITION_OFFSET = "0x8000"
DEFAULT_APP_OFFSET = "0x10000"


def resource_path(relative_path):
    base_path = getattr(sys, "_MEIPASS", os.path.abspath(os.path.dirname(__file__)))
    return os.path.join(base_path, relative_path)


def esptool_command_prefix():
    if getattr(sys, "frozen", False):
        helper_path = os.path.join(os.path.dirname(sys.executable), "esptool_runner.exe")
        return [helper_path]
    return [sys.executable, "-m", "esptool"]


@dataclass
class FlashFile:
    role: str
    offset: str
    path: str


def list_serial_ports():
    ports = []
    try:
        from serial.tools import list_ports

        for port in list_ports.comports():
            label = port.device
            if port.description and port.description != "n/a":
                label = f"{port.device} - {port.description}"
            ports.append(label)
    except Exception:
        pass

    if ports:
        return ports

    if os.name == "nt":
        try:
            import winreg

            key_path = r"HARDWARE\DEVICEMAP\SERIALCOMM"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                index = 0
                while True:
                    try:
                        _, value, _ = winreg.EnumValue(key, index)
                        ports.append(value)
                        index += 1
                    except OSError:
                        break
        except Exception:
            pass
    return sorted(set(ports))


def extract_port_name(label):
    return label.split(" - ", 1)[0].strip()


def choose_best_port(port_labels):
    if not port_labels:
        return "", ""
    if len(port_labels) == 1:
        return extract_port_name(port_labels[0]), port_labels[0]

    keywords = (
        "cp210",
        "ch340",
        "ch341",
        "wch",
        "silicon labs",
        "ftdi",
        "usb serial",
        "usb-uart",
        "uart",
        "jtag",
        "esp",
    )
    scored = []
    for label in port_labels:
        lowered = label.lower()
        score = 0
        for index, keyword in enumerate(keywords):
            if keyword in lowered:
                score += 100 - index
        if "bluetooth" in lowered:
            score -= 200
        if "com" in lowered:
            score += 5
        scored.append((score, label))
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    return extract_port_name(scored[0][1]), scored[0][1]


def normalize_offset(value):
    text = value.strip().lower()
    if not text:
        raise ValueError("烧录地址不能为空")
    if text.startswith("0x"):
        int(text, 16)
        return text
    int(text, 10)
    return hex(int(text, 10))


def safe_extract_zip(zip_path, target_dir):
    root = os.path.abspath(target_dir)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue

            member_name = member.filename.replace("\\", "/")
            if member_name.startswith("/") or ".." in member_name.split("/"):
                raise ValueError(f"压缩包包含不安全路径：{member.filename}")

            target_path = os.path.abspath(os.path.join(root, member_name))
            if not target_path.startswith(root + os.sep):
                raise ValueError(f"压缩包包含不安全路径：{member.filename}")

            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with archive.open(member) as source, open(target_path, "wb") as output:
                shutil.copyfileobj(source, output)


def guess_role(path):
    name = os.path.basename(path).lower()
    parent = os.path.basename(os.path.dirname(path)).lower()
    combined = f"{parent}/{name}"
    if "bootloader" in combined:
        return ROLE_BOOTLOADER
    if "partition-table" in combined or "partitions" in combined or "partition" in combined:
        return ROLE_PARTITION
    if name.endswith(".bin"):
        return ROLE_APP
    return ROLE_OTHER


def find_file_by_name(root_dir, file_name):
    expected = os.path.basename(file_name).lower()
    for folder, _, files in os.walk(root_dir):
        for name in files:
            if name.lower() == expected:
                return os.path.join(folder, name)
    return ""


def load_json_manifest(root_dir):
    for folder, _, files in os.walk(root_dir):
        for name in files:
            if name.lower() != "flasher_args.json":
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue

            flash_files = data.get("flash_files")
            if not isinstance(flash_files, dict):
                continue

            entries = []
            for offset, file_name in flash_files.items():
                full_path = os.path.abspath(os.path.join(folder, str(file_name)))
                if not os.path.isfile(full_path):
                    full_path = find_file_by_name(root_dir, str(file_name))
                if full_path and os.path.isfile(full_path):
                    entries.append(FlashFile(guess_role(full_path), normalize_offset(str(offset)), full_path))

            settings = data.get("flash_settings") if isinstance(data.get("flash_settings"), dict) else {}
            if "baud" in data and "baud" not in settings:
                settings["baud"] = data.get("baud")
            return entries, settings, path
    return [], {}, ""


def load_text_flash_args(root_dir):
    names = {"flash_args", "flash_args.txt", "download.config"}
    for folder, _, files in os.walk(root_dir):
        for name in files:
            if name.lower() not in names:
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    tokens = shlex.split(handle.read(), posix=False)
            except OSError:
                continue

            entries = []
            index = 0
            while index < len(tokens) - 1:
                token = tokens[index].strip("'\"")
                next_token = tokens[index + 1].strip("'\"")
                try:
                    offset = normalize_offset(token)
                except ValueError:
                    index += 1
                    continue

                if next_token.lower().endswith(".bin"):
                    full_path = os.path.abspath(os.path.join(folder, next_token))
                    if not os.path.isfile(full_path):
                        full_path = find_file_by_name(root_dir, next_token)
                    if full_path and os.path.isfile(full_path):
                        entries.append(FlashFile(guess_role(full_path), offset, full_path))
                    index += 2
                else:
                    index += 1

            if entries:
                return entries, path
    return [], ""


def infer_flash_files(root_dir):
    bin_files = []
    for folder, _, files in os.walk(root_dir):
        for name in files:
            if name.lower().endswith(".bin"):
                bin_files.append(os.path.join(folder, name))

    if not bin_files:
        return []

    selected = []
    bootloader = next((path for path in bin_files if guess_role(path) == ROLE_BOOTLOADER), "")
    partition = next((path for path in bin_files if guess_role(path) == ROLE_PARTITION), "")

    ignored = ("bootloader", "partition", "ota_data", "phy_init", "boot_app0")
    app_candidates = [
        path
        for path in bin_files
        if path not in (bootloader, partition)
        and not any(marker in os.path.basename(path).lower() for marker in ignored)
    ]
    app_candidates.sort(key=lambda item: os.path.getsize(item), reverse=True)

    if bootloader:
        selected.append(FlashFile(ROLE_BOOTLOADER, DEFAULT_BOOTLOADER_OFFSET, bootloader))
    if partition:
        selected.append(FlashFile(ROLE_PARTITION, DEFAULT_PARTITION_OFFSET, partition))
    if app_candidates:
        app_offset = DEFAULT_BOOTLOADER_OFFSET if not bootloader and not partition and len(app_candidates) == 1 else DEFAULT_APP_OFFSET
        selected.append(FlashFile(ROLE_APP, app_offset, app_candidates[0]))
    return selected


class FlashRow(ttk.Frame):
    def __init__(self, master, role="", offset="", path="", on_remove=None):
        super().__init__(master, style="Panel.TFrame")
        self.on_remove = on_remove
        self.role_var = tk.StringVar(value=role or ROLE_OTHER)
        self.offset_var = tk.StringVar(value=offset)
        self.path_var = tk.StringVar(value=path)

        ttk.Combobox(self, textvariable=self.role_var, values=(ROLE_BOOTLOADER, ROLE_PARTITION, ROLE_APP, ROLE_OTHER), width=12, state="readonly").grid(row=0, column=0, padx=(0, 8), sticky="ew")
        ttk.Entry(self, width=11, textvariable=self.offset_var).grid(row=0, column=1, padx=(0, 8), sticky="ew")
        ttk.Entry(self, textvariable=self.path_var).grid(row=0, column=2, padx=(0, 8), sticky="ew")
        ttk.Button(self, text="选择", width=7, command=self.choose_file).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(self, text="移除", width=7, command=self.remove).grid(row=0, column=4)
        self.columnconfigure(2, weight=1)

    def choose_file(self):
        path = filedialog.askopenfilename(title="选择 BIN 文件", filetypes=(("BIN files", "*.bin"), ("All files", "*.*")))
        if path:
            self.path_var.set(path)
            self.role_var.set(guess_role(path))
            if not self.offset_var.get().strip():
                self.offset_var.set(DEFAULT_BOOTLOADER_OFFSET)

    def remove(self):
        if self.on_remove:
            self.on_remove(self)

    def get_flash_file(self):
        return FlashFile(self.role_var.get().strip() or ROLE_OTHER, normalize_offset(self.offset_var.get()), self.path_var.get().strip())


class Esp32FlasherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        icon_path = resource_path(os.path.join("assets", "esp32_flasher.ico"))
        if os.path.exists(icon_path):
            self.iconbitmap(icon_path)
        self.geometry("760x560")
        self.minsize(680, 500)

        self.log_queue = queue.Queue()
        self.process = None
        self.worker = None
        self.rows = []
        self.package_temp_dir = ""
        self.flash_entries = []

        self.mode_var = tk.StringVar(value=USER_MODE)
        self.port_var = tk.StringVar(value=AUTO_PORT_LABEL)
        self.package_var = tk.StringVar(value="未选择固件包")
        self.firmware_summary_var = tk.StringVar(value="等待选择固件 ZIP")
        self.board_var = tk.StringVar(value="板子：自动识别")
        self.chip_var = tk.StringVar(value="auto")
        self.baud_var = tk.StringVar(value=FAST_AUTO_BAUD_LABEL)
        self.flash_mode_var = tk.StringVar(value="dio")
        self.flash_freq_var = tk.StringVar(value="40m")
        self.flash_size_var = tk.StringVar(value="detect")
        self.erase_var = tk.BooleanVar(value=False)
        self.after_flash_var = tk.StringVar(value="hard-reset")
        self.status_var = tk.StringVar(value="就绪")
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text_var = tk.StringVar(value="进度：0%")

        self.configure(bg="#eef3f8")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._configure_style()
        self._build_ui()
        self.refresh_ports()
        self.mode_var.trace_add("write", lambda *_: self.apply_mode())
        self.after(100, self.drain_log_queue)

    def _configure_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        style.configure("App.TFrame", background="#eef3f8")
        style.configure("Panel.TFrame", background="#ffffff")
        style.configure("Title.TLabel", background="#14324a", foreground="#ffffff", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("SubTitle.TLabel", background="#14324a", foreground="#c9d6e2")
        style.configure("Card.TLabelframe", background="#ffffff", bordercolor="#d6dee8", relief="solid")
        style.configure("Card.TLabelframe.Label", background="#ffffff", foreground="#18324a", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("TLabel", background="#ffffff", foreground="#263747")
        style.configure("Muted.TLabel", background="#ffffff", foreground="#718096")
        style.configure("Status.TLabel", background="#eef3f8", foreground="#52616f")
        style.configure("Primary.TButton", background="#0f766e", foreground="#ffffff", padding=(16, 10), borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#115e59"), ("disabled", "#9bb8b5")])
        style.configure("Danger.TButton", background="#b42318", foreground="#ffffff", padding=(12, 8), borderwidth=0)
        style.configure("TButton", padding=(10, 7), background="#e6edf5", foreground="#1f2d3d", borderwidth=0)
        style.configure("TCheckbutton", background="#ffffff", foreground="#263747")
        style.configure("TRadiobutton", background="#eef3f8", foreground="#263747")
        style.configure(
            "Green.Horizontal.TProgressbar",
            troughcolor="#dfe7ef",
            background="#16a34a",
            lightcolor="#16a34a",
            darkcolor="#15803d",
            bordercolor="#dfe7ef",
        )

    def _build_ui(self):
        root = ttk.Frame(self, padding=16, style="App.TFrame")
        root.pack(fill="both", expand=True)

        header = tk.Frame(root, bg="#14324a", padx=18, pady=14)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="选择固件包，自动识别板子和串口，快速下载", style="SubTitle.TLabel").pack(anchor="w", pady=(4, 0))

        mode_bar = ttk.Frame(root, style="App.TFrame")
        mode_bar.pack(fill="x", pady=(0, 10))
        ttk.Radiobutton(mode_bar, text="用户模式", variable=self.mode_var, value=USER_MODE).pack(side="left")
        ttk.Radiobutton(mode_bar, text="开发者模式", variable=self.mode_var, value=DEVELOPER_MODE).pack(side="left", padx=(16, 0))

        main = ttk.LabelFrame(root, text="下载", padding=14, style="Card.TLabelframe")
        main.pack(fill="x")

        ttk.Label(main, text="固件包").grid(row=0, column=0, sticky="w")
        ttk.Label(main, textvariable=self.package_var, style="Muted.TLabel", wraplength=520).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="选择固件 ZIP", command=self.choose_package, style="Primary.TButton").grid(row=1, column=2, sticky="ew", padx=(12, 0))

        ttk.Label(main, text="串口").grid(row=2, column=0, sticky="w")
        self.port_combo = ttk.Combobox(main, textvariable=self.port_var, state="readonly")
        self.port_combo.grid(row=3, column=0, sticky="ew", pady=(2, 10))
        ttk.Button(main, text="刷新", command=self.refresh_ports).grid(row=3, column=1, sticky="ew", padx=(8, 12), pady=(2, 10))
        self.flash_button = ttk.Button(main, text="开始下载", command=self.start_flash, style="Primary.TButton")
        self.flash_button.grid(row=3, column=2, sticky="ew", pady=(2, 10))

        ttk.Label(main, textvariable=self.firmware_summary_var, style="Muted.TLabel").grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(main, textvariable=self.board_var, style="Muted.TLabel").grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(main, textvariable=self.progress_text_var, style="Muted.TLabel").grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.progress_bar = ttk.Progressbar(main, variable=self.progress_var, maximum=100, style="Green.Horizontal.TProgressbar")
        self.progress_bar.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.stop_button = ttk.Button(main, text="停止", command=self.stop_flash, state="disabled", style="Danger.TButton")
        self.stop_button.grid(row=4, column=2, rowspan=2, sticky="nsew", pady=(2, 0))
        main.columnconfigure(0, weight=1)
        main.columnconfigure(2, weight=0)

        self.developer_frame = ttk.Frame(root, style="App.TFrame")
        self.developer_canvas = tk.Canvas(self.developer_frame, bg="#eef3f8", highlightthickness=0)
        self.developer_scrollbar = ttk.Scrollbar(self.developer_frame, orient="vertical", command=self.developer_canvas.yview)
        self.developer_content = ttk.Frame(self.developer_canvas, style="App.TFrame")
        self.developer_window = self.developer_canvas.create_window((0, 0), window=self.developer_content, anchor="nw")
        self.developer_canvas.configure(yscrollcommand=self.developer_scrollbar.set)
        self.developer_canvas.pack(side="left", fill="both", expand=True)
        self.developer_scrollbar.pack(side="right", fill="y")
        self.developer_content.bind("<Configure>", self.update_developer_scrollregion)
        self.developer_canvas.bind("<Configure>", self.resize_developer_window)
        self.developer_canvas.bind("<MouseWheel>", self.on_developer_mousewheel)
        self.developer_content.bind("<MouseWheel>", self.on_developer_mousewheel)
        self._build_developer_ui(self.developer_content)

        log_frame = ttk.LabelFrame(root, text="状态日志", padding=8, style="Card.TLabelframe")
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = tk.Text(log_frame, height=8, wrap="word", state="disabled", bg="#101820", fg="#d8e5ee", relief="flat", padx=10, pady=10, font=("Consolas", 10))
        self.log_text.pack(fill="both", expand=True)
        ttk.Label(root, textvariable=self.status_var, anchor="w", style="Status.TLabel").pack(fill="x", pady=(8, 0))
        self.apply_mode()

    def _build_developer_ui(self, parent):
        settings = ttk.LabelFrame(parent, text="开发者参数", padding=12, style="Card.TLabelframe")
        settings.pack(fill="x", pady=(0, 10))
        labels = ("芯片", "波特率", "Flash 模式", "Flash 频率", "Flash 大小", "烧录后")
        for col, label in enumerate(labels):
            ttk.Label(settings, text=label).grid(row=0, column=col, sticky="w", padx=(0, 8))
        ttk.Combobox(settings, textvariable=self.chip_var, values=("auto", "esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c2", "esp32c6", "esp32h2"), state="readonly", width=10).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Combobox(settings, textvariable=self.baud_var, values=(FAST_AUTO_BAUD_LABEL, "1500000", "921600", "460800", "230400", "115200"), state="readonly", width=12).grid(row=1, column=1, sticky="ew", padx=(0, 8))
        ttk.Combobox(settings, textvariable=self.flash_mode_var, values=("dio", "qio", "dout", "qout", "keep"), state="readonly", width=10).grid(row=1, column=2, sticky="ew", padx=(0, 8))
        ttk.Combobox(settings, textvariable=self.flash_freq_var, values=("40m", "80m", "60m", "48m", "30m", "26m", "20m", "keep"), state="readonly", width=10).grid(row=1, column=3, sticky="ew", padx=(0, 8))
        ttk.Combobox(settings, textvariable=self.flash_size_var, values=("detect", "2MB", "4MB", "8MB", "16MB", "32MB", "64MB", "128MB", "keep"), state="readonly", width=10).grid(row=1, column=4, sticky="ew", padx=(0, 8))
        ttk.Combobox(settings, textvariable=self.after_flash_var, values=("hard-reset", "soft-reset", "no-reset", "no-reset-stub"), state="readonly", width=12).grid(row=1, column=5, sticky="ew")
        ttk.Checkbutton(settings, text="烧录前擦除整片 Flash", variable=self.erase_var).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        files = ttk.LabelFrame(parent, text="烧录文件", padding=12, style="Card.TLabelframe")
        files.pack(fill="both", expand=True)
        header = ttk.Frame(files, style="Panel.TFrame")
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(header, text="类型", width=12).grid(row=0, column=0, padx=(0, 8), sticky="w")
        ttk.Label(header, text="地址", width=11).grid(row=0, column=1, padx=(0, 8), sticky="w")
        ttk.Label(header, text="文件路径").grid(row=0, column=2, sticky="w")
        header.columnconfigure(2, weight=1)
        self.rows_frame = ttk.Frame(files, style="Panel.TFrame")
        self.rows_frame.pack(fill="both", expand=True)
        file_buttons = ttk.Frame(files, style="Panel.TFrame")
        file_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(file_buttons, text="添加文件", command=self.add_row).pack(side="left")
        ttk.Button(file_buttons, text="清空文件", command=self.clear_rows).pack(side="left", padx=(8, 0))
        ttk.Button(file_buttons, text="清空日志", command=self.clear_log).pack(side="right")

    def update_developer_scrollregion(self, _event=None):
        self.developer_canvas.configure(scrollregion=self.developer_canvas.bbox("all"))

    def resize_developer_window(self, event):
        self.developer_canvas.itemconfigure(self.developer_window, width=event.width)

    def bind_developer_mousewheel(self, _event=None):
        return

    def unbind_developer_mousewheel(self, _event=None):
        return

    def on_developer_mousewheel(self, event):
        if self.mode_var.get() == DEVELOPER_MODE:
            self.developer_canvas.yview_scroll(int(-event.delta / 120), "units")

    def apply_mode(self):
        if self.mode_var.get() == DEVELOPER_MODE:
            self.developer_frame.pack(fill="both", expand=True, pady=(12, 0), before=self.log_text.master)
            self.log_text.configure(height=12, wrap="none")
        else:
            self.developer_frame.pack_forget()
            self.unbind_developer_mousewheel()
            self.log_text.configure(height=7, wrap="word")

    def refresh_ports(self):
        ports = list_serial_ports()
        current = self.port_var.get()
        values = [AUTO_PORT_LABEL] + ports
        self.port_combo["values"] = values
        self.port_var.set(current if current in values else AUTO_PORT_LABEL)
        self.status_var.set(f"发现 {len(ports)} 个串口" if ports else "未发现串口")

    def choose_package(self):
        path = filedialog.askopenfilename(title="选择固件压缩包", filetypes=(("ZIP archives", "*.zip"), ("All files", "*.*")))
        if path:
            self.load_package(path)

    def load_package(self, zip_path):
        if not zipfile.is_zipfile(zip_path):
            messagebox.showerror("固件包错误", "请选择有效的 ZIP 压缩包")
            return

        self.cleanup_package_dir()
        self.package_temp_dir = tempfile.mkdtemp(prefix="esp32_firmware_")
        try:
            safe_extract_zip(zip_path, self.package_temp_dir)
            entries, settings, manifest_path = load_json_manifest(self.package_temp_dir)
            source = "flasher_args.json"
            if not entries:
                entries, flash_args_path = load_text_flash_args(self.package_temp_dir)
                source = os.path.basename(flash_args_path) if flash_args_path else "文件名规则"
            if not entries:
                entries = infer_flash_files(self.package_temp_dir)
                source = "文件名规则"
            if not entries:
                raise ValueError("压缩包中没有识别到可烧录的 .bin 文件")

            self.apply_flash_settings(settings)
            self.package_var.set(os.path.normpath(zip_path))
            self.set_rows(entries)
            self.clear_log()
            self.append_log(f"固件已识别：{source}\n")
            if manifest_path:
                self.append_log(f"配置文件：{manifest_path}\n")
            self.append_log(self.firmware_summary(entries) + "\n")
            self.status_var.set("固件识别完成")
        except Exception as exc:
            self.cleanup_package_dir()
            messagebox.showerror("加载失败", str(exc))

    def apply_flash_settings(self, settings):
        if not settings:
            return
        if settings.get("flash_mode"):
            self.flash_mode_var.set(str(settings["flash_mode"]))
        if settings.get("flash_freq"):
            self.flash_freq_var.set(str(settings["flash_freq"]))
        if settings.get("flash_size"):
            self.flash_size_var.set(str(settings["flash_size"]))
        if settings.get("baud") and str(settings["baud"]).isdigit():
            self.baud_var.set(str(settings["baud"]))

    def firmware_summary(self, entries):
        names = {item.role: os.path.basename(item.path) for item in entries}
        parts = []
        if ROLE_BOOTLOADER in names:
            parts.append("Bootloader")
        if ROLE_PARTITION in names:
            parts.append("分区表")
        if ROLE_APP in names:
            parts.append("主程序")
        return f"已识别 {len(entries)} 个文件：" + "、".join(parts or ["BIN"])

    def clear_rows(self):
        for row in list(self.rows):
            row.destroy()
        self.rows = []
        self.flash_entries = []
        self.firmware_summary_var.set("等待选择固件 ZIP")

    def set_rows(self, entries):
        self.clear_rows()
        self.flash_entries = entries
        self.firmware_summary_var.set(self.firmware_summary(entries))
        for item in entries:
            self.add_row(item.role, item.offset, item.path, update_entries=False)

    def add_row(self, role="", offset=DEFAULT_BOOTLOADER_OFFSET, path="", update_entries=True):
        row = FlashRow(self.rows_frame, role=role, offset=offset, path=path, on_remove=self.remove_row)
        row.pack(fill="x", pady=3)
        self.rows.append(row)
        if update_entries:
            self.flash_entries = self.collect_flash_files(allow_empty=True)

    def remove_row(self, row):
        if row in self.rows:
            self.rows.remove(row)
        row.destroy()
        self.flash_entries = self.collect_flash_files(allow_empty=True)

    def collect_flash_files(self, allow_empty=False):
        files = []
        for row in self.rows:
            flash_file = row.get_flash_file()
            if not flash_file.path:
                continue
            if not os.path.isfile(flash_file.path):
                if allow_empty:
                    continue
                raise ValueError(f"文件不存在：{flash_file.path}")
            files.append(flash_file)
        if not files and not allow_empty:
            raise ValueError("请先选择固件 ZIP")
        return files

    def append_log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def set_running(self, running):
        self.flash_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def snapshot_settings(self):
        return {
            "chip": self.chip_var.get(),
            "baud": self.baud_var.get(),
            "flash_mode": self.flash_mode_var.get(),
            "flash_freq": self.flash_freq_var.get(),
            "flash_size": self.flash_size_var.get(),
            "after_flash": self.after_flash_var.get(),
            "erase": self.erase_var.get(),
            "developer_mode": self.mode_var.get() == DEVELOPER_MODE,
        }

    def start_flash(self):
        try:
            files = self.collect_flash_files()
            self.ensure_packaged_runtime()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        self.clear_log()
        self.progress_var.set(0)
        self.progress_text_var.set("进度：0% - 准备下载")
        self.set_running(True)
        self.board_var.set("板子：自动识别中")
        self.status_var.set("正在准备下载...")
        self.worker = threading.Thread(
            target=self.run_flash_workflow,
            args=(self.port_var.get(), self.snapshot_settings(), files),
            daemon=True,
        )
        self.worker.start()

    def ensure_packaged_runtime(self):
        if not getattr(sys, "frozen", False):
            return
        helper_path = esptool_command_prefix()[0]
        if not os.path.exists(helper_path):
            raise ValueError("缺少 esptool_runner.exe。请把 ESP32Flasher.exe 和 esptool_runner.exe 放在同一目录后再运行。")

    def run_flash_workflow(self, port_setting, settings, files):
        port = ""
        self.emit_progress(2, "准备下载")
        if port_setting == AUTO_PORT_LABEL or not port_setting:
            self.emit_progress(4, "正在识别串口")
            self.log_queue.put(("log", "自动识别串口...\n"))
            port_labels = list_serial_ports()
            port, port_label = choose_best_port(port_labels)
            if not port:
                self.log_queue.put(("error", "未发现可用串口，请检查 USB 连接和驱动。\n"))
                self.log_queue.put(("done", 1))
                return
            self.log_queue.put(("port", port_label))
            self.log_queue.put(("log", f"串口：{port_label}\n"))
            self.emit_progress(7, "串口已识别")
        else:
            port = extract_port_name(port_setting)
            self.log_queue.put(("log", f"串口：{port_setting}\n"))
            self.emit_progress(7, "串口已选择")

        bauds = BAUD_CANDIDATES if settings["baud"] == FAST_AUTO_BAUD_LABEL else (settings["baud"],)
        for index, baud in enumerate(bauds):
            if index > 0:
                self.emit_progress(0, "准备降速重试")
            self.emit_progress(8, f"准备连接 {baud}")
            if settings["baud"] == FAST_AUTO_BAUD_LABEL:
                self.log_queue.put(("baud", baud))
                self.log_queue.put(("log", f"尝试高速下载：{baud}\n"))
            code = self.run_flash_once(port, baud, settings, files)
            if code == 0:
                self.log_queue.put(("done", 0))
                return
            if settings["baud"] != FAST_AUTO_BAUD_LABEL:
                self.log_queue.put(("done", code))
                return
            if index < len(bauds) - 1:
                self.log_queue.put(("log", "本次未成功，自动降速重试。\n\n"))

        self.log_queue.put(("done", 1))

    def emit_progress(self, value, status):
        self.log_queue.put(("progress", (max(0, min(100, int(value))), status)))

    def run_flash_once(self, port, baud, settings, files):
        commands = self.build_commands(port, baud, settings, files)
        for cmd in commands:
            if settings["developer_mode"]:
                self.log_queue.put(("log", " ".join(f'"{item}"' if " " in item else item for item in cmd) + "\n"))
            if "erase-flash" in cmd:
                code = self.run_command(cmd, 10, 20, "正在擦除 Flash")
            else:
                start = 20 if settings["erase"] else 10
                code = self.run_command(cmd, start, 98, "正在写入固件", files)
            if code != 0:
                return code
        return 0

    def build_base_command(self, port, baud, settings):
        return [
            *esptool_command_prefix(),
            "--chip",
            settings["chip"],
            "--port",
            port,
            "--baud",
            baud,
            "--connect-attempts",
            "5",
            "--after",
            settings["after_flash"],
        ]

    def build_commands(self, port, baud, settings, files):
        base = self.build_base_command(port, baud, settings)
        commands = []
        if settings["erase"]:
            commands.append(base + ["erase-flash"])
        write_cmd = base + ["write-flash", "--flash-mode", settings["flash_mode"], "--flash-freq", settings["flash_freq"], "--flash-size", settings["flash_size"]]
        for item in files:
            write_cmd.extend([item.offset, item.path])
        commands.append(write_cmd)
        return commands

    def run_command(self, cmd, progress_start=0, progress_end=100, default_status="正在执行", files=None):
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except Exception as exc:
            self.log_queue.put(("error", f"启动 esptool 失败：{exc}\n"))
            return 1

        self.emit_progress(progress_start, default_status)
        last_progress = progress_start
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.log_queue.put(("log", line))
            board = self.extract_board_name(line)
            if board:
                self.log_queue.put(("board", board))
                self.emit_progress(max(last_progress, progress_start + 3), "已识别板子")
                last_progress = max(last_progress, progress_start + 3)
            status = self.extract_line_status(line, default_status)
            progress = self.extract_progress(line)
            address_progress = self.extract_address_progress(line, files) if files else None
            if address_progress is not None:
                mapped = progress_start + (progress_end - progress_start) * address_progress / 100
                last_progress = max(last_progress, mapped)
                self.emit_progress(last_progress, status)
            elif progress is not None:
                mapped = progress_start + (progress_end - progress_start) * progress / 100
                last_progress = max(last_progress, mapped)
                self.emit_progress(last_progress, status)
            elif status != default_status:
                self.emit_progress(last_progress, status)
        code = self.process.wait()
        if code == 0:
            self.emit_progress(progress_end, "正在收尾")
        self.process = None
        return code

    def extract_board_name(self, line):
        text = line.strip()
        if text.startswith("Chip is "):
            return text.replace("Chip is ", "", 1)
        if "Detecting chip type..." in text:
            detected = text.split("Detecting chip type...", 1)[-1].strip()
            if detected:
                return detected
        return ""

    def extract_progress(self, line):
        match = re.search(r"\((\d{1,3})\s*%\)", line)
        if not match:
            match = re.search(r"\b(\d{1,3})\s*%", line)
        if not match:
            return None
        value = int(match.group(1))
        return max(0, min(100, value))

    def extract_address_progress(self, line, files):
        match = re.search(r"Writing at 0x([0-9a-fA-F]+)", line)
        if not match:
            return None
        address = int(match.group(1), 16)
        segments = []
        for item in files:
            try:
                offset = int(item.offset, 16)
                size = os.path.getsize(item.path)
            except (OSError, ValueError):
                continue
            segments.append((offset, size))
        if not segments:
            return None
        segments.sort(key=lambda part: part[0])
        total_size = sum(size for _, size in segments)
        if total_size <= 0:
            return None

        written = 0
        for offset, size in segments:
            if address >= offset + size:
                written += size
            elif offset <= address < offset + size:
                written += max(0, address - offset)
                break
            elif address < offset:
                break
        return max(0, min(100, int(written * 100 / total_size)))

    def extract_line_status(self, line, default_status):
        text = line.strip()
        lowered = text.lower()
        if "connecting" in lowered:
            return "正在连接板子"
        if "detecting chip type" in lowered or text.startswith("Chip is "):
            return "正在识别板子"
        if "stub running" in lowered:
            return "下载器已启动"
        if "erasing" in lowered:
            return "正在擦除 Flash"
        if "writing at" in lowered or "wrote" in lowered:
            return "正在写入固件"
        if "hash of data verified" in lowered or "verified" in lowered:
            return "正在校验固件"
        if "hard resetting" in lowered or "resetting" in lowered:
            return "正在复位板子"
        if "leaving" in lowered:
            return "正在完成下载"
        return default_status

    def stop_flash(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            time.sleep(0.2)
            if self.process.poll() is None:
                self.process.kill()
        self.append_log("\n已停止。\n")
        self.status_var.set("已停止")
        self.progress_var.set(0)
        self.set_running(False)

    def drain_log_queue(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind in ("log", "error"):
                    self.append_log(payload)
                elif kind == "port":
                    values = list(self.port_combo["values"])
                    if payload not in values:
                        self.port_combo["values"] = [AUTO_PORT_LABEL, payload] + values[1:]
                    self.port_var.set(payload)
                elif kind == "baud":
                    self.baud_var.set(payload)
                elif kind == "board":
                    self.board_var.set(f"板子：{payload}")
                elif kind == "progress":
                    if isinstance(payload, tuple):
                        value, text = payload
                    else:
                        value, text = payload, ""
                    value = max(0, min(100, int(value)))
                    if value >= self.progress_var.get() or value == 0:
                        self.progress_var.set(value)
                    if text:
                        self.progress_text_var.set(f"进度：{value}% - {text}")
                        self.status_var.set(text)
                elif kind == "done":
                    self.process = None
                    self.set_running(False)
                    if payload == 0:
                        self.status_var.set("下载完成")
                        self.progress_var.set(100)
                        self.progress_text_var.set("进度：100% - 下载完成")
                        self.append_log("\n下载完成。\n")
                        messagebox.showinfo("下载完成", "固件下载完成。")
                    else:
                        self.status_var.set("下载失败")
                        self.progress_var.set(0)
                        self.progress_text_var.set("进度：0% - 下载失败")
                        self.append_log("\n下载失败，请按住 BOOT 键或降低波特率重试。\n")
                        messagebox.showerror("下载失败", "固件下载失败，请按住 BOOT 键或降低波特率重试。")
        except queue.Empty:
            pass
        self.after(100, self.drain_log_queue)

    def cleanup_package_dir(self):
        if self.package_temp_dir and os.path.isdir(self.package_temp_dir):
            shutil.rmtree(self.package_temp_dir, ignore_errors=True)
        self.package_temp_dir = ""

    def on_close(self):
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("确认退出", "下载正在进行，确定要停止并退出吗？"):
                return
            self.stop_flash()
        self.cleanup_package_dir()
        self.destroy()


if __name__ == "__main__":
    app = Esp32FlasherApp()
    app.mainloop()
