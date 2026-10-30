"""Windows 原生文件拖放；不依赖额外 Tcl 插件，打包后仍是单个 exe。"""
import ctypes
from ctypes import wintypes
from pathlib import Path


def firmware_drop_target(paths):
    """明确区分 ZIP 串口包与 OTA 应用 BIN，避免推断烧录地址。"""
    if len(paths) != 1:
        raise ValueError("请一次拖入一个 ZIP 固件包或 BIN 应用固件。")
    path = Path(paths[0])
    if not path.is_file():
        raise ValueError("拖入的内容不是可读取的文件。")
    suffix = path.suffix.lower()
    if suffix not in (".zip", ".bin"):
        raise ValueError("支持 ZIP 串口固件包和 BIN 无线升级固件。")
    return ("serial" if suffix == ".zip" else "ota"), str(path.resolve())


class WindowsFileDrop:
    """只接受文件路径；真正的校验/页面切换在 Tk 主循环处理。"""
    def __init__(self, root, on_drop):
        self.root = root
        self.on_drop = on_drop
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        self.shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                               wintypes.LPWSTR, wintypes.UINT]
        self.shell32.DragQueryFileW.restype = wintypes.UINT
        self.shell32.DragFinish.argtypes = [wintypes.HANDLE]
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                    wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
                    ctypes.c_size_t, ctypes.c_size_t)
        self.callback = callback_type(self._message)
        self.comctl32.SetWindowSubclass.argtypes = [wintypes.HWND, callback_type,
                                                  ctypes.c_size_t, ctypes.c_size_t]
        self.comctl32.SetWindowSubclass.restype = wintypes.BOOL
        self.comctl32.RemoveWindowSubclass.argtypes = [wintypes.HWND, callback_type, ctypes.c_size_t]
        self.comctl32.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT,
                                                 ctypes.c_size_t, ctypes.c_ssize_t]
        self.comctl32.DefSubclassProc.restype = ctypes.c_ssize_t
        root.update_idletasks()
        self.hwnd = self.user32.GetAncestor(root.winfo_id(), 2)
        if not self.comctl32.SetWindowSubclass(self.hwnd, self.callback, 1, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        self.shell32.DragAcceptFiles(self.hwnd, True)

    def _message(self, hwnd, message, wparam, lparam, subclass_id, reference):
        if message == 0x0233:  # WM_DROPFILES
            try:
                paths = []
                count = self.shell32.DragQueryFileW(wparam, 0xffffffff, None, 0)
                for index in range(count):
                    length = self.shell32.DragQueryFileW(wparam, index, None, 0)
                    path = ctypes.create_unicode_buffer(length + 1)
                    self.shell32.DragQueryFileW(wparam, index, path, length + 1)
                    paths.append(path.value)
                self.root.after_idle(self.on_drop, paths)
            finally:
                self.shell32.DragFinish(wparam)
            return 0
        if message == 0x0082:  # WM_NCDESTROY，原生窗口销毁前卸载回调。
            self.comctl32.RemoveWindowSubclass(hwnd, self.callback, subclass_id)
        return self.comctl32.DefSubclassProc(hwnd, message, wparam, lparam)
