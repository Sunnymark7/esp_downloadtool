"""在创建任何 Tk 窗口前启用 DPI 感知，避免 Windows 对整窗做位图放大。"""
import ctypes
import sys


def work_area(root):
    """初始窗口适配屏幕工作区，保留任务栏和标题栏空间。"""
    from ctypes import wintypes
    rect = wintypes.RECT()
    if sys.platform == "win32" and ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def enable_high_dpi():
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        # Per-Monitor V2：由系统提供当前屏幕的实际像素，不插值拉伸文字。
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
        if ctypes.get_last_error() == 5:  # manifest 已设置，不能再次覆盖。
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()
