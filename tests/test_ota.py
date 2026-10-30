"""真实 OTA 编排配合隔离网络替身；不连接或写入任何实物设备。"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import batch_ota as ota


def image_bytes():
    image = bytearray(512)
    image[0] = 0xE9
    image[12:14] = b"\x09\x00"
    image[32:36] = b"\x32\x54\xcd\xab"
    image[176:208] = b"x" * 32
    identity = b"AirHand-S3-N16R8-noPSRAM-v1"
    image[288:288+len(identity)] = identity
    image[320:327] = b"2.0.0\0\0"
    return bytes(image)


def info(suffix="1234", **values):
    return {"ok": True, "chip": "ESP32-S3", "mac": f"AA:BB:CC:DD:{suffix[:2]}:{suffix[2:]}",
            "version": "1.0.0", "partition": "app0", "bootState": "valid", "busy": False,
            "maintenance": False, **values}


class FakeWifi:
    def __init__(self):
        self.ssid = "office"
        self.profile = "原网络"
        self.profiles = {}
        self.history = []

    def current(self):
        return {"ssid": self.ssid, "profile": self.profile}

    def add_open_profile(self, ssid, name):
        self.profiles[name] = ssid

    def connect(self, name):
        self.history.append(("connect", name))
        self.profile, self.ssid = name, self.profiles.get(name, "office")

    def delete_profile(self, name):
        self.history.append(("delete", name))

    def disconnect(self):
        self.history.append(("disconnect",))

    def close(self):
        self.history.append(("close",))


class OtaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "firmware.bin"
        self.path.write_bytes(image_bytes())

    def test_identity_rejects_wrong_chip_or_layout(self):
        self.assertEqual(ota.image_identity(image_bytes()), ("AirHand-S3-N16R8-noPSRAM-v1", "2.0.0"))
        for position in [0,12,32,288]:
            changed = bytearray(image_bytes()); changed[position] ^= 0xFF
            with self.assertRaises(ota.BatchOtaError):
                ota.image_identity(changed)

    def test_connect_checks_real_ssid_and_mac_before_returning(self):
        wifi = FakeWifi()
        with patch.object(ota, "_request", return_value=(200, info())), patch.object(ota.time, "sleep"):
            self.assertEqual(ota._connect("Yshand_1234", wifi, "temporary", threading.Event())["mac"], info()["mac"])
        with patch.object(ota, "_request", return_value=(200, info("5678"))):
            with self.assertRaisesRegex(ota.BatchOtaError, "MAC"):
                ota._connect("Yshand_1234", wifi, "temporary", threading.Event())

    def test_verified_requires_valid_hash_mac_and_new_slot(self):
        wifi = FakeWifi(); wifi.ssid = "Yshand_1234"
        correct = info(version="2.0.0", imageSha256="78"*32, partition="app1")
        candidates = [info(), {**correct, "bootState": "pending"}, {**correct, "imageSha256": "bad"},
                      {**correct, "mac": "wrong"}, {**correct, "partition": "app0"}, correct]
        with patch.object(ota, "_request", side_effect=[(200,item) for item in candidates]), patch.object(ota.time, "sleep"):
            result = ota._wait_verified("2.0.0", "78"*32, info(), wifi, "Yshand_1234", "temporary")
        self.assertEqual(result, correct)

    def test_verify_recovers_after_reboot_network_errors(self):
        wifi = FakeWifi(); wifi.ssid = "Yshand_1234"
        correct = info(version="2.0.0", imageSha256="78"*32, partition="app1")
        with patch.object(ota, "_request", side_effect=[OSError("reboot"),(200,correct)]), patch.object(ota.time, "sleep"):
            self.assertEqual(ota._wait_verified("2.0.0", "78"*32, info(), wifi, "Yshand_1234", "temporary"), correct)

    def run_fake_batch(self, **overrides):
        wifi, events = FakeWifi(), []
        defaults = {"_connect": info(), "_login": "fresh-token", "_request": (200,{"ok":True,"chip":"ESP32-S3","maxSize":6553600}),
                    "_upload": {"ok":True}, "_wait_verified": info(version="2.0.0",partition="app1")}
        defaults.update(overrides)
        patches = [patch.object(ota,"Wifi",return_value=wifi)]
        for name,value in defaults.items():
            patches.append(patch.object(ota,name,side_effect=value) if isinstance(value,Exception) else patch.object(ota,name,return_value=value))
        mocks = {}
        try:
            for p in patches:
                mocked = p.start(); mocks[p.attribute] = mocked
            results = ota.run_batch(["Yshand_1234"], self.path, output_path=self.temp.name,
                                    callback=lambda kind,data: events.append((kind,data)))
        finally:
            for p in reversed(patches): p.stop()
        return results,wifi,events,mocks

    def test_batch_success_restores_network_before_done(self):
        results,wifi,events,mocks = self.run_fake_batch()
        self.assertTrue(results[0].ok)
        self.assertEqual(wifi.history[0], ("connect","原网络"))
        self.assertEqual(wifi.history[-1], ("close",))
        self.assertEqual(events[-1][0], "done")
        mocks["_upload"].assert_called_once_with(image_bytes(), "fresh-token")
        report = json.loads(next(Path(self.temp.name).glob("batch-ota-*.json")).read_text(encoding="utf-8"))
        self.assertTrue(report["devices"][0]["ok"])
        self.assertNotIn("fresh-token", json.dumps(report))

    def test_busy_or_oversize_never_uploads(self):
        for overrides in [{"_connect":info(busy=True)}, {"_connect":info(maintenance=True)},
                          {"_request":(200,{"ok":True,"chip":"ESP32-S3","maxSize":1})}]:
            result,wifi,_,mocks = self.run_fake_batch(**overrides)
            self.assertFalse(result[0].ok)
            mocks["_upload"].assert_not_called()
            self.assertEqual(wifi.history[-1], ("close",))

    def test_failed_upload_does_not_claim_success(self):
        result,_,_,mocks = self.run_fake_batch(_upload=ota.BatchOtaError("image rejected"))
        self.assertFalse(result[0].ok)
        mocks["_wait_verified"].assert_not_called()

    def test_lost_success_response_rechecks_flash_identity(self):
        result,_,events,mocks = self.run_fake_batch(_upload=OSError("response lost"))
        self.assertTrue(result[0].ok)
        mocks["_wait_verified"].assert_called_once()
        self.assertTrue(any(kind=="warning" for kind,_ in events))

    def test_cancel_before_start_never_uploads(self):
        wifi = FakeWifi(); stop = threading.Event(); stop.set()
        with patch.object(ota,"Wifi",return_value=wifi), patch.object(ota,"_upload") as upload:
            self.assertEqual(ota.run_batch(["Yshand_1234"],self.path,output_path=self.temp.name,stop_event=stop),[])
            upload.assert_not_called()
        self.assertEqual(wifi.history[-1], ("close",))

    def test_discovery_only_reads_and_restores(self):
        wifi,events = FakeWifi(),[]
        with patch.object(ota,"scan_airhand",return_value=["Yshand_1234"]), patch.object(ota,"Wifi",return_value=wifi), \
                patch.object(ota,"_connect",return_value=info()) as connect, patch.object(ota,"_upload") as upload:
            result = ota.discover_devices(callback=lambda kind,data:events.append((kind,data)))
            self.assertEqual(result,[info()]); upload.assert_not_called(); connect.assert_called_once()
        self.assertEqual(events[-1][0],"probe_done")
        self.assertEqual(wifi.history[0],("connect","原网络"))


if __name__ == "__main__":
    unittest.main()
