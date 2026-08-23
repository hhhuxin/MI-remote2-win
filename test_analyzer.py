import json
import unittest
from unittest.mock import patch

from Xiaomi32B8_HID_Analyzer import DeviceInfo, EventRecorder


class RecorderTests(unittest.TestCase):
    def test_raw_and_json_exports_keep_all_bytes(self):
        r = EventRecorder(DeviceInfo())
        r.add("RAW_INPUT", bytes.fromhex("5D 00 02 00"), report_id="5D", event_type="DOWN")
        r.add("RAW_INPUT", bytes.fromhex("5D 00 03 00"), report_id="5D", event_type="UP")
        writes = []
        with patch("pathlib.Path.write_text", side_effect=lambda text, **kwargs: writes.append(text)):
            r.export_raw("a.log"); r.export_json("a.json")
        self.assertIn("5D 00 02 00", writes[0])
        payload = json.loads(writes[1])
        self.assertEqual(payload["summary"]["total_events"], 2)
        self.assertEqual(payload["events"][0]["data"], "5D 00 02 00")

    def test_summary_deduplicates_reports(self):
        r = EventRecorder(DeviceInfo())
        for _ in range(3): r.add("RAW_INPUT", b"abc")
        self.assertEqual(r.summary()["unique_reports"], 1)
        self.assertEqual(r.summary()["reports"][0]["count"], 3)


if __name__ == "__main__": unittest.main()
