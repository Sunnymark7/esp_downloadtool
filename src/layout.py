"""可缩放的内容区：窗口变小时滚动内容，底部操作始终可见。"""
import tkinter as tk
from tkinter import ttk


def wrapping_label(parent, **kwargs):
    """长路径/状态跟随实际宽度换行，不用文本长度撑大窗口。"""
    label = ttk.Label(parent, width=1, **kwargs)
    label.bind("<Configure>", lambda event: label.configure(wraplength=max(40, event.width)))
    return label


class ScrollableContent(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, style="App.TFrame")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                background="#f4f8f7", width=1, height=1)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.content = ttk.Frame(self.canvas, style="App.TFrame")
        self.window = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.canvas.bind("<Configure>", self._resize)
        self.content.bind("<Configure>", self._content_changed)
        # 单个根窗口绑定即可覆盖内容内的 Label/Entry 等所有控件。
        parent.winfo_toplevel().bind("<MouseWheel>", self._wheel, add="+")

    def _resize(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)

    def _content_changed(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _wheel(self, event):
        # 原生列表/文本框保留自己的滚动行为，避免同时滚动两层。
        if isinstance(event.widget, (tk.Text, ttk.Treeview, ttk.Combobox)):
            return
        widget = event.widget
        while widget is not None:
            if widget is self:
                if self.content.winfo_height() > self.canvas.winfo_height():
                    self.canvas.yview_scroll(-int(event.delta / 120), "units")
                return
            widget = getattr(widget, "master", None)

    def top(self):
        self.canvas.yview_moveto(0)
