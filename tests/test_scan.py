import sys
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import batch_ota as ota
from wifi_windows import BssEntry, fresh_networks


class ScanWifi:
    def __init__(self, rounds):
        self.rounds = iter(rounds)
        self.latest = []
        self.calls = 0
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def scan(self, **_):
        self.calls += 1
        value = next(self.rounds)
        if isinstance(value, Exception): raise value
        self.latest = value
    def networks(self): return self.latest


def network(ssid, secure=False):
    return {"ssid": ssid, "secure": secure}


class ScanTests(unittest.TestCase):
    def test_broadcast_timestamp_rejects_stale_cached_and_invalid_entries(self):
        now = 134378273914773936
        entries = []
        for age in [0, 3, 10, 11, 300]:
            entry = BssEntry()
            name = ("Yshand_" + str(age)).encode()
            entry.ssid.length = len(name)
            entry.ssid.value[:len(name)] = name
            entry.host_timestamp = now - age * 10_000_000
            entry.quality = 90
            entries.append(entry)
        entries.append(BssEntry())  # 未报告时间的无效记录。
        self.assertEqual([n['ssid'] for n in fresh_networks(entries, now)],
                         ['Yshand_0', 'Yshand_3', 'Yshand_10'])

    def test_three_rounds_merge_late_results_and_do_not_drop_earlier_devices(self):
        wifi = ScanWifi([[network("Yshand_1111")],
                         [network("Yshand_2222"), network("Other"), network("Yshand_secure", True)],
                         [network("Yshand_3333"), network("Yshand_1111")]])
        events = []
        with patch.object(ota, "Wifi", return_value=wifi), patch.object(threading.Event, "wait", return_value=False):
            found = ota.scan_airhand(callback=lambda k,d: events.append((k,d)))
        self.assertEqual(found, ["Yshand_1111", "Yshand_2222", "Yshand_3333"])
        self.assertEqual(wifi.calls, 3)
        self.assertEqual([len(d) for k,d in events if k == "scan"], [1, 2, 3])

    def test_failed_round_retries_and_preserves_results(self):
        wifi = ScanWifi([[network("Yshand_1111")], ota.WifiError("driver busy"), [network("Yshand_2222")]])
        with patch.object(ota, "Wifi", return_value=wifi), patch.object(threading.Event, "wait", return_value=False):
            self.assertEqual(ota.scan_airhand(), ["Yshand_1111", "Yshand_2222"])

    def test_cancel_does_not_start_a_scan(self):
        event = threading.Event(); event.set()
        wifi = ScanWifi([])
        with patch.object(ota, "Wifi", return_value=wifi):
            self.assertEqual(ota.scan_airhand(stop_event=event), [])
        self.assertEqual(wifi.calls, 0)


if __name__ == "__main__": unittest.main()
