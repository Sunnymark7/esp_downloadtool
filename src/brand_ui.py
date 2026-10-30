"""品牌页头：原始标志以实际屏幕像素重采样，底纹不会覆盖操作区域。"""
import tkinter as tk
import ctypes
from ctypes import wintypes
from PIL import Image, ImageTk

BG = "#f4f8f7"
TEAL = "#007e78"
INK = "#183b39"


class BrandHeader(tk.Canvas):
    def __init__(self, master, logo_path, scale):
        self.scale = scale
        super().__init__(master, background=BG, highlightthickness=0, height=round(82 * scale))
        self.logo = Image.open(logo_path).convert("RGBA")
        self.last_size = None
        self.bind("<Configure>", self.redraw)

    def redraw(self, event):
        size = (event.width, event.height)
        if size == self.last_size:
            return
        self.last_size = size
        self.delete("all")
        scale = self.scale
        # 从原图取左侧符号作为窗口内标识，不重绘或替换品牌内容。
        symbol = self.logo.crop((0, 0, self.logo.height, self.logo.height))
        symbol.thumbnail((round(44 * scale), round(44 * scale)), Image.Resampling.LANCZOS)
        self.symbol = ImageTk.PhotoImage(symbol, master=self)
        self.create_image(25 * scale, 30 * scale, image=self.symbol, anchor="center")
        self.create_text(58 * scale, 10 * scale, text="烧录平台", anchor="nw",
                         fill=INK, font=("Microsoft YaHei UI", 19, "bold"))
        self.create_text(58 * scale, 46 * scale, text="串口烧录  /  批量无线升级", anchor="nw",
                         fill="#526c69", font=("Microsoft YaHei UI", 10))


class PageWatermark:
    """原生透明拥有者窗口：Tk 不再给透明位图重绘实色背景。

    图形只使用原始 PNG 的透明度轮廓；所有 RGB 都由品牌青绿色生成，
    不使用色键抠图，因此没有红/紫色边缘。窗口不激活且鼠标穿透。
    """
    def __init__(self, master, logo_path, scale):
        self.master, self.scale = master, scale
        logo = Image.open(logo_path).convert("RGBA")
        self.symbol_source = logo.crop((0, 0, logo.height, logo.height))
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
            wintypes.HINSTANCE, ctypes.c_void_p]
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        self.user32.UpdateLayeredWindow.restype = wintypes.BOOL
        self.gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        self.gdi.CreateCompatibleDC.restype = ctypes.c_void_p
        self.gdi.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
            wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD]
        self.gdi.CreateDIBSection.restype = ctypes.c_void_p
        self.gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.gdi.SelectObject.restype = ctypes.c_void_p
        self.gdi.DeleteObject.argtypes = [ctypes.c_void_p]
        self.gdi.DeleteDC.argtypes = [ctypes.c_void_p]
        master.update_idletasks()
        self.owner = self.user32.GetAncestor(master.winfo_id(), 2)
        # WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW。
        self.hwnd = self.user32.CreateWindowExW(0x80800A0, "STATIC", "",
            0x80000000, 0, 0, 1, 1, self.owner, None, None, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.image_size = 0
        self.image = None
        self.pending = None
        master.bind("<Configure>", self.schedule, add="+")
        master.bind("<Map>", self.schedule, add="+")
        master.bind("<Unmap>", self.hide, add="+")
        master.bind("<Destroy>", self.destroy, add="+")
        self.schedule()

    def schedule(self, event=None):
        if event is not None and event.widget is not self.master:
            return
        if self.pending is not None:
            self.master.after_cancel(self.pending)
        self.pending = self.master.after(25, self.render)

    def hide(self, event=None):
        if event is None or event.widget is self.master:
            self.user32.ShowWindow(self.hwnd, 0)

    def destroy(self, event):
        if event.widget is self.master and self.hwnd:
            self.user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    def render(self):
        self.pending = None
        if not self.hwnd or not self.master.winfo_viewable():
            return
        width, height = self.master.winfo_width(), self.master.winfo_height()
        size = round(min(width * .56, height * .66, 490 * self.scale))
        if size < 10:
            return
        if self.image_size != size:
            self.image_size = size
            alpha = self.symbol_source.resize((size, size), Image.Resampling.LANCZOS).getchannel("A")
            alpha = alpha.point(lambda value: round(value * .085))
            # UpdateLayeredWindow 要求预乘 BGRA，透明像素必须为全零。
            self.image = Image.merge("RGBA", (alpha.point(lambda v: 0),
                alpha.point(lambda v: round(v * 166 / 255)),
                alpha.point(lambda v: round(v * 156 / 255)), alpha))
        x = self.master.winfo_rootx() + width - size - round(18 * self.scale)
        y = self.master.winfo_rooty() + height - size - round(22 * self.scale)
        self._update_layer(self.image, x, y)

    def _update_layer(self, image, x, y):
        class BitmapHeader(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG),
                ("height", wintypes.LONG), ("planes", wintypes.WORD),
                ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                ("image_size", wintypes.DWORD), ("xppm", wintypes.LONG),
                ("yppm", wintypes.LONG), ("used", wintypes.DWORD), ("important", wintypes.DWORD)]
        class Blend(ctypes.Structure):
            _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte),
                        ("alpha", ctypes.c_ubyte), ("format", ctypes.c_ubyte)]
        dc = self.gdi.CreateCompatibleDC(None)
        bits = ctypes.c_void_p()
        header = BitmapHeader(ctypes.sizeof(BitmapHeader), image.width, -image.height, 1, 32)
        bitmap = self.gdi.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        if not dc or not bitmap or not bits.value:
            if bitmap: self.gdi.DeleteObject(bitmap)
            if dc: self.gdi.DeleteDC(dc)
            raise ctypes.WinError(ctypes.get_last_error())
        old = self.gdi.SelectObject(dc, bitmap)
        try:
            raw = image.tobytes("raw", "BGRA")
            ctypes.memmove(bits, raw, len(raw))
            destination, dimensions = wintypes.POINT(x, y), wintypes.SIZE(image.width, image.height)
            origin, blend = wintypes.POINT(), Blend(0, 0, 255, 1)
            if not self.user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(destination),
                ctypes.byref(dimensions), dc, ctypes.byref(origin), 0, ctypes.byref(blend), 2):
                raise ctypes.WinError(ctypes.get_last_error())
            self.user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
        finally:
            self.gdi.SelectObject(dc, old)
            self.gdi.DeleteObject(bitmap)
            self.gdi.DeleteDC(dc)
