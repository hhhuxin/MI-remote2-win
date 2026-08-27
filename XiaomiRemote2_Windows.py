"""Windows client for Xiaomi Bluetooth Remote 2 / 2 Pro.

The HID path intentionally follows the community Windows implementation from
HD838A/remote-mic-app's RC003 candidate: a dedicated message-only Win32 window
on a background thread, Raw Input registration, and exact device-path
filtering. It never opens the ``\\kbd`` handle directly (Windows kbdhid owns it
and returns ACCESS_DENIED).
"""
from __future__ import annotations

import ctypes
import json
import os
import struct
import sys
import threading
import time
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Button, Frame, Label, StringVar, Tk, filedialog, messagebox
from tkinter import ttk

from Xiaomi32B8_HID_Analyzer import CaptureEvent, DeviceInfo, EventRecorder
from xiaomi_remote2_ble import ATVVVoiceController, PCMOutput

VID, PID = 0x2717, 0x32B8
DEVICE_NAMES = {"mi rc", "xiaomi bluetooth remote 2", "xiaomi bluetooth remote 2 pro", "小米蓝牙语音遥控器"}
REMOTE_BUTTONS = ("power", "voice", "up", "down", "left", "right", "ok", "back", "volume_plus", "volume_minus", "home", "menu", "tv")
BUTTON_LABELS = {
    "power": "电源", "voice": "语音", "up": "上", "down": "下", "left": "左", "right": "右",
    "ok": "确定", "back": "返回", "volume_plus": "音量+", "volume_minus": "音量-", "home": "主页", "menu": "菜单", "tv": "TV",
}
BUTTON_BY_VKEY = {
    0x27: "right", 0x25: "left", 0x28: "down", 0x26: "up", 0x0D: "ok",
    0x24: "home", 0x5D: "menu", 0x74: "voice", 0xE5: "voice",
    0xC0: "tv", 0x5F: "power", 0xA6: "back", 0x08: "back", 0xAD: "mute", 0xAF: "volume_plus", 0xAE: "volume_minus",
}
BUTTON_BY_MAKE = {0x6A: "back", 0x5E: "power", 0x30: "volume_plus", 0x2E: "volume_minus", 0x20: "mute"}
HID_USAGE_TO_BUTTON = {
    0x66: "power", 0x3E: "voice", 0x52: "up", 0x51: "down", 0x50: "left", 0x4F: "right",
    0x28: "ok", 0x29: "back", 0xF1: "back", 0x0224: "back",
    0x80: "volume_plus", 0x81: "volume_minus", 0x4A: "home", 0x65: "menu", 0x35: "tv",
}

# These outputs are intentionally unused function keys. The app never hooks or
# rewrites normal keyboard input; it only emits a synthetic key when a remote
# event is received. Users can change each mapping in the UI and config file.
ACTION_LABELS = {
    "none": "仅记录，不输出",
    "VOICE": "语音：按住说话",
    "F13": "独立键 F13", "F14": "独立键 F14", "F15": "独立键 F15", "F16": "独立键 F16",
    "F17": "独立键 F17", "F18": "独立键 F18", "F19": "独立键 F19", "F20": "独立键 F20",
    "F21": "独立键 F21", "F22": "独立键 F22", "F23": "独立键 F23", "F24": "独立键 F24",
    "REMOTE_TV": "独立键 TV（OEM-8）",
    "UP": "标准键 上", "DOWN": "标准键 下", "LEFT": "标准键 左", "RIGHT": "标准键 右",
    "ENTER": "标准键 Enter", "BACKSPACE": "标准键 Backspace", "ALT_TAB": "标准键 Alt+Tab",
    "VOLUME_UP": "系统音量+", "VOLUME_DOWN": "系统音量-", "MUTE": "系统静音", "PLAY_PAUSE": "播放/暂停",
}
ACTION_VK = {
    **{f"F{i}": 0x6F + i for i in range(13, 25)},
    "REMOTE_TV": 0xDF,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27, "ENTER": 0x0D, "BACKSPACE": 0x08,
    "VOLUME_UP": 0xAF, "VOLUME_DOWN": 0xAE, "MUTE": 0xAD, "PLAY_PAUSE": 0xB3,
}
ACTION_VK.update({f"F{i}": 0x6F + i for i in range(1, 13)})
ACTION_VK.update({"ESC": 0x1B, "TAB": 0x09, "SPACE": 0x20, "DELETE": 0x2E, "INSERT": 0x2D, "PAGE_UP": 0x21, "PAGE_DOWN": 0x22, "APPS": 0x5D, "BROWSER_BACK": 0xA6, "MEDIA_NEXT": 0xB0, "MEDIA_PREVIOUS": 0xB1})
ACTION_LABELS.update({f"F{i}": f"键盘 F{i}" for i in range(1, 13)})
ACTION_LABELS.update({"ESC": "键盘 Esc", "TAB": "键盘 Tab", "SPACE": "键盘 Space", "DELETE": "键盘 Delete", "INSERT": "键盘 Insert", "PAGE_UP": "键盘 PageUp", "PAGE_DOWN": "键盘 PageDown", "APPS": "键盘菜单键", "BROWSER_BACK": "键盘浏览器返回", "MEDIA_NEXT": "媒体下一首", "MEDIA_PREVIOUS": "媒体上一首"})
ACTION_LABELS["KEY_1"] = "键盘数字 1"
ACTION_VK["KEY_1"] = 0x31
DEFAULT_ACTIONS = {button: f"F{13 + index}" for index, button in enumerate(REMOTE_BUTTONS[:12])}
DEFAULT_ACTIONS["tv"] = "REMOTE_TV"
DEFAULT_ACTIONS["voice"] = "VOICE"


