"""Windows 实际 Tk 布局与原生 WM_DROPFILES 回归，不连接设备或写入固件。"""
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from esp32_flasher_gui import Esp32FlasherApp, DEVELOPER_MODE, USER_MODE
from file_drop import firmware_drop_target
from test_ota import image_bytes


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Esp32FlasherApp()
        cls.app.title("Ys元思烧录平台 · 自动回归")
        cls.temp = tempfile.TemporaryDirectory()
        cls.bin = Path(cls.temp.name) / "中文 固件.bin"
        cls.bin.write_bytes(image_bytes())
        cls.zip = Path(cls.temp.name) / "中文 固件包.zip"
        with zipfile.ZipFile(cls.zip, "w") as archive:
            archive.writestr("firmware.bin", b"firmware")
    @classmethod
    def tearDownClass(cls):
        cls.app.cleanup_package_dir()
        cls.app.destroy()
        cls.temp.cleanup()

    def pump(self):
        for _ in range(4):
            self.app.update(); time.sleep(.025)

    def test_small_window_fixed_actions_and_scroll_content(self):
        app = self.app
        for width, height in [(580,460),(620,530),(880,730)]:
            app.geometry(f"{round(width*app.ui_scale)}x{round(height*app.ui_scale)}")
            for page in (0,1):
                app.notebook.select(page)
                for mode in ([USER_MODE,DEVELOPER_MODE] if page==0 else [USER_MODE]):
                    app.mode_var.set(mode)
                    app.package_var.set("D:/很长的固件目录/"*12+"firmware.zip")
                    app.ota_page.status.set("正在验证设备并等待恢复网络。"*8)
                    self.pump()
                    buttons = [app.flash_button,app.stop_button] if page==0 else [app.ota_page.start_button,app.ota_page.stop_button]
                    for button in buttons:
                        self.assertTrue(button.winfo_ismapped())
                        self.assertGreater(button.winfo_height(),20)
                        self.assertGreaterEqual(button.winfo_rootx(),app.winfo_rootx())
                        self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),app.winfo_rootx()+app.winfo_width())
                        self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),app.winfo_rooty()+app.winfo_height())
            self.assertTrue(app.page_watermark.hwnd)

    def native_drop(self, path):
        class DropFiles(ctypes.Structure):
            _fields_=[("offset",wintypes.DWORD),("point",wintypes.POINT),("nonclient",wintypes.BOOL),("wide",wintypes.BOOL)]
        header=DropFiles(ctypes.sizeof(DropFiles),wintypes.POINT(),False,True)
        raw=bytes(header)+(str(path)+"\0\0").encode("utf-16-le")
        kernel=ctypes.WinDLL("kernel32")
        kernel.GlobalAlloc.argtypes=[wintypes.UINT,ctypes.c_size_t]; kernel.GlobalAlloc.restype=ctypes.c_void_p
        kernel.GlobalLock.argtypes=[ctypes.c_void_p];kernel.GlobalLock.restype=ctypes.c_void_p
        kernel.GlobalUnlock.argtypes=[ctypes.c_void_p]
        handle=kernel.GlobalAlloc(0x42,len(raw))
        pointer=kernel.GlobalLock(handle);ctypes.memmove(pointer,raw,len(raw));kernel.GlobalUnlock(handle)
        user=ctypes.WinDLL("user32")
        user.SendMessageW.argtypes=[wintypes.HWND,wintypes.UINT,ctypes.c_size_t,ctypes.c_ssize_t]
        user.SendMessageW.restype=ctypes.c_ssize_t
        user.SendMessageW(self.app.file_drop.hwnd,0x233,handle,0)
        self.pump()  # DragFinish 由生产回调释放 HDROP。

    def test_native_unicode_file_drop_routes_bin_and_zip(self):
        self.native_drop(self.bin)
        self.assertEqual(self.app.ota_page.firmware.get(),str(self.bin.resolve()))
        self.assertEqual(self.app.notebook.index("current"),1)
        self.assertFalse(self.app.ota_page.running)
        self.native_drop(self.zip)
        self.assertEqual(self.app.package_var.get(),str(self.zip))
        self.assertEqual(self.app.notebook.index("current"),0)
        self.assertTrue(self.app.rows)
        self.assertFalse(self.app.running)

    def test_drop_rejects_multiple_and_busy_does_not_replace_firmware(self):
        with self.assertRaises(ValueError): firmware_drop_target([str(self.bin),str(self.zip)])
        previous=self.app.ota_page.firmware.get()
        self.app.ota_page.scanning=True
        try:
            with patch("esp32_flasher_gui.messagebox.showinfo"):
                self.app.load_dropped_files([str(self.bin)])
        finally: self.app.ota_page.scanning=False
        self.assertEqual(self.app.ota_page.firmware.get(),previous)

    def test_scan_merge_keeps_versions_and_marks_missing_without_removing(self):
        page=self.app.ota_page
        page.tree.delete(*page.tree.get_children())
        page.events.put(("scan",["Yshand_1111"]))
        self.pump()
        page.events.put(("device",dict(index=1,count=1,ssid="Yshand_1111",mac="00:00:00:00:11:11",version="v1",state="已识别")))
        self.pump()
        page.events.put(("scan",["Yshand_2222"]))
        page.events.put(("probe_done",dict(count=1,identified=0,ssids=["Yshand_2222"])))
        self.pump()
        rows={page.tree.item(item,"values")[0]:page.tree.item(item,"values") for item in page.tree.get_children()}
        self.assertEqual(rows["Yshand_1111"][2:],("v1","本轮未发现"))
        self.assertIn("Yshand_2222",rows)


if __name__ == "__main__": unittest.main()
