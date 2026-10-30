"""主程序中的批量 OTA 页面。网络和升级工作在后台线程执行。"""
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from batch_ota import current_profile, discover_devices, image_identity, run_batch


class OtaPage(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12, style="App.TFrame")
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.firmware = tk.StringVar(value="未选择升级固件")
        self.version = tk.StringVar(value="选择 firmware.bin 后显示版本与兼容信息")
        self.prefix = tk.StringVar(value="Yshand_")
        self.restore = tk.StringVar(value=current_profile())
        self.status = tk.StringVar(value="扫描附近设备，勾选后即可批量升级。")
        self.progress = tk.DoubleVar()
        self.ssids = []
        self.scanning = False
        self._build()
        self.after(100, self._drain)

    def _build(self):
        from layout import ScrollableContent, wrapping_label
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.scroll = ScrollableContent(self)
        self.scroll.grid(row=0, column=0, sticky="nsew")
        body = self.scroll.content
        body.columnconfigure(0, weight=1)
        wrapping_label(body, text="自动切换设备热点，逐台上传并验证，无需手动配网。", style="Status.TLabel").grid(row=0, column=0, sticky="ew", pady=(0, 12))
        firmware = ttk.LabelFrame(body, text="升级固件", padding=12, style="Card.TLabelframe")
        firmware.grid(row=1, column=0, sticky="ew")
        firmware.columnconfigure(0, weight=1)
        ttk.Label(firmware, text="拖入 BIN，或点击选择").grid(row=0, column=0, sticky="w")
        self.choose_button = ttk.Button(firmware, text="选择 BIN", command=self.choose_firmware)
        self.choose_button.grid(row=0, column=1, padx=(10, 0))
        wrapping_label(firmware, textvariable=self.firmware, style="Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 4))
        wrapping_label(firmware, textvariable=self.version, style="Muted.TLabel").grid(row=2, column=0, columnspan=2, sticky="ew")
        devices = ttk.LabelFrame(body, text="设备列表", padding=12, style="Card.TLabelframe")
        devices.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        devices.columnconfigure(0, weight=1)
        tools = ttk.Frame(devices, style="Panel.TFrame")
        tools.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tools.columnconfigure(1, weight=1)
        ttk.Label(tools, text="热点前缀").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(tools, textvariable=self.prefix, width=10).grid(row=0, column=1, sticky="ew")
        self.scan_button = ttk.Button(tools, text="扫描设备", command=self.scan)
        self.scan_button.grid(row=0, column=2, padx=(10, 0))
        table = ttk.Frame(devices, style="Panel.TFrame")
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("ssid", "mac", "version", "state"), show="headings", selectmode="extended", height=5)
        scale = self.winfo_toplevel().ui_scale
        for key, title, width in [("ssid", "设备热点", 150), ("mac", "MAC 地址", 155), ("version", "版本", 100), ("state", "状态", 120)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=round(width*scale), minwidth=round(width*.8*scale), anchor="w", stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.errors = {}
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._update_start())
        self.tree.bind("<Double-1>", self.show_result)
        bottom = ttk.Frame(devices, style="Panel.TFrame")
        bottom.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(bottom, text="全选", command=lambda: self.tree.selection_set(self.tree.get_children())).pack(side="left")
        ttk.Button(bottom, text="清除选择", command=lambda: self.tree.selection_remove(self.tree.selection())).pack(side="left", padx=(8, 0))
        wrapping_label(devices, text="Ctrl / Shift 多选 · 双击查看详情 · 再次扫描可补充设备", style="Muted.TLabel").grid(row=3, column=0, sticky="ew", pady=(8, 0))
        options = ttk.Frame(body, style="App.TFrame")
        options.grid(row=3, column=0, sticky="ew", pady=(12, 4))
        options.columnconfigure(0, weight=1)
        ttk.Label(options, text="完成后恢复 Wi-Fi（原网络配置名称）", style="Status.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(options, textvariable=self.restore, width=1).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        footer = ttk.Frame(self, style="App.TFrame", padding=(0, 10, 0, 0))
        footer.grid(row=1, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        wrapping_label(footer, textvariable=self.status, style="Status.TLabel").grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.stop_button = ttk.Button(footer, text="停止剩余任务", command=self.stop, state="disabled")
        self.stop_button.grid(row=1, column=0, sticky="e", padx=(0, 8))
        self.start_button = ttk.Button(footer, text="开始批量升级", command=self.start, style="Primary.TButton", state="disabled")
        self.start_button.grid(row=1, column=1, sticky="e")
        ttk.Progressbar(footer, variable=self.progress, maximum=100, style="Green.Horizontal.TProgressbar").grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))

    def choose_firmware(self):
        path = filedialog.askopenfilename(title="选择 OTA 固件", filetypes=(("应用固件", "*.bin"),))
        if path:
            self.load_firmware(path)

    def load_firmware(self, path):
        if self.running or self.scanning or self.winfo_toplevel().running:
            return False
        try:
            data = Path(path).read_bytes()
            compatibility, version = image_identity(data)
        except Exception as error:
            messagebox.showerror("固件不匹配", str(error))
            return False
        self.firmware.set(str(Path(path).resolve()))
        self.version.set(f"{version}  ·  {len(data) / 1024:.0f} KB  ·  ESP32-S3")
        self.status.set("固件已校验，请扫描并选择目标设备后开始升级。")
        self._update_start()
        return True

    def _update_start(self):
        enabled = not self.running and not self.scanning and Path(self.firmware.get()).is_file() and bool(self.tree.selection())
        self.start_button.configure(state="normal" if enabled else "disabled")

    def scan(self):
        if self.running or self.scanning:
            return
        if self.winfo_toplevel().running:
            messagebox.showinfo("串口任务正在进行", "请先完成串口任务，再扫描设备版本。")
            return
        prefix = self.prefix.get().strip() or "Yshand_"
        self.scan_button.configure(state="disabled")
        self.scanning = True
        self.stop_event.clear()
        self.progress.set(0)
        self._update_start()
        self.choose_button.configure(state="disabled")
        self.status.set("正在分轮扫描设备，随后读取版本并恢复原网络。")
        self.stop_button.configure(text="停止读取版本", state="normal")
        def worker():
            try:
                discover_devices(prefix, self.stop_event,
                                 callback=lambda kind, data: self.events.put((kind, data)))
            except Exception as error:
                self.events.put(("error", str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def show_result(self, _event=None):
        for item in self.tree.selection()[:1]:
            values = self.tree.item(item, "values")
            messagebox.showinfo(values[0], self.errors.get(values[0], " · ".join(values)))

    def start(self):
        if self.winfo_toplevel().running:
            messagebox.showinfo("串口下载正在进行", "请等待串口下载结束后再开始 OTA。")
            return
        if self.scanning:
            return
        ssids = [self.tree.item(item, "values")[0] for item in self.tree.selection()]
        if not ssids or self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.progress.set(0)
        self.start_button.configure(state="disabled")
        self.scan_button.configure(state="disabled")
        self.choose_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.set(f"准备升级 {len(ssids)} 台设备…")
        firmware, restore = self.firmware.get(), self.restore.get().strip()
        output = Path.home() / "Documents" / "Ys元思烧录平台" / "ota-reports"
        def worker():
            try:
                run_batch(ssids, firmware, restore_profile=restore, output_path=output,
                          stop_event=self.stop_event, callback=lambda kind, data: self.events.put((kind, data)))
            except Exception as error:
                self.events.put(("error", str(error)))
                self.events.put(("done", {"results": [], "cancelled": False}))
        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self.status.set("正在结束版本读取并恢复网络。" if self.scanning else
                        "当前设备完成验证后停止，不中断正在写入的固件。")

    def _drain(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "scan_progress":
                    self.status.set(f"热点扫描 {data['round']}/{data['rounds']} 轮 · 已发现 {data['count']} 台")
                elif kind == "scan":
                    existing = {self.tree.item(item, "values")[0]: item for item in self.tree.get_children()}
                    new_items = []
                    for ssid in data:
                        if ssid not in existing:
                            new_items.append(self.tree.insert("", "end", values=(ssid, "—", "未读取", "已发现")))
                    self.tree.selection_add(new_items)
                    self.status.set(f"已合并 {len(data)} 台设备，扫描结束后读取版本。" if data else "本轮暂未发现设备，继续扫描…")
                    self._update_start()
                elif kind == "device":
                    ssid = data["ssid"]
                    if data.get("error"):
                        self.errors[ssid] = data["error"]
                    for item in self.tree.get_children():
                        values = list(self.tree.item(item, "values"))
                        if values[0] == ssid:
                            values[1] = data.get("mac", values[1])
                            values[2] = data.get("version", values[2])
                            if data["state"] == "识别失败" and values[2] in ("未读取", "读取中", "—"):
                                values[2] = "未识别"
                            elif data.get("version"):
                                self.errors.pop(ssid, None)
                            values[3] = data["state"]
                            self.tree.item(item, values=values)
                    self.status.set(f"{data['index']}/{data['count']}  {ssid} · {data['state']}")
                    if data["state"] in ("完成", "失败", "已识别", "识别失败"):
                        self.progress.set(data["index"] / data["count"] * 100)
                elif kind == "probe_done":
                    self.scanning = False
                    self.scan_button.configure(state="normal")
                    self.choose_button.configure(state="normal")
                    self.stop_button.configure(text="停止剩余任务", state="disabled")
                    if not data.get("cancelled"):
                        seen = set(data.get("ssids", []))
                        for item in self.tree.get_children():
                            values = list(self.tree.item(item, "values"))
                            if values[0] not in seen:
                                values[3] = "本轮未发现"
                                self.tree.item(item, values=values)
                                self.tree.selection_remove(item)
                    self.status.set(f"{'已停止' if data.get('cancelled') else '扫描完成'} · 发现 {data['count']} 台，版本可读 {data['identified']} 台；可再次扫描补充。")
                    self._update_start()
                elif kind == "done":
                    self.running = False
                    self.scan_button.configure(state="normal")
                    self.choose_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self._update_start()
                    results = data["results"]
                    success = sum(result.ok for result in results)
                    self.status.set(f"{'已停止' if data.get('cancelled') else '升级结束'} · 成功 {success}/{len(results)} 台")
                    if data.get("report"):
                        self.report_path = data["report"]
                elif kind == "restoring":
                    self.status.set(data)
                elif kind == "warning":
                    self.status.set(data)
                elif kind == "error":
                    self.status.set(data)
                    if not self.running:
                        self.scanning = False
                        self.scan_button.configure(state="normal")
                        self.choose_button.configure(state="normal")
                        self.stop_button.configure(state="disabled", text="停止剩余任务")
                        self._update_start()
        except queue.Empty:
            pass
        self.after(100, self._drain)