def path_matches(path: str) -> bool:
    p = path.lower()
    return ((f"vid_{VID:04x}" in p and f"pid_{PID:04x}" in p) or
            (f"dev_vid&{VID:06x}" in p and f"pid&{PID:04x}" in p) or
            (f"dev_vid&01{VID:04x}" in p and f"pid&{PID:04x}" in p))


def normalize_path(path: str) -> str:
    return path.strip().lower()

def display_device_path(path: str, index: int) -> str:
    """Keep the selector readable while retaining the full path internally."""
    tail = path.replace("\\", "/").split("/")[-1]
    return f"{index}: {tail[-72:]}"


class RawInputUnavailableError(RuntimeError):
    pass


class RawInputDeviceList(ctypes.Structure):
    _fields_ = [("hDevice", wintypes.HANDLE), ("dwType", wintypes.DWORD)]


def _win32():
    if sys.platform != "win32":
        raise RawInputUnavailableError("Raw Input is only available on Windows")
    return ctypes.windll.user32, ctypes.windll.kernel32


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32), ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16), ("wParamH", ctypes.c_uint16)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_key(vk: int, released: bool = False, modifiers: tuple[int, ...] = ()) -> None:
    """Emit one tagged synthetic key event; physical keyboard events are untouched."""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    flags = 0x0002 if released else 0
    marker = 0x584D5232  # XMR2; no global hook consumes this event.
    for modifier in modifiers:
        item = _INPUT(type=1, ki=_KEYBDINPUT(modifier, 0, flags, 0, marker))
        user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT))
    item = _INPUT(type=1, ki=_KEYBDINPUT(vk, 0, flags, 0, marker))
    user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT))
    if released:
        for modifier in reversed(modifiers):
            item = _INPUT(type=1, ki=_KEYBDINPUT(modifier, 0, flags, 0, marker))
            user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT))


def _device_name(user32, handle) -> str | None:
    RIDI_DEVICENAME = 0x20000007
    user32.GetRawInputDeviceInfoW.argtypes = (wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID, ctypes.POINTER(wintypes.UINT))
    user32.GetRawInputDeviceInfoW.restype = ctypes.c_uint
    size = wintypes.UINT(0)
    user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, None, ctypes.byref(size))
    if not size.value: return None
    buffer = ctypes.create_unicode_buffer(size.value)
    if user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, buffer, ctypes.byref(size)) in (0, 0xFFFFFFFF): return None
    return buffer.value


