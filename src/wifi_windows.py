"""Windows 原生 WLAN API；不解析本地化 netsh 输出，也不假定网卡名称为 WLAN。"""
import ctypes as C
from ctypes import wintypes as W
import threading
import time
from xml.sax.saxutils import escape


class WifiError(RuntimeError):
    pass


class GUID(C.Structure):
    _fields_ = [("data", C.c_ubyte * 16)]


class Interface(C.Structure):
    _fields_ = [("guid", GUID), ("description", W.WCHAR * 256), ("state", W.DWORD)]


class SSID(C.Structure):
    _fields_ = [("length", W.DWORD), ("value", C.c_ubyte * 32)]

    def text(self):
        return bytes(self.value[:min(self.length, 32)]).decode("utf-8", errors="replace")


class RateSet(C.Structure):
    _fields_ = [("count", W.DWORD), ("rates", W.USHORT * 126)]


class BssEntry(C.Structure):
    _fields_ = [("ssid", SSID), ("phy", W.DWORD), ("bssid", C.c_ubyte * 6),
        ("type", W.DWORD), ("phy_type", W.DWORD), ("rssi", W.LONG),
        ("quality", W.DWORD), ("reg_domain", C.c_ubyte), ("beacon_period", W.USHORT),
        ("timestamp", C.c_uint64), ("host_timestamp", C.c_uint64),
        ("capabilities", W.USHORT), ("frequency", W.DWORD), ("rates", RateSet),
        ("ie_offset", W.DWORD), ("ie_size", W.DWORD)]


def fresh_networks(entries, now_filetime, max_age_seconds=10):
    # ullHostTimestamp 为系统收到该广播的 FILETIME，100ns/单位。
    # 普通可用网络列表会保留已关机热点，不能直接作为“本轮发现”。
    return [{"ssid": entry.ssid.text(), "signal": entry.quality,
             "secure": bool(entry.capabilities & 0x10)} for entry in entries
            if entry.host_timestamp and
            -1 <= (now_filetime - entry.host_timestamp) / 10_000_000 <= max_age_seconds]


class Association(C.Structure):
    _fields_ = [("ssid", SSID), ("bss", W.DWORD), ("bssid", C.c_ubyte * 6),
                ("phy", W.DWORD), ("phy_index", W.DWORD), ("signal", W.DWORD),
                ("rx", W.DWORD), ("tx", W.DWORD)]


class Connection(C.Structure):
    _fields_ = [("state", W.DWORD), ("mode", W.DWORD), ("profile", W.WCHAR * 256),
                ("association", Association), ("security", W.DWORD * 4)]


class Parameters(C.Structure):
    _fields_ = [("mode", W.DWORD), ("profile", W.LPCWSTR), ("ssid", C.c_void_p),
                ("bssid_list", C.c_void_p), ("bss", W.DWORD), ("flags", W.DWORD)]


class Notification(C.Structure):
    _fields_ = [("source", W.DWORD), ("code", W.DWORD), ("guid", GUID),
                ("size", W.DWORD), ("data", C.c_void_p)]


def check(code, operation):
    if code:
        detail = "请检查 Windows 定位权限和 Wi-Fi 是否开启" if code == 5 else C.FormatError(code).strip()
        raise WifiError(f"{operation}失败（{code}）：{detail}")


