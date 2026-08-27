import unittest
from unittest.mock import patch
from XiaomiRemote2_Windows import ACTION_VK, DEFAULT_ACTIONS, HID_USAGE_TO_BUTTON, REMOTE_BUTTONS, RawInputListener, _INPUT, path_matches, normalize_path
from xiaomi_remote2_ui import clear_all_text, parse_key_combo, send_combo_down, send_combo_up
from xiaomi_remote2_protocol import Capabilities, IMAADPCMDecoder, mic_open_command


class Remote2IdentityTests(unittest.TestCase):
    def test_remote_button_profile_has_thirteen_controls(self):
        self.assertEqual(len(REMOTE_BUTTONS), 13)
        self.assertEqual(HID_USAGE_TO_BUTTON[0x3E], "voice")
        self.assertEqual(HID_USAGE_TO_BUTTON[0xF1], "back")
        self.assertEqual(HID_USAGE_TO_BUTTON[0x29], "back")
        self.assertEqual(HID_USAGE_TO_BUTTON[0x0224], "back")

    def test_default_independent_keys_use_function_key_range(self):
        self.assertEqual(ACTION_VK["F13"], 0x7C)
        self.assertEqual(ACTION_VK["F24"], 0x87)
        self.assertEqual(ACTION_VK["F1"], 0x70)
        self.assertEqual(ACTION_VK["KEY_1"], 0x31)
        self.assertEqual(__import__("ctypes").sizeof(_INPUT), 40)
        self.assertEqual(len(DEFAULT_ACTIONS), 13)
        self.assertEqual(DEFAULT_ACTIONS["tv"], "REMOTE_TV")

    def test_hid_usage_fallback_recognizes_known_usage_byte(self):
        listener = RawInputListener(lambda *_args: None)
        self.assertEqual(listener._hid_usages(None, None, bytes([0x00, 0x4F]), 0), {0x4F})

    def test_rc003_snapshot_report_decodes_three_usage_slots(self):
        listener = RawInputListener(lambda *_args: None)
        report = bytes.fromhex("01 00 00 4F 00 00 00 00 00")
        self.assertEqual(listener._decode_rc003_report(report), {0x4F})

    def test_hid_snapshot_emits_press_and_release_edges(self):
        events = []
        listener = RawInputListener(lambda *event: events.append(event))
        def body(report):
            return len(report).to_bytes(4, "little") + (1).to_bytes(4, "little") + report
        listener._handle_hid(None, None, body(bytes.fromhex("01 00 00 4F 00 00 00 00 00")), "path")
        listener._handle_hid(None, None, body(bytes.fromhex("01 00 00 00 00 00 00 00 00")), "path")
        self.assertEqual([(event[1], event[2]) for event in events], [("DOWN", "right"), ("UP", "right")])

    def test_back_snapshot_emits_press_and_release_edges(self):
        events = []
        listener = RawInputListener(lambda *event: events.append(event))
        def body(report):
            return len(report).to_bytes(4, "little") + (1).to_bytes(4, "little") + report
        listener._handle_hid(None, None, body(bytes.fromhex("01 00 00 F1 00 00 00 00 00")), "path")
        listener._handle_hid(None, None, body(bytes.fromhex("01 00 00 00 00 00 00 00 00")), "path")
        self.assertEqual([(event[1], event[2]) for event in events], [("DOWN", "back"), ("UP", "back")])

    def test_mapping_parser_supports_modifier_only_and_main_key_combos(self):
        self.assertEqual(parse_key_combo("Ctrl + Win"), ((0x11, 0x5B), None))
        self.assertEqual(parse_key_combo("Ctrl + Shift + S"), ((0x11, 0x10), 0x53))
        self.assertEqual(parse_key_combo("确定"), ((), 0x0D))
        self.assertEqual(parse_key_combo("1"), ((), 0x31))

    def test_mapping_injector_keeps_modifier_only_press_release_balanced(self):
        with patch("xiaomi_remote2_ui._send_key") as send:
            combo = parse_key_combo("Ctrl + Win")
            send_combo_down(*combo); send_combo_up(*combo)
        self.assertEqual([call.args for call in send.call_args_list], [(0x11, False), (0x5B, False), (0x5B, True), (0x11, True)])

    def test_clear_all_text_selects_everything_then_backspaces(self):
        with patch("xiaomi_remote2_ui._send_key") as send:
            clear_all_text()
        self.assertEqual([call.args for call in send.call_args_list], [
            (0x41, False, (0x11,)), (0x41, True, (0x11,)),
            (0x08, False, ()), (0x08, True, ()),
        ])

    def test_ble_hid_path_variants(self):
        self.assertTrue(path_matches(r"\\?\HID#{00001812}_Dev_VID&012717_PID&32B8_REV&00A4\kbd"))
        self.assertTrue(path_matches(r"HID\\VID_2717&PID_32B8\\kbd"))
        self.assertFalse(path_matches(r"HID\\VID_1234&PID_32B8\\kbd"))

    def test_path_normalization(self):
        self.assertEqual(normalize_path("  ABC "), "abc")

    def test_atvv_profile_is_shared(self):
        self.assertEqual(mic_open_command(), b"\x0c\x00")
        caps = Capabilities.parse(bytes.fromhex("0B 01 00 02 03 00 78"))
        self.assertEqual((caps.selected_codec, caps.frame_size, caps.sample_rate), (2, 120, 16000))
        self.assertEqual(len(IMAADPCMDecoder().decode(b"\x00\x11")), 4)


if __name__ == "__main__": unittest.main()