def enumerate_device_paths() -> list[str]:
    user32, _ = _win32()
    user32.GetRawInputDeviceList.argtypes = (ctypes.POINTER(RawInputDeviceList), ctypes.POINTER(wintypes.UINT), wintypes.UINT)
    user32.GetRawInputDeviceList.restype = ctypes.c_uint
    count = wintypes.UINT(0)
    if user32.GetRawInputDeviceList(None, ctypes.byref(count), ctypes.sizeof(RawInputDeviceList)) != 0:
        raise RawInputUnavailableError(f"GetRawInputDeviceList failed: {ctypes.get_last_error()}")
    items = (RawInputDeviceList * count.value)()
    written = user32.GetRawInputDeviceList(items, ctypes.byref(count), ctypes.sizeof(RawInputDeviceList))
    paths = []
    for i in range(int(written)):
        path = _device_name(user32, items[i].hDevice)
        if path and path_matches(path): paths.append(path)
    return list(dict.fromkeys(paths))


class RawInputListener:
    def __init__(self, callback, status_callback=None):
        self.callback = callback
        self.status_callback = status_callback or (lambda _msg: None)
        self.thread = None
        self.hwnd = None
        self.running = False
        self.stop_event = threading.Event()
        self.path = None
        self.class_name = None
        self._wndproc_keepalive = None
        self._pressed: set[str] = set()
        self._hid_pressed: set[str] = set()
        self._preparsed_cache: dict[int, ctypes.Array] = {}
        self.thread_id = None
        self.capture_all = os.environ.get("XMR2_CAPTURE_ALL") == "1"

    def start(self, selected_path: str | None = None):
        if self.running: raise RawInputUnavailableError("Raw Input listener already running")
        paths = enumerate_device_paths()
        if self.capture_all:
            paths = []
        if len(paths) == 0:
            # Some Windows BLE HID stacks do not expose the collection through
            # GetRawInputDeviceList until a registration exists. Register
            # broadly and bind to the first event whose path proves VID/PID;
            # every event is still filtered by the exact path thereafter.
            self.status_callback("No pre-enumerated path; using first matching Raw Input event")
            self.path = None
        else:
            self.status_callback(f"Found {len(paths)} matching path(s)")
        if selected_path:
            if paths and normalize_path(selected_path) not in {normalize_path(p) for p in paths}:
                raise RawInputUnavailableError("所选遥控器路径已消失，请刷新设备列表")
            self.path = selected_path
        elif len(paths) > 1:
            raise RawInputUnavailableError(f"检测到 {len(paths)} 个遥控器，请在设备列表中选择一个")
        elif paths:
            self.path = paths[0]
        self.stop_event.clear(); self.running = True
        self.thread = threading.Thread(target=self._run, name="xiaomi-raw-input", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.hwnd and sys.platform == "win32":
            try:
                u = ctypes.windll.user32; u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM); u.PostMessageW.restype = wintypes.BOOL
                u.PostMessageW(self.hwnd, 0x0010, 0, 0)
            except Exception: pass
        if self.thread_id and sys.platform == "win32":
            try:
                u = ctypes.windll.user32
                u.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
                u.PostThreadMessageW.restype = wintypes.BOOL
                u.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
            except Exception: pass
        if self.thread:
            self.thread.join(timeout=2.0)
            if self.thread.is_alive(): raise RawInputUnavailableError("Raw Input listener thread did not stop")
        self.thread = None; self.hwnd = None; self.running = False; self.thread_id = None
        for button in tuple(self._pressed): self.callback(b"", "UP", button, "forced release")
        self._pressed.clear()
        self._hid_pressed.clear()

    def _run(self):
        user32, kernel32 = _win32()
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self.thread_id = kernel32.GetCurrentThreadId()
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]
        class RAWINPUTDEVICE(ctypes.Structure):
            _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT), ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]
        class HEADER(ctypes.Structure):
            _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD), ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]
        user32.GetRawInputData.argtypes = (wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID, ctypes.POINTER(wintypes.UINT), wintypes.UINT)
        user32.GetRawInputData.restype = ctypes.c_uint
        user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),); user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID); user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM); user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DestroyWindow.argtypes = (wintypes.HWND,); user32.DestroyWindow.restype = wintypes.BOOL
        user32.PostQuitMessage.argtypes = (ctypes.c_int,); user32.PostQuitMessage.restype = None
        user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT); user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),); user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.RegisterRawInputDevices.argtypes = (ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT); user32.RegisterRawInputDevices.restype = wintypes.BOOL
        class_name = f"XiaomiRemote2Raw-{uuid.uuid4().hex}"; self.class_name = class_name
        def wndproc(hwnd, msg, wparam, lparam):
            try:
                if msg == 0x00FF:
                    self._handle_raw(user32, lparam, HEADER)
                    return 0
                if msg == 0x0010:
                    user32.DestroyWindow(hwnd); return 0
                if msg == 0x0002:
                    self.hwnd = None; user32.PostQuitMessage(0); return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            except Exception as exc:
                self.status_callback(f"WM_INPUT ERROR: {type(exc).__name__}: {exc}")
                return 0
        wndproc_cb = WNDPROC(wndproc); self._wndproc_keepalive = wndproc_cb
        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW(0, wndproc_cb, 0, 0, hinstance, None, None, None, None, class_name)
        registered = False
        try:
            if not user32.RegisterClassW(ctypes.byref(wc)): raise RawInputUnavailableError(f"RegisterClassW failed: {ctypes.get_last_error()}")
            registered = True
            hwnd_message = ctypes.c_void_p(-3)
            self.hwnd = user32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0, hwnd_message, None, hinstance, None)
            if not self.hwnd: raise RawInputUnavailableError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
            # RIDEV_NOLEGACY applies to the entire keyboard usage, not to one
            # selected device, and would disable the user's physical keyboard.
            input_sink = 0x100
            devices = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(0x01, 0x06, input_sink, self.hwnd), RAWINPUTDEVICE(0x0C, 0x01, input_sink, self.hwnd))
            if not user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(RAWINPUTDEVICE)): raise RawInputUnavailableError(f"RegisterRawInputDevices failed: {ctypes.get_last_error()}")
            self.status_callback(f"READY path={self.path}")
            msg = wintypes.MSG()
            while not self.stop_event.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0: break
                user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self.status_callback(f"RAW INPUT ERROR: {type(exc).__name__}: {exc}")
        finally:
            if self.hwnd: user32.DestroyWindow(self.hwnd); self.hwnd = None
            if registered:
                try:
                    remove = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(0x01, 0x06, 1, None), RAWINPUTDEVICE(0x0C, 0x01, 1, None))
                    user32.RegisterRawInputDevices(remove, 2, ctypes.sizeof(RAWINPUTDEVICE))
                except Exception: pass
            if registered:
                try: user32.UnregisterClassW(class_name, hinstance)
                except Exception: pass

    def _handle_raw(self, user32, lparam, header_type):
        size = wintypes.UINT(0); header_size = ctypes.sizeof(header_type)
        user32.GetRawInputData(lparam, 0x10000003, None, ctypes.byref(size), header_size)
        if not size.value: return
        buf = ctypes.create_string_buffer(size.value)
        if user32.GetRawInputData(lparam, 0x10000003, buf, ctypes.byref(size), header_size) != size.value: return
        header = header_type.from_buffer_copy(buf, 0); path = _device_name(user32, header.hDevice)
        if not path or (not self.capture_all and not path_matches(path)): return
        if self.capture_all:
            self.callback(b"", "DIAGNOSTIC", "UNKNOWN", f"path={path} raw_type={header.dwType}")
        else:
            if self.path is None:
                self.path = path
                self.status_callback(f"BOUND path={path}")
            if normalize_path(path) != normalize_path(self.path): return
        body = bytes(buf.raw[header_size:size.value])
        if header.dwType == 1 and len(body) >= 16:
            make, flags, _reserved, vkey, message, extra = struct.unpack_from("<HHHHII", body, 0)
            button = BUTTON_BY_VKEY.get(vkey) or (BUTTON_BY_MAKE.get(make) if vkey == 0xFF else None)
            pressed = message not in (0x0101, 0x0105)
            edge = "DOWN" if pressed and button not in self._pressed else ("REPEAT" if pressed else "UP")
            if button:
                if pressed: self._pressed.add(button)
                else: self._pressed.discard(button)
            self.callback(body, edge, button or "UNKNOWN", f"path={path} vkey=0x{vkey:02X} make=0x{make:02X} flags=0x{flags:04X} message=0x{message:04X} extra=0x{extra:08X}")
        elif header.dwType == 2:
            self._handle_hid(user32, header.hDevice, body, path)

    def _handle_hid(self, user32, device, body: bytes, path: str) -> None:
        """Decode HID Consumer/Keyboard reports through hid.dll's preparsed data."""
        if len(body) < 8:
            self.callback(body, "UNKNOWN", "UNKNOWN", f"path={path} raw_type=HID short_report")
            return
        size_hid, count = struct.unpack_from("<II", body, 0)
        if not size_hid or not count:
            self.callback(body, "UNKNOWN", "UNKNOWN", f"path={path} raw_type=HID empty_report")
            return
        raw = body[8:]
        device_key = int(ctypes.cast(device, ctypes.c_void_p).value or 0)
        emitted_edge = False
        for index in range(min(int(count), len(raw) // int(size_hid))):
            report = raw[index * size_hid:(index + 1) * size_hid]
            usages = self._hid_usages(user32, device, report, device_key)
            buttons = {HID_USAGE_TO_BUTTON[u] for u in usages if u in HID_USAGE_TO_BUTTON}
            if not buttons and not self._hid_pressed:
                continue
            pressed = buttons - self._hid_pressed
            released = self._hid_pressed - buttons
            self._hid_pressed = buttons
            if buttons and not pressed and not released:
                for button in sorted(buttons):
                    emitted_edge = True
                    self.callback(report, "REPEAT", button, f"path={path} raw_type=HID usage={self._usage_text(usages)}")
            for button in sorted(pressed):
                emitted_edge = True
                self.callback(report, "DOWN", button, f"path={path} raw_type=HID usage={self._usage_text(usages)}")
            for button in sorted(released):
                emitted_edge = True
                self.callback(report, "UP", button, f"path={path} raw_type=HID usage={self._usage_text(usages)}")
        if not emitted_edge:
            self.callback(raw[:size_hid], "UNKNOWN", "UNKNOWN", f"path={path} raw_type=HID usages=unrecognized")

    @staticmethod
    def _usage_text(usages: set[int]) -> str:
        return ",".join(f"0x{u:02X}" for u in sorted(usages)) or "none"

    def _hid_usages(self, user32, device, report: bytes, device_key: int) -> set[int]:
        """Return usage IDs from a HID report; use a byte fallback for older stacks."""
        exact = self._decode_rc003_report(report)
        if exact is not None:
            return exact
        try:
            RIDI_PREPARSEDDATA = 0x20000005
            user32.GetRawInputDeviceInfoW.argtypes = (wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID, ctypes.POINTER(wintypes.UINT))
            user32.GetRawInputDeviceInfoW.restype = ctypes.c_uint
            preparsed = self._preparsed_cache.get(device_key)
            if preparsed is None:
                size = wintypes.UINT(0)
                user32.GetRawInputDeviceInfoW(device, RIDI_PREPARSEDDATA, None, ctypes.byref(size))
                if not size.value: raise RuntimeError("preparsed data unavailable")
                preparsed = ctypes.create_string_buffer(size.value)
                if user32.GetRawInputDeviceInfoW(device, RIDI_PREPARSEDDATA, preparsed, ctypes.byref(size)) in (0, 0xFFFFFFFF):
                    raise RuntimeError("preparsed data query failed")
                self._preparsed_cache[device_key] = preparsed
            class USAGE_AND_PAGE(ctypes.Structure):
                _fields_ = [("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT)]
            hid = ctypes.WinDLL("hid.dll")
            hid.HidP_GetUsagesEx.argtypes = (ctypes.c_int, wintypes.USHORT, ctypes.POINTER(USAGE_AND_PAGE), ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG)
            hid.HidP_GetUsagesEx.restype = wintypes.LONG
            items = (USAGE_AND_PAGE * 64)(); length = wintypes.ULONG(64)
            report_buf = ctypes.create_string_buffer(report)
            status = hid.HidP_GetUsagesEx(0, 0, items, ctypes.byref(length), ctypes.cast(preparsed, ctypes.c_void_p), ctypes.cast(report_buf, ctypes.c_void_p), len(report))
            if status == 0:
                return {int(item.Usage) for item in items[:length.value]}
        except Exception:
            pass
        # Xiaomi's Consumer reports carry one usage byte; this fallback keeps
        # the listener useful when a vendor HID parser is unavailable.
        return {value for value in report if value in HID_USAGE_TO_BUTTON}

    @staticmethod
    def _decode_rc003_report(report: bytes) -> set[int] | None:
        """Decode the upstream RC003 snapshot: report-id prefix + 3 uint16 slots."""
        if len(report) == 9 and report[:3] == b"\x01\x00\x00":
            payload = report[3:]
        elif len(report) == 7 and report[0] == 0x01:
            payload = report[1:]
        elif len(report) == 6:
            payload = report
        else:
            return None
        return {usage for usage, in struct.iter_unpack("<H", payload) if usage}


class XiaomiRemote2App:
    def __init__(self, root: Tk):
        self.root = root; root.title("Xiaomi Bluetooth Remote 2 Windows"); root.geometry("1180x720")
        self.device = DeviceInfo(); self.device.vid = f"0x{VID:04X}"; self.device.pid = f"0x{PID:04X}"; self.device.manufacturer = "MIOM"
        self.recorder = EventRecorder(self.device); self.listener = RawInputListener(self.on_raw, self.on_status); self.listening = False
        self.status = StringVar(value="● 已停止"); self.layer = StringVar(value="Raw Input: 未启动")
        self.mapping_file = Path(__file__).with_name("xiaomi_remote2_mapping.json")
        self.mapping = dict(DEFAULT_ACTIONS); self.mapping_vars: dict[str, StringVar] = {}; self.output_held: set[str] = set()
        self.path_var = StringVar(value="自动选择（仅连接一个时）"); self.path_values: list[str] = []
        self.voice_status = StringVar(value="BLE 语音：未连接"); self.audio_var = StringVar(value="请选择音频输出设备")
        self.voice = ATVVVoiceController(self.on_voice_status)
        self.load_mapping()
        self.build_ui(); root.protocol("WM_DELETE_WINDOW", self.close)

    def build_ui(self):
        top = Frame(self.root); top.pack(fill=X, padx=8, pady=6)
        Label(top, textvariable=self.status, fg="#16794c").pack(side=LEFT)
        # Keep a long device path from pushing the action buttons off-screen.
        Label(top, textvariable=self.layer, width=58, anchor="w").pack(side=LEFT, padx=20)
        for text, cmd in (("启动小米遥控器 2", self.start_all), ("开始监听", self.start), ("停止监听", self.stop), ("清空", self.clear), ("BLE诊断", self.ble_inspect), ("保存", self.save)):
            Button(top, text=text, command=cmd).pack(side=RIGHT, padx=3)

        device_row = Frame(self.root); device_row.pack(fill=X, padx=8, pady=(0, 4))
        Label(device_row, text="遥控器设备:").pack(side=LEFT)
        self.path_combo = ttk.Combobox(device_row, textvariable=self.path_var, state="readonly", width=92)
        self.path_combo.pack(side=LEFT, fill=X, expand=True, padx=8)
        Button(device_row, text="刷新设备", command=self.refresh_paths).pack(side=RIGHT)

        info = ttk.LabelFrame(self.root, text="设备"); info.pack(fill=X, padx=8, pady=4)
        Label(info, text="VID 0x2717   PID 0x32B8   支持：Xiaomi Bluetooth Remote 2 / 2 Pro", anchor="w").pack(fill=X, padx=8, pady=4)

        voice = ttk.LabelFrame(self.root, text="语音（程序输出选 CABLE Input；麦克风测试选 CABLE Output）")
        voice.pack(fill=X, padx=8, pady=4)
        Label(voice, textvariable=self.voice_status, width=42, anchor="w").pack(side=LEFT, padx=6)
        Label(voice, text="程序播放到:").pack(side=LEFT)
        self.audio_combo = ttk.Combobox(voice, textvariable=self.audio_var, state="readonly", width=38)
        self.audio_combo.pack(side=LEFT, padx=6)
        Button(voice, text="刷新音频", command=self.refresh_audio_outputs).pack(side=LEFT, padx=2)
        Button(voice, text="连接语音", command=self.connect_voice).pack(side=LEFT, padx=2)
        self.refresh_audio_outputs()
        Label(voice, text="系统输入:").pack(side=LEFT, padx=(12, 2))
        self.input_var = StringVar(value="请选择系统输入设备")
        self.input_combo = ttk.Combobox(voice, textvariable=self.input_var, state="readonly", width=30)
        self.input_combo.pack(side=LEFT, padx=2)
        self.refresh_input_devices()

        mapping = ttk.LabelFrame(self.root, text="按键映射（默认输出独立虚拟键，不拦截实体键盘）")
        mapping.pack(fill=X, padx=8, pady=4)
        for index, button in enumerate(REMOTE_BUTTONS):
            column = index // 7
            row = index % 7
            cell = Frame(mapping); cell.grid(row=row, column=column, sticky="ew", padx=8, pady=2)
            mapping.grid_columnconfigure(column, weight=1)
            Label(cell, text=BUTTON_LABELS[button], width=8, anchor="w").pack(side=LEFT)
            var = StringVar(value=ACTION_LABELS.get(self.mapping.get(button, "none"), ACTION_LABELS["none"]))
            self.mapping_vars[button] = var
            combo = ttk.Combobox(cell, textvariable=var, values=list(ACTION_LABELS.values()), state="readonly", width=19)
            combo.pack(side=LEFT, fill=X, expand=True)
        Button(mapping, text="保存映射", command=self.save_mapping).grid(row=7, column=0, columnspan=2, pady=4)

        cols = ("time", "source", "edge", "button", "length", "hex", "details"); self.tree = ttk.Treeview(self.root, columns=cols, show="headings")
        names = {"time":"时间", "source":"来源", "edge":"边沿", "button":"按键", "length":"长度", "hex":"HEX", "details":"详情"}
        for c in cols: self.tree.heading(c, text=names[c]); self.tree.column(c, width=100 if c not in ("hex", "details") else (300 if c == "hex" else 460))
        self.tree.pack(fill=BOTH, expand=True, padx=8, pady=5)
        self.refresh_paths()

    def on_voice_status(self, text):
        self.root.after(0, lambda: self.voice_status.set(text))

    def refresh_audio_outputs(self):
        try:
            names = PCMOutput.list_devices()
            self.audio_combo["values"] = names
            preferred = next((n for n in names if n.strip().casefold().startswith("cable input")), names[0] if names else "")
            if preferred:
                self.audio_var.set(preferred); self.voice.output.device_name = preferred
            elif not names:
                self.audio_var.set("未找到音频输出设备")
        except Exception as exc:
            self.audio_combo["values"] = []
            self.audio_var.set("未安装 sounddevice / VB-CABLE")
            self.voice_status.set(f"BLE 语音：音频不可用（{exc}）")

    def refresh_input_devices(self):
        try:
            import sounddevice as sd
            names = [str(d["name"]) for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
            self.input_combo["values"] = names
            preferred = next((n for n in names if n.strip().casefold().startswith("cable output")), names[0] if names else "")
            if preferred: self.input_var.set(preferred)
            elif not names: self.input_var.set("未找到系统输入设备")
        except Exception as exc:
            self.input_combo["values"] = []
            self.input_var.set(f"输入设备不可用：{exc}")

    def start_all(self):
        """One-click setup for the normal remote + voice workflow."""
        self.refresh_paths()
        if len(self.path_values) == 1:
            self.path_combo.current(1)
        self.start()
        self.refresh_audio_outputs()
        self.connect_voice()

    def connect_voice(self):
        selected = self.audio_var.get()
        if selected and not selected.startswith("未") and not selected.startswith("请"):
            self.voice.output.device_name = selected
        def worker():
            try:
                self.voice.connect()
            except Exception as exc:
                self.on_voice_status(f"BLE 语音：连接失败（{exc}）")
        threading.Thread(target=worker, name="xiaomi-voice-connect", daemon=True).start()

    def load_mapping(self):
        try:
            saved = json.loads(self.mapping_file.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.mapping.update({button: action for button, action in saved.items() if button in REMOTE_BUTTONS and action in ACTION_LABELS})
        except (OSError, ValueError):
            pass

    def save_mapping(self):
        inverse = {label: action for action, label in ACTION_LABELS.items()}
        self.mapping = {button: inverse.get(var.get(), "none") for button, var in self.mapping_vars.items()}
        try:
            self.mapping_file.write_text(json.dumps(self.mapping, ensure_ascii=False, indent=2), encoding="utf-8")
            self.layer.set("Raw Input: 映射已保存")
        except OSError as exc:
            messagebox.showerror("映射", f"保存映射失败：{exc}")

    def refresh_paths(self):
        try:
            self.path_values = enumerate_device_paths()
            labels = [display_device_path(path, index + 1) for index, path in enumerate(self.path_values)]
            self.path_combo["values"] = ["自动选择（仅连接一个时）"] + labels
            self.path_combo.current(0)
            self.layer.set(f"Raw Input: 检测到 {len(self.path_values)} 个匹配设备")
        except Exception as exc:
            self.path_values = []
            if hasattr(self, "path_combo"):
                self.path_combo["values"] = ["自动选择（仅连接一个时）"]
                self.path_combo.current(0)
            self.layer.set(f"Raw Input: 设备扫描不可用（{exc}）")

    def selected_path(self) -> str | None:
        index = self.path_combo.current() if hasattr(self, "path_combo") else 0
        return self.path_values[index - 1] if index > 0 and index <= len(self.path_values) else None

    def emit_mapping(self, button: str, edge: str):
        action = self.mapping.get(button, "none")
        if action == "none":
            return
        if action == "ALT_TAB":
            if edge == "DOWN": _send_key(0x09, False, (0x12,))
            elif edge == "UP": _send_key(0x09, True, (0x12,))
            return
        vk = ACTION_VK.get(action)
        if vk is None:
            return
        if edge == "DOWN" and action not in self.output_held:
            _send_key(vk, False); self.output_held.add(action)
        elif edge == "UP" and action in self.output_held:
            _send_key(vk, True); self.output_held.discard(action)

    def voice_edge(self, edge: str):
        if edge == "DOWN":
            self.voice.press()
        elif edge == "UP":
            self.voice.release()

    def on_status(self, text): self.root.after(0, lambda: self.layer.set(f"Raw Input: {text}"))
    def on_raw(self, data, edge, button, details):
        event = self.recorder.add("RAW_INPUT", data, event_type=edge, details=details, report_id="")
        try:
            log_path = Path(__file__).with_name("xiaomi_remote2_live.log")
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"{event.timestamp}\t{edge}\t{button}\t{data.hex(' ').upper()}\t{details}\n")
        except OSError:
            pass
        if button in REMOTE_BUTTONS:
            if button == "voice" and self.mapping.get("voice") == "VOICE":
                self.voice_edge(edge)
            else:
                self.emit_mapping(button, edge)
        self.root.after(0, lambda: self.tree.insert("", END, values=(event.timestamp, event.source, edge, button, len(data), data.hex(" ").upper(), details)))
    def start(self):
        try: self.listener.start(self.selected_path()); self.listening = True; self.status.set("● 正在监听")
        except Exception as exc: messagebox.showerror("Raw Input", str(exc)); self.layer.set(f"Raw Input: ERROR {exc}")
    def stop(self):
        try: self.listener.stop()
        except Exception as exc: self.layer.set(f"Raw Input: STOP ERROR {exc}")
        for action in tuple(self.output_held):
            vk = ACTION_VK.get(action)
            if vk is not None: _send_key(vk, True)
        self.output_held.clear()
        self.listening = False; self.status.set("● 已停止")
    def clear(self):
        self.recorder.clear()
        for item in self.tree.get_children(): self.tree.delete(item)
    def save(self):
        directory = filedialog.askdirectory(title="选择导出目录")
        if not directory: return
        self.recorder.export_json(str(Path(directory) / "xiaomi_remote2_test.json"))
        self.recorder.export_raw(str(Path(directory) / "xiaomi_remote2_raw.log"))
        messagebox.showinfo("导出", "已保存 xiaomi_remote2_test.json 和 xiaomi_remote2_raw.log")
    def ble_inspect(self):
        def worker():
            try:
                from xiaomi_remote2_ble import inspect_sync
                result = inspect_sync()
                text = "\n".join(f"{item.name}: {item.status}\n" + "\n".join(f"  {s['uuid']}: {', '.join(s['characteristics'])}" for s in item.services) for item in result) or "未发现匹配的已配对 BLE 遥控器"
            except Exception as exc:
                text = f"BLE 诊断不可用：{exc}"
            self.root.after(0, lambda: messagebox.showinfo("BLE / ATVV 诊断", text))
        threading.Thread(target=worker, daemon=True).start()
    def close(self): self.stop(); self.voice.close(); self.root.destroy()


def main():
    # Phase-one product UI intentionally runs as a presentation prototype.
    # The existing listener/controller classes remain available for phase two.
    from xiaomi_remote2_ui import RemotePrototypeApp
    root = Tk(); RemotePrototypeApp(root); root.mainloop()


if __name__ == "__main__": main()