class Wifi:
    def __init__(self):
        self.api = C.WinDLL("wlanapi")
        self.handle = W.HANDLE()
        version = W.DWORD()
        check(self.api.WlanOpenHandle(2, None, C.byref(version), C.byref(self.handle)), "打开无线服务")
        pointer = C.c_void_p()
        try:
            check(self.api.WlanEnumInterfaces(self.handle, None, C.byref(pointer)), "读取无线网卡")
            count = W.DWORD.from_address(pointer.value).value
            items = (Interface * count).from_address(pointer.value + 8)
            if not count:
                raise WifiError("没有可用的 Wi-Fi 网卡")
            selected = next((item for item in items if item.state == 1), items[0])
            self.guid = GUID.from_buffer_copy(selected.guid)
            self.scan_finished = threading.Event()
            self.scan_failed = False
            callback_type = C.WINFUNCTYPE(None, C.POINTER(Notification), C.c_void_p)
            self.notification_callback = callback_type(self._notification)
            self.api.WlanRegisterNotification.argtypes = [W.HANDLE, W.DWORD, W.BOOL,
                callback_type, C.c_void_p, C.c_void_p, C.POINTER(W.DWORD)]
            previous = W.DWORD()
            check(self.api.WlanRegisterNotification(self.handle, 8, True,
                    self.notification_callback, None, None, C.byref(previous)), "监听扫描结果")
        except Exception:
            self.close()
            raise
        finally:
            if pointer.value:
                self.api.WlanFreeMemory(pointer)

    def close(self):
        if self.handle:
            self.api.WlanCloseHandle(self.handle, None)
            self.handle = W.HANDLE()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def current(self):
        pointer, size, kind = C.c_void_p(), W.DWORD(), W.DWORD()
        code = self.api.WlanQueryInterface(self.handle, C.byref(self.guid), 7, None,
                                            C.byref(size), C.byref(pointer), C.byref(kind))
        if code == 5023:  # ERROR_INVALID_STATE：尚未连接。
            return {"ssid": "", "profile": ""}
        check(code, "读取当前网络")
        try:
            data = Connection.from_address(pointer.value)
            return {"ssid": data.association.ssid.text(), "profile": data.profile}
        finally:
            self.api.WlanFreeMemory(pointer)

    def _notification(self, pointer, _context):
        data = pointer.contents
        if (data.source == 8 and bytes(data.guid) == bytes(self.guid)
                and data.code in (7, 8)):
            self.scan_failed = data.code == 8
            self.scan_finished.set()

    def scan(self, timeout=8, stop_event=None):
        # WlanScan 只提交异步请求；等待 ACM 完成通知后才能读取新列表。
        self.scan_finished.clear()
        self.scan_failed = False
        check(self.api.WlanScan(self.handle, C.byref(self.guid), None, None, None), "扫描热点")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return False
            if self.scan_finished.wait(.1):
                if self.scan_failed:
                    raise WifiError("无线网卡本轮扫描失败，请检查 Wi-Fi 后重试。")
                return True
        return False  # 某些驱动漏通知；调用方读取当前结果并继续下一轮。

    def networks(self):
        pointer = C.c_void_p()
        check(self.api.WlanGetNetworkBssList(self.handle, C.byref(self.guid), None,
                    3, False, None, C.byref(pointer)), "读取广播热点列表")
        try:
            size = W.DWORD.from_address(pointer.value).value
            count = W.DWORD.from_address(pointer.value + 4).value
            if size < 8 or count * C.sizeof(BssEntry) > size - 8:
                raise WifiError("无线驱动返回的广播列表长度无效。")
            items = (BssEntry * count).from_address(pointer.value + 8)
            now = int((time.time() + 11644473600) * 10_000_000)
            return fresh_networks(items, now)
        finally:
            self.api.WlanFreeMemory(pointer)

    def add_open_profile(self, ssid, name):
        xml = f'''<?xml version="1.0"?><WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
<name>{escape(name)}</name><SSIDConfig><SSID><hex>{ssid.encode().hex()}</hex><name>{escape(ssid)}</name></SSID></SSIDConfig>
<connectionType>ESS</connectionType><connectionMode>manual</connectionMode><MSM><security><authEncryption>
<authentication>open</authentication><encryption>none</encryption><useOneX>false</useOneX>
</authEncryption></security></MSM></WLANProfile>'''
        reason = W.DWORD()
        check(self.api.WlanSetProfile(self.handle, C.byref(self.guid), 2, C.c_wchar_p(xml),
                                      None, True, None, C.byref(reason)), "创建临时连接")

    def connect(self, name):
        params = Parameters(0, name, None, None, 1, 0)
        check(self.api.WlanConnect(self.handle, C.byref(self.guid), C.byref(params), None), "连接热点")

    def delete_profile(self, name):
        code = self.api.WlanDeleteProfile(self.handle, C.byref(self.guid), C.c_wchar_p(name), None)
        if code != 1168:
            check(code, "删除临时连接")

    def disconnect(self):
        check(self.api.WlanDisconnect(self.handle, C.byref(self.guid), None), "断开临时网络")
