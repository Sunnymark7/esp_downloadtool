"""AirHand 批量 OTA 核心：扫描设备热点、切换网络、上传并等待首启验证。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from wifi_windows import Wifi, WifiError


BASE_URL = "http://192.168.10.1"


class BatchOtaError(RuntimeError):
    """用户可以直接理解的批量升级错误。"""


@dataclass
class DeviceResult:
    ssid: str
    ok: bool
    before: dict | None = None
    after: dict | None = None
    error: str = ""


def scan_airhand(prefix: str = "Yshand_", stop_event=None, callback=None,
                 rounds: int = 3) -> list[str]:
    stop_event = stop_event or threading.Event()
    callback = callback or (lambda *_: None)
    found = set()
    with Wifi() as wifi:
        for index in range(rounds):
            if stop_event.is_set():
                break
            callback("scan_progress", {"round": index + 1, "rounds": rounds, "count": len(found)})
            try:
                wifi.scan(stop_event=stop_event)
                found.update(item["ssid"] for item in wifi.networks()
                    if item["ssid"].casefold().startswith(prefix.casefold()) and not item["secure"])
            except WifiError:
                if index == rounds - 1 and not found:
                    raise
                callback("warning", "本轮热点扫描失败，正在重试并保留已发现设备。")
            callback("scan", sorted(found))
            if index < rounds - 1 and stop_event.wait(.6):
                break
    return sorted(found)


def current_profile() -> str:
    try:
        with Wifi() as wifi:
            return wifi.current()["profile"]
    except (WifiError, OSError):
        return ""


def restore_wifi(wifi, profile_name, original_profile, callback):
    """只恢复此任务替换的连接并清理自己的临时配置，不删除用户原配置。"""
    try:
        if original_profile:
            callback("restoring", "正在恢复原 Wi-Fi…")
            wifi.connect(original_profile)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if wifi.current()["profile"] == original_profile:
                    break
                time.sleep(.5)
            else:
                raise WifiError("等待恢复网络超时")
        else:
            wifi.disconnect()
    except WifiError as error:
        callback("warning", f"无法恢复原 Wi-Fi，请手动连接：{error}")
    finally:
        try:
            wifi.delete_profile(profile_name)
        except WifiError as error:
            callback("warning", str(error))
        wifi.close()


def discover_devices(prefix="Yshand_", stop_event=None, callback=None):
    """发现热点后逐台只读查询版本；不登录、不上传、不执行设备动作。"""
    stop_event = stop_event or threading.Event()
    callback = callback or (lambda *_: None)
    ssids = scan_airhand(prefix, stop_event=stop_event, callback=callback)
    callback("scan", ssids)
    if not ssids:
        callback("probe_done", {"count": 0, "identified": 0, "ssids": [], "cancelled": stop_event.is_set()})
        return []
    wifi = Wifi()
    profile_name = "YsFlasher-Probe-" + uuid.uuid4().hex[:10]
    try:
        original = wifi.current()["profile"]
    except Exception:
        wifi.close()
        raise
    results = []
    try:
        for index, ssid in enumerate(ssids, 1):
            if stop_event.is_set():
                break
            state = {"index": index, "count": len(ssids), "ssid": ssid, "state": "读取版本"}
            callback("device", state)
            try:
                info = _connect(ssid, wifi, profile_name, stop_event, timeout=18)
                results.append(info)
                callback("device", {**state, "state": "已识别", "version": info.get("version", "未知"),
                                    "mac": info["mac"]})
            except Exception as error:
                callback("device", {**state, "state": "识别失败", "error": str(error)})
        return results
    finally:
        restore_wifi(wifi, profile_name, original, callback)
        callback("probe_done", {"count": len(ssids), "identified": len(results),
                                "ssids": ssids, "cancelled": stop_event.is_set()})


def _request(path: str, token: str = "", data: bytes | dict | None = None,
             headers: dict[str, str] | None = None, timeout: float = 8) -> tuple[int, dict]:
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = "Bearer " + token
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
        request_headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
    request = urllib.request.Request(BASE_URL + path, data=data, headers=request_headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read())
        except Exception:
            body = {"message": str(error)}
        return error.code, body


def _connect(ssid: str, wifi, profile_name: str, stop_event, timeout: float = 35) -> dict:
    wifi.add_open_profile(ssid, profile_name)
    wifi.connect(profile_name)
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        if stop_event.is_set():
            raise BatchOtaError("已取消，未上传固件")
        try:
            # 不以固定 IP 的可达性代替目标确认，避免读到尚未断开的上一台设备。
            if wifi.current()["ssid"] != ssid:
                time.sleep(.5)
                continue
            status, info = _request("/api/firmware", timeout=3)
            if status == 200 and info.get("ok"):
                if info.get("chip") != "ESP32-S3" or not info.get("mac"):
                    raise BatchOtaError("目标设备信息不完整或芯片不匹配")
                suffix = info["mac"].replace(":", "")[-4:].upper()
                if ssid.upper().startswith("YSHAND_") and ssid[-4:].upper() != suffix:
                    raise BatchOtaError("设备 MAC 与热点名称不匹配，已拒绝写入")
                return info
            last = str(info)
        except (OSError, ValueError) as error:
            last = str(error)
        time.sleep(.5)
    raise BatchOtaError(f"{ssid} 连接超时：{last}")


def _login(username: str, password: str) -> str:
    status, body = _request("/api/login", data={"username": username, "password": password})
    if status != 200 or not body.get("token"):
        raise BatchOtaError(f"登录失败：HTTP {status}")
    return body["token"]


def image_identity(image: bytes) -> tuple[str, str]:
    if len(image) < 336 or image[0] != 0xE9 or image[12:14] != b"\x09\x00" or image[32:36] != b"\x32\x54\xcd\xab":
        raise BatchOtaError("固件文件太小，无法读取 AirHand 身份信息")
    compatibility = image[288:320].split(b"\0", 1)[0].decode(errors="replace")
    version = image[320:336].split(b"\0", 1)[0].decode(errors="replace")
    if compatibility != "AirHand-S3-N16R8-noPSRAM-v1" or not version:
        raise BatchOtaError("固件不是受支持的 AirHand ESP32-S3 镜像")
    return compatibility, version


def _upload(image: bytes, token: str) -> dict:
    boundary = "ESP32FlasherBatchBoundary"
    prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"firmware\"; filename=\"firmware.bin\"\r\n"
              "Content-Type: application/octet-stream\r\n\r\n").encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    update_time = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    status, body = _request("/api/ota", token, prefix + image + suffix, {
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "X-Firmware-Size": str(len(image)),
        "X-Firmware-MD5": hashlib.md5(image).hexdigest(),
        "X-Update-Time": update_time,
    }, timeout=180)
    if status != 200 or not body.get("ok"):
        raise BatchOtaError(f"OTA 失败：HTTP {status} {body.get('message', '')}")
    return body


def _wait_verified(version: str, expected_hash: str, before: dict, wifi, ssid: str,
                   profile_name: str, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    retry_at = 0
    while time.monotonic() < deadline:
        try:
            if wifi.current()["ssid"] != ssid:
                if time.monotonic() >= retry_at:
                    wifi.connect(profile_name)
                    retry_at = time.monotonic() + 5
            else:
                status, info = _request("/api/firmware", timeout=5)
                last = info
                if status == 200 and info.get("mac") == before["mac"] and info.get("bootState") == "valid":
                    if (info.get("version") == version and info.get("imageSha256") == expected_hash and
                            info.get("partition") != before.get("partition")):
                        return info
                    # 旧槽 VALID 可能是复位之前短暂读到的旧应用，继续等待切槽或超时。
        except (OSError, ValueError, WifiError):
            pass
        time.sleep(1)
    raise BatchOtaError(f"首启验证超时或固件已回滚：{last}")


def run_batch(ssids: list[str], firmware_path: str | Path, restore_profile: str = "",
              username: str = "yskj", password: str = "0123456789",
              output_path: str | Path = "outputs/batch-ota",
              stop_event: threading.Event | None = None, callback=None) -> list[DeviceResult]:
    """顺序升级指定热点。callback(kind, payload) 在工作线程调用。"""
    stop_event = stop_event or threading.Event()
    callback = callback or (lambda *_: None)
    image = Path(firmware_path).read_bytes()
    compatibility, version = image_identity(image)
    output = Path(output_path)
    output.mkdir(parents=True, exist_ok=True)
    profile_name = "ESP32Flasher-OTA-" + uuid.uuid4().hex[:10]
    wifi = Wifi()
    try:
        original = wifi.current()
    except Exception:
        wifi.close()
        raise
    restore_profile = restore_profile or original["profile"]
    results: list[DeviceResult] = []
    seen_macs: set[str] = set()
    report = {"version": version, "compatibility": compatibility, "size": len(image),
              "fileSha256": hashlib.sha256(image).hexdigest(), "devices": []}
    report_path = output / ("batch-ota-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6] + ".json")
    try:
        callback("start", {"version": version, "compatibility": compatibility, "count": len(ssids)})
        for index, ssid in enumerate(ssids, 1):
            if stop_event.is_set():
                break
            result = DeviceResult(ssid=ssid, ok=False)
            try:
                callback("device", {"index": index, "count": len(ssids), "ssid": ssid, "state": "连接中"})
                before = _connect(ssid, wifi, profile_name, stop_event)
                result.before = before
                mac = before.get("mac", "")
                if mac in seen_macs:
                    raise BatchOtaError(f"检测到重复 MAC，已跳过：{mac}")
                seen_macs.add(mac)
                if before.get("busy") or before.get("maintenance"):
                    raise BatchOtaError("设备正忙或处于维护状态")
                callback("device", {"index": index, "count": len(ssids), "ssid": ssid, "state": "上传中", "mac": mac})
                token = _login(username, password)
                status, metadata = _request("/api/ota", token)
                if status != 200 or not metadata.get("ok") or metadata.get("chip") != "ESP32-S3":
                    raise BatchOtaError("无法核验目标 OTA 能力")
                if len(image) > int(metadata.get("maxSize", 0)):
                    raise BatchOtaError("固件超过目标 OTA 分区容量")
                if stop_event.is_set():
                    raise BatchOtaError("已取消，未上传固件")
                # HTTP 成功响应丢失也可能已切槽：只在最终读取镜像身份后判定结果。
                try:
                    _upload(image, token)
                except (OSError, ValueError) as error:
                    callback("warning", f"{ssid} 上传响应丢失，正在重新核验版本：{error}")
                callback("device", {"index": index, "count": len(ssids), "ssid": ssid, "state": "首启验证中", "mac": mac})
                result.after = _wait_verified(version, image[176:208].hex(), before, wifi, ssid, profile_name)
                result.ok = True
                callback("device", {"index": index, "count": len(ssids), "ssid": ssid, "state": "完成", "mac": mac, "version": version})
            except Exception as error:
                result.error = str(error)
                callback("device", {"index": index, "count": len(ssids), "ssid": ssid, "state": "失败", "error": result.error})
            results.append(result)
            report["devices"].append({"ssid": result.ssid, "ok": result.ok, "before": result.before, "after": result.after, "error": result.error})
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return results
    finally:
        restore_wifi(wifi, profile_name, restore_profile, callback)
        callback("done", {"results": results, "cancelled": stop_event.is_set(), "report": str(report_path)})
