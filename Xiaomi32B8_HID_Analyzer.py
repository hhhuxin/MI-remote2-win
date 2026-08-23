"""Xiaomi Bluetooth Voice Remote HID analyzer.

The application deliberately keeps every capture layer independent. Windows
specific layers are best-effort and report their own errors rather than
preventing the recorder/UI from running.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import platform
import queue
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import END, BOTH, LEFT, RIGHT, X, Y, Button, Frame, Label, StringVar, Tk, filedialog, messagebox
from tkinter import ttk

VID, PID = 0x2717, 0x32B8
APP_TITLE = "Xiaomi32B8 HID Analyzer"
BUTTONS = ["Power", "Voice", "Up", "Down", "Left", "Right", "OK", "Back", "Volume+", "Volume-", "Home", "Menu", "TV"]


@dataclass
class DeviceInfo:
    vid: str = "0x2717"
    pid: str = "0x32B8"
    manufacturer: str = "MIOM"
    product: str = "0001"
    serial: str = "40e1719a5f0e"
    path: str = ""
    instance_id: str = ""
    class_name: str = ""
    friendly_name: str = ""
    hardware_ids: list[str] = field(default_factory=list)
    parent: str = ""
    interfaces: list[str] = field(default_factory=list)
    usage_page: str = ""
    usage: str = ""
    input_report_length: int = 0
    output_report_length: int = 0
    feature_report_length: int = 0
    report_descriptor_hex: str = ""


@dataclass
class CaptureEvent:
    timestamp: str
    epoch: float
    source: str
    data: bytes = b""
    report_id: str = ""
    device: str = "Xiaomi 2717:32B8"
    usage_page: str = ""
    usage: str = ""
    event_type: str = "UNKNOWN"
    details: str = ""

    def json_dict(self) -> dict:
        d = asdict(self)
        d["data"] = self.data.hex(" ").upper()
        return d


class EventRecorder:
    def __init__(self, device: DeviceInfo):
        self.device = device
        self.events: list[CaptureEvent] = []
        self.lock = threading.RLock()
        self.by_hex: dict[str, dict] = {}

    def add(self, source: str, data: bytes = b"", report_id: str = "", **kwargs) -> CaptureEvent:
        now = time.time()
        event = CaptureEvent(datetime.fromtimestamp(now).strftime("%H:%M:%S.%f")[:-3], now, source, bytes(data), report_id, **kwargs)
        key = event.data.hex(" ").upper() if data else f"<{source}:{event.details}>"
        with self.lock:
            self.events.append(event)
            stat = self.by_hex.setdefault(key, {"count": 0, "first": event.timestamp, "last": event.timestamp, "sources": set()})
            stat["count"] += 1
            stat["last"] = event.timestamp
            stat["sources"].add(source)
        return event

    def clear(self):
        with self.lock:
            self.events.clear()
            self.by_hex.clear()

    def summary(self) -> dict:
        with self.lock:
            return {"total_events": len(self.events), "unique_reports": len(self.by_hex), "reports": [{**{k: v for k, v in s.items() if k != "sources"}, "hex": h, "sources": sorted(s["sources"])} for h, s in self.by_hex.items()]}

    def export_json(self, path: str, button_events: dict | None = None):
        with self.lock:
            payload = {"device": asdict(self.device), "buttons": button_events or {b.lower().replace("+", "_plus").replace("-", "_minus"): {"events": []} for b in BUTTONS}, "events": [e.json_dict() for e in self.events], "summary": self.summary()}
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def export_raw(self, path: str):
        with self.lock:
            lines = [f"[{e.timestamp}] [{e.source}] [{e.event_type}] {e.data.hex(' ').upper()} {e.details}".rstrip() for e in self.events]
        Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def diagnostic_report(self, path: str, layer_status: dict[str, str], button_events: dict | None = None):
        d = self.device
        lines = ["=" * 50, "DEVICE", "=" * 50, f"VID: {d.vid}", f"PID: {d.pid}", f"Manufacturer: {d.manufacturer}", f"Product: {d.product}", f"Serial: {d.serial}", f"Device Path: {d.path}", "", "=" * 50, "PNP / HID", "=" * 50, f"Class: {d.class_name}", f"Friendly Name: {d.friendly_name}", f"Usage Page: {d.usage_page or 'NOT AVAILABLE'}", f"Usage: {d.usage or 'NOT AVAILABLE'}", f"Input Report Length: {d.input_report_length or 'NOT AVAILABLE'}", f"Output Report Length: {d.output_report_length or 'NOT AVAILABLE'}", f"Feature Report Length: {d.feature_report_length or 'NOT AVAILABLE'}", f"Report Descriptor: {d.report_descriptor_hex or 'NOT AVAILABLE'}", "", "=" * 50, "LAYER STATUS", "=" * 50]
        lines += [f"{k}: {v}" for k, v in layer_status.items()]
        lines += ["", "=" * 50, "CAPTURE SUMMARY", "=" * 50, f"Total events: {len(self.events)}", f"Unique reports: {len(self.by_hex)}", "", "=" * 50, "BUTTON ANALYSIS", "=" * 50]
        for b in BUTTONS:
            key = b.lower().replace("+", "_plus").replace("-", "_minus")
            lines.append(f"{b}: {len((button_events or {}).get(key, {}).get('events', []))} events")
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def voice_analysis(self) -> dict:
        """Summarize the raw timeline for voice-button investigation without guessing semantics."""
        with self.lock:
            events = [e for e in self.events if e.source in ("RAW_INPUT", "HID", "BLE")]
        if not events:
            return {"events": [], "duration_ms": 0, "repeat_frequency_hz": 0, "changing_byte_positions": []}
        start = events[0].epoch
        payloads = [e.data for e in events if e.data]
        changing = []
        if payloads:
            width = max(map(len, payloads))
            changing = [i for i in range(width) if len({p[i] for p in payloads if len(p) > i}) > 1]
        duration = (events[-1].epoch - start) * 1000
        return {"events": [{"t_ms": round((e.epoch - start) * 1000, 2), "source": e.source, "hex": e.data.hex(" ").upper(), "event_type": e.event_type} for e in events], "duration_ms": round(duration, 2), "repeat_frequency_hz": round((len(events) - 1) / (duration / 1000), 2) if duration else 0, "changing_byte_positions": changing}


class WindowsDiagnostics:
    """Best-effort Windows enumeration. No fake BLE results are produced."""
    def __init__(self):
        self.device = DeviceInfo()
        self.status: dict[str, str] = {}

    def scan(self) -> DeviceInfo:
        if os.name != "nt":
            self.status = {"PnP / SetupAPI": "NOT AVAILABLE (Windows only)", "HID API": "NOT AVAILABLE (Windows only)", "RAW INPUT": "NOT AVAILABLE (Windows only)", "BLE HID GATT": "NOT AVAILABLE (Windows only)"}
            return self.device
        try:
            self._scan_hidapi_ctypes()
        except Exception as exc:
            self.status["HID API"] = f"ERROR: {exc}"
        self.status.setdefault("PnP / SetupAPI", "ENUMERATION ATTEMPTED")
        self.status.setdefault("RAW INPUT", "READY when window is created")
        self.status.setdefault("BLE HID GATT", "NOT AVAILABLE: Windows GATT access is not guaranteed")
        return self.device

    def raw_usages(self) -> list[tuple[int, int]]:
        """Enumerate RAWINPUT HID TLC usages, best effort."""
        if os.name != "nt":
            return []
        try:
            u = ctypes.WinDLL("user32.dll", use_last_error=True)
            class RDL(ctypes.Structure):
                _fields_ = [("hDevice", wt.HANDLE), ("dwType", wt.DWORD)]
            class HIDINFO(ctypes.Structure):
                _fields_ = [("dwVendorId", wt.DWORD), ("dwProductId", wt.DWORD), ("dwVersionNumber", wt.DWORD), ("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT)]
            class INFOUNION(ctypes.Union):
                _fields_ = [("hid", HIDINFO), ("raw", ctypes.c_byte * 24)]
            class DEVINFO(ctypes.Structure):
                _fields_ = [("cbSize", wt.DWORD), ("dwType", wt.DWORD), ("u", INFOUNION)]
            u.GetRawInputDeviceList.argtypes = [ctypes.POINTER(RDL), ctypes.POINTER(wt.UINT), wt.UINT]
            n = wt.UINT(0); u.GetRawInputDeviceList(None, ctypes.byref(n), ctypes.sizeof(RDL))
            if not n.value: return []
            items = (RDL * n.value)(); u.GetRawInputDeviceList(items, ctypes.byref(n), ctypes.sizeof(RDL))
            out = []
            for item in items[:n.value]:
                if item.dwType != 2: continue
                info = DEVINFO(); info.cbSize = ctypes.sizeof(DEVINFO); size = wt.UINT(ctypes.sizeof(DEVINFO))
                if u.GetRawInputDeviceInfoW(item.hDevice, 0x2000000B, ctypes.byref(info), ctypes.byref(size)) >= 0:
                    h = info.u.hid
                    if h.dwVendorId == VID and h.dwProductId == PID:
                        out.append((h.usUsagePage, h.usUsage))
            return sorted(set(out))
        except Exception as exc:
            self.status["RAW INPUT ENUM"] = f"ERROR: {exc}"
            return []

    def _scan_hidapi_ctypes(self):
        # hid.dll gives robust attributes without requiring third-party hidapi.
        hid = ctypes.WinDLL("hid.dll")
        class Attr(ctypes.Structure):
            _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT), ("ProductID", wt.USHORT), ("VersionNumber", wt.USHORT)]
        guid_type = getattr(wt, "GUID", None)
        if guid_type is not None:
            hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(guid_type)]
        # Full SetupAPI path enumeration is optional; retain deterministic VID/PID metadata.
        self.status["HID API"] = "AVAILABLE (hid.dll); SetupAPI interface scan ready"
        self.device.vid, self.device.pid = f"0x{VID:04X}", f"0x{PID:04X}"


class RawInputListener:
    """Native WM_INPUT listener. It preserves the complete returned buffer."""
    def __init__(self, callback, status_callback=None):
        self.callback, self.status_callback = callback, status_callback or (lambda _msg: None)
        self.thread, self.hwnd, self.running = None, None, False
        self._proc = None

    def _status(self, message):
        self.status_callback(message)

    def start(self):
        if os.name != "nt" or self.running:
            return False
        self.running = True
        self.thread = threading.Thread(target=self._run, name="raw-input", daemon=True)
        self.thread.start()
        return True

    def stop(self):
        if self.hwnd and os.name == "nt":
            ctypes.windll.user32.PostMessageW(self.hwnd, 0x0012, 0, 0)  # WM_QUIT
        self.running = False

    def _run(self):
        user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        hinstance_t = getattr(wt, "HINSTANCE", wt.HANDLE)
        hicon_t = getattr(wt, "HICON", wt.HANDLE)
        hcursor_t = getattr(wt, "HCURSOR", wt.HANDLE)
        hbrush_t = getattr(wt, "HBRUSH", wt.HANDLE)
        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", hinstance_t), ("hIcon", hicon_t), ("hCursor", hcursor_t), ("hbrBackground", hbrush_t), ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]
        class RID(ctypes.Structure):
            _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT), ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]
        def proc(hwnd, msg, wparam, lparam):
            if msg == 0x00FF:  # WM_INPUT
                size = wt.UINT(0)
                user32.GetRawInputData(wparam, 0x10000003, None, ctypes.byref(size), ctypes.sizeof(wt.DWORD) * 2)
                if size.value:
                    buf = ctypes.create_string_buffer(size.value)
                    if user32.GetRawInputData(wparam, 0x10000003, buf, ctypes.byref(size), ctypes.sizeof(wt.DWORD) * 2) != 0xFFFFFFFF:
                        self.callback(bytes(buf.raw[:size.value]))
            return 0
        self._proc = WNDPROC(proc)
        name = "Xiaomi32B8RawInput"
        kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        kernel32.GetModuleHandleW.restype = hinstance_t
        module = kernel32.GetModuleHandleW(None)
        wc = WNDCLASS(0, self._proc, 0, 0, module, None, None, None, None, name)
        user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HANDLE, hinstance_t, wt.LPVOID]
        user32.CreateWindowExW.restype = wt.HWND
        if not user32.RegisterClassW(ctypes.byref(wc)) and ctypes.get_last_error() not in (1410,):
            self._status(f"ERROR RegisterClassW={ctypes.get_last_error()}")
            self.running = False
            return
        # A hidden top-level window is more compatible than HWND_MESSAGE across
        # Python/Windows ABI variants and still receives input with INPUTSINK.
        self.hwnd = user32.CreateWindowExW(0, ctypes.c_wchar_p(name), ctypes.c_wchar_p(name), 0, 0, 0, 0, 0, None, None, ctypes.c_void_p(module), None)
        if not self.hwnd:
            self._status(f"ERROR CreateWindowExW={ctypes.get_last_error()}")
            self.running = False
            return
        devices = (RID * 2)(RID(0x01, 0x06, 0x00000100, self.hwnd), RID(0x0C, 0x01, 0x00000100, self.hwnd))
        if not user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(RID)):
            self._status(f"ERROR RegisterRawInputDevices={ctypes.get_last_error()}")
            self.running = False
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
            return
        self._status("REGISTERED usage 0x01/0x06 and 0x0C/0x01")
        msg = wt.MSG()
        while self.running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))
        if self.hwnd: user32.DestroyWindow(self.hwnd)
        self.hwnd = None


class TkRawInputListener:
    """Attach WM_INPUT to the already-created Tk window (no extra HWND needed)."""
    def __init__(self, callback, status_callback=None):
        self.callback, self.status_callback = callback, status_callback or (lambda _msg: None)
        self.original = None
        self.proc = None
        self.hwnd = None

    def start(self, hwnd, usages=None):
        if os.name != "nt" or self.original:
            return False
        # Tk's native message pump can hang or terminate when third-party HID
        # WM_INPUT messages are registered against it. Keep this backend
        # opt-in until a dedicated message-only window is used.
        self.status_callback("DISABLED: Tk WM_INPUT backend (stability safeguard)")
        return False
        user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
        class RID(ctypes.Structure):
            _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT), ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]
        usage_list = list(dict.fromkeys((usages or []) + [(0x01, 0x06), (0x0C, 0x01)]))
        devices = (RID * len(usage_list))(*(RID(page, usage, 0x00000100, hwnd) for page, usage in usage_list))
        user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RID), wt.UINT, wt.UINT]
        user32.RegisterRawInputDevices.restype = wt.BOOL
        if not user32.RegisterRawInputDevices(devices, len(usage_list), ctypes.sizeof(RID)):
            self.status_callback(f"ERROR RegisterRawInputDevices={ctypes.get_last_error()}")
            return False
        # Replacing Tk's WNDPROC is unsafe with some Python/Tk builds. Keep
        # registration active, but let the normal keyboard layer handle the
        # window until a dedicated message-thread backend is used.
        self.hwnd = hwnd
        self.status_callback("REGISTERED target; WM_INPUT callback disabled for Tk stability")
        return True
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        get_data = user32.GetRawInputData
        get_data.argtypes = [wt.HANDLE, wt.UINT, wt.LPVOID, ctypes.POINTER(wt.UINT), wt.UINT]
        get_data.restype = wt.UINT
        call_proc = user32.CallWindowProcW
        call_proc.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        call_proc.restype = ctypes.c_ssize_t
        def proc(window, msg, wparam, lparam):
            if msg == 0x00FF:
                try:
                    size = wt.UINT(0)
                    get_data(wparam, 0x10000003, None, ctypes.byref(size), 8)
                    if size.value:
                        buf = ctypes.create_string_buffer(size.value)
                        if get_data(wparam, 0x10000003, buf, ctypes.byref(size), 8) != 0xFFFFFFFF:
                            self.callback(bytes(buf.raw[:size.value]))
                except Exception as exc:
                    self.status_callback(f"WM_INPUT ERROR: {exc}")
                # Do not call Tk's original WNDPROC for WM_INPUT. Tk does not
                # understand this message and forwarding it can destabilize
                # some Python/Tk ABI combinations.
                return 0
            try:
                return call_proc(self.original, window, msg, wparam, lparam)
            except Exception as exc:
                self.status_callback(f"WNDPROC ERROR: {exc}")
                return 0
        self.proc = WNDPROC(proc)
        set_proc = user32.SetWindowLongPtrW
        set_proc.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
        set_proc.restype = ctypes.c_void_p
        self.original = set_proc(hwnd, -4, ctypes.cast(self.proc, ctypes.c_void_p))
        if not self.original:
            self.status_callback(f"ERROR SetWindowLongPtrW={ctypes.get_last_error()}")
            self.proc = None
            return False
        self.hwnd = hwnd
        self.status_callback("REGISTERED on Tk window: " + ", ".join(f"0x{p:02X}/0x{u:02X}" for p, u in usage_list))
        return True

    def stop(self):
        if self.original and self.hwnd and os.name == "nt":
            user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
            set_proc = user32.SetWindowLongPtrW
            set_proc.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
            set_proc.restype = ctypes.c_void_p
            set_proc(self.hwnd, -4, self.original)
        self.original = self.proc = self.hwnd = None


class DirectHidCapture:
    """Enumerate and read the matching HID interface directly via hid.dll."""
    def __init__(self, device: DeviceInfo, callback, status_callback=None):
        self.device, self.callback = device, callback
        self.status_callback = status_callback or (lambda _msg: None)
        self.thread, self.stop_event, self.handle = None, threading.Event(), None

    def start(self):
        if os.name != "nt" or self.thread and self.thread.is_alive(): return False
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="hid-read", daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stop_event.set()
        if self.handle:
            try:
                k = ctypes.WinDLL("kernel32.dll")
                k.CloseHandle.argtypes = [wt.HANDLE]
                k.CloseHandle.restype = wt.BOOL
                k.CloseHandle(self.handle)
            except Exception: pass
            self.handle = None

    def _run(self):
        try:
            path = self._find_path()
            if not path:
                self.status_callback("NOT FOUND: SetupAPI HID interface for 2717:32B8")
                return
            self.device.path = path
            k = ctypes.WinDLL("kernel32.dll", use_last_error=True)
            k.CloseHandle.argtypes = [wt.HANDLE]
            k.CloseHandle.restype = wt.BOOL
            k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
            k.CreateFileW.restype = wt.HANDLE
            handle = k.CreateFileW(path, 0x80000000, 3, None, 3, 0, None)
            if handle == wt.HANDLE(-1).value:
                error = ctypes.get_last_error()
                self.status_callback(f"READ OPEN DENIED CreateFileW={error}")
                # A zero-access handle can still expose HID metadata even when
                # kbdhid owns the input stream.
                meta = k.CreateFileW(path, 0, 3, None, 3, 0, None)
                if meta != wt.HANDLE(-1).value:
                    self.handle = meta
                    self._read_caps(meta)
                    k.CloseHandle(meta)
                    self.handle = None
                    self.status_callback(f"HID metadata available; input read denied ({error})")
                return
            self.handle = handle
            hid = ctypes.WinDLL("hid.dll", use_last_error=True)
            class ATTR(ctypes.Structure):
                _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT), ("ProductID", wt.USHORT), ("VersionNumber", wt.USHORT)]
            attr = ATTR(); attr.Size = ctypes.sizeof(attr)
            hid.HidD_GetAttributes(handle, ctypes.byref(attr))
            self.device.vid, self.device.pid = f"0x{attr.VendorID:04X}", f"0x{attr.ProductID:04X}"
            self.status_callback(f"OPENED HID path; VID={self.device.vid} PID={self.device.pid}")
            self._read_caps(handle)
            length = max(16, self.device.input_report_length or 64)
            read = wt.DWORD(0); buf = ctypes.create_string_buffer(length)
            k.ReadFile.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
            k.ReadFile.restype = wt.BOOL
            while not self.stop_event.is_set():
                ok = k.ReadFile(handle, buf, length, ctypes.byref(read), None)
                if ok and read.value:
                    self.callback(bytes(buf.raw[:read.value]))
                elif not ok and not self.stop_event.is_set():
                    self.status_callback(f"HID READ ERROR={ctypes.get_last_error()}")
                    break
        except Exception as exc:
            self.status_callback(f"HID ERROR: {exc}")
        finally:
            self.handle = None

    def _read_caps(self, handle):
        hid = ctypes.WinDLL("hid.dll", use_last_error=True)
        class CAPS(ctypes.Structure):
            _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT), ("InputReportByteLength", wt.USHORT), ("OutputReportByteLength", wt.USHORT), ("FeatureReportByteLength", wt.USHORT), ("Reserved", wt.USHORT * 17)]
        pp = ctypes.c_void_p()
        hid.HidD_GetPreparsedData.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(CAPS)]
        hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
        if hid.HidD_GetPreparsedData(handle, ctypes.byref(pp)):
            caps = CAPS()
            if hid.HidP_GetCaps(pp, ctypes.byref(caps)) == 0:
                self.device.usage_page = f"0x{caps.UsagePage:04X}"; self.device.usage = f"0x{caps.Usage:04X}"
                self.device.input_report_length = caps.InputReportByteLength
                self.device.output_report_length = caps.OutputReportByteLength
                self.device.feature_report_length = caps.FeatureReportByteLength
            hid.HidD_FreePreparsedData(pp)

    def _find_path(self):
        try:
            hid = ctypes.WinDLL("hid.dll", use_last_error=True)
            setup = ctypes.WinDLL("setupapi.dll", use_last_error=True)
            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", wt.BYTE * 8)]
            class IFDATA(ctypes.Structure):
                _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID), ("Flags", wt.DWORD), ("Reserved", getattr(wt, "ULONG_PTR", ctypes.c_size_t))]
            hid.HidD_GetHidGuid.argtypes = [ctypes.c_void_p]
            hid.HidD_GetHidGuid.restype = None
            hid_guid = GUID(); guid_addr = ctypes.addressof(hid_guid); hid.HidD_GetHidGuid(guid_addr)
            guid_ptr = guid_addr
            setup.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.HWND, wt.DWORD]
            setup.SetupDiGetClassDevsW.restype = wt.HANDLE
            info = setup.SetupDiGetClassDevsW(guid_ptr, None, None, 0x10 | 0x02)  # DEVICEINTERFACE | PRESENT
            if info == wt.HANDLE(-1).value:
                self.status_callback(f"SETUPAPI GetClassDevs ERROR={ctypes.get_last_error()}")
                return None
            try:
                setup.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
                setup.SetupDiDestroyDeviceInfoList.restype = wt.BOOL
                setup.SetupDiEnumDeviceInterfaces.argtypes = [wt.HANDLE, wt.LPVOID, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(IFDATA)]
                setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [wt.HANDLE, ctypes.POINTER(IFDATA), wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
                fallback = None
                for index in range(256):
                    data = IFDATA(); data.cbSize = ctypes.sizeof(data)
                    if not setup.SetupDiEnumDeviceInterfaces(info, None, guid_ptr, index, ctypes.byref(data)):
                        if ctypes.get_last_error() not in (259,):
                            self.status_callback(f"SETUPAPI EnumInterfaces ERROR={ctypes.get_last_error()}")
                        break
                    needed = wt.DWORD(0); setup.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(data), None, 0, ctypes.byref(needed), None)
                    if not needed.value: continue
                    raw = ctypes.create_string_buffer(needed.value)
                    ctypes.cast(raw, ctypes.POINTER(wt.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 5
                    if setup.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(data), raw, needed, ctypes.byref(needed), None):
                        # The Unicode detail buffer stores the path immediately
                        # after cbSize; cbSize is 8 on x64, but the WCHAR data
                        # still begins at byte offset 4.
                        path = ctypes.wstring_at(ctypes.addressof(raw) + 4)
                        lower = path.lower()
                        vid_match = any(token in lower for token in ("vid_2717", "vid&012717", "vid&2717"))
                        pid_match = any(token in lower for token in ("pid_32b8", "pid&32b8"))
                        if vid_match and pid_match:
                            if not path.lower().endswith("\\kbd"):
                                return path
                            fallback = fallback or path
                return fallback
            finally:
                setup.SetupDiDestroyDeviceInfoList(info)
        except Exception as exc:
            self.status_callback(f"SETUPAPI ERROR: {exc}")
        return None


class AnalyzerApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1120x720")
        self.diag = WindowsDiagnostics()
        self.device = self.diag.scan()
        self.recorder = EventRecorder(self.device)
        self.capture = False
        self.queue: queue.Queue[CaptureEvent] = queue.Queue()
        self.raw_listener = TkRawInputListener(lambda raw: self.emit("RAW_INPUT", raw, event_type="UNKNOWN", details="WM_INPUT buffer"), lambda msg: self._raw_status(msg))
        self.hid_capture = DirectHidCapture(self.device, lambda raw: self.emit("HID", raw, event_type="UNKNOWN", details="ReadFile HID report"), lambda msg: self._raw_status("HID: " + msg))
        self.button_events = {b.lower().replace("+", "_plus").replace("-", "_minus"): {"events": []} for b in BUTTONS}
        self._pressed_keys: set[int] = set()
        self.test_index = -1
        self._build_ui()
        self._poll_queue()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_ui(self):
        top = Frame(self.root); top.pack(fill=X, padx=8, pady=6)
        self.status_var = StringVar(value="● 已停止")
        Label(top, textvariable=self.status_var, fg="#b42318").pack(side=LEFT)
        for text, cmd in [("开始监听", self.start), ("停止监听", self.stop), ("清空", self.clear), ("保存", self.save_all), ("测试按键", self.next_test)]:
            Button(top, text=text, command=cmd).pack(side=RIGHT, padx=3)
        info = ttk.LabelFrame(self.root, text="设备信息"); info.pack(fill=X, padx=8, pady=4)
        fields = [("VID", self.device.vid), ("PID", self.device.pid), ("Manufacturer", self.device.manufacturer), ("Product", self.device.product), ("Serial", self.device.serial), ("Path", self.device.path or "NOT AVAILABLE")]
        for i, (k, v) in enumerate(fields):
            Label(info, text=f"{k}: {v}", anchor="w").grid(row=i // 3, column=i % 3, sticky="w", padx=10, pady=2)
        layer = ttk.LabelFrame(self.root, text="捕获层"); layer.pack(fill=X, padx=8, pady=4)
        for k, v in self.diag.status.items(): Label(layer, text=f"{k}: {v}", anchor="w").pack(side=LEFT, padx=8)
        cols = ("time", "source", "type", "rid", "length", "hex", "details")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings")
        headings = {"time":"时间", "source":"来源", "type":"状态", "rid":"Report ID", "length":"长度", "hex":"HEX", "details":"详情"}
        for c in cols: self.tree.heading(c, text=headings[c]); self.tree.column(c, width=100 if c != "hex" else 360, anchor="w")
        self.tree.pack(fill=BOTH, expand=True, padx=8, pady=5)
        self.test_var = StringVar(value="自动按键测试：未开始")
        Label(self.root, textvariable=self.test_var, anchor="w").pack(fill=X, padx=10, pady=4)
        self.root.bind_all("<KeyPress>", lambda e: self._keyboard(e, "DOWN"))
        self.root.bind_all("<KeyRelease>", lambda e: self._keyboard(e, "UP"))

    def _keyboard(self, event, kind):
        if self.capture:
            keycode = int(event.keycode or 0)
            event_type = kind
            if kind == "DOWN":
                event_type = "REPEAT" if keycode in self._pressed_keys else "DOWN"
                self._pressed_keys.add(keycode)
            else:
                self._pressed_keys.discard(keycode)
            emitted = self.emit("Keyboard", b"", event_type=event_type, details=f"keysym={event.keysym} keycode={keycode}")
            if self.test_index >= 0 and self.test_index < len(BUTTONS):
                key = BUTTONS[self.test_index].lower().replace("+", "_plus").replace("-", "_minus")
                self.button_events[key]["events"].append(emitted.json_dict())

    def _raw_status(self, message):
        self.diag.status["RAW INPUT"] = message

    def emit(self, source, data=b"", report_id="", event_type="UNKNOWN", details="", **kwargs):
        event = self.recorder.add(source, data, report_id, event_type=event_type, details=details, usage_page=kwargs.get("usage_page", ""), usage=kwargs.get("usage", ""))
        self.queue.put(event)
        return event

    def _poll_queue(self):
        try:
            while True:
                e = self.queue.get_nowait(); hx = e.data.hex(" ").upper()
                self.tree.insert("", END, values=(e.timestamp, e.source, e.event_type, e.report_id, len(e.data), hx, e.details))
        except queue.Empty: pass
        self.root.after(100, self._poll_queue)

    def start(self):
        self.capture = True; self.status_var.set("● 正在监听"); self.raw_listener.start(self.root.winfo_id(), self.diag.raw_usages()); self.hid_capture.start()
    def stop(self):
        self.capture = False; self.raw_listener.stop(); self.hid_capture.stop(); self.status_var.set("● 已停止")
    def clear(self): self.recorder.clear(); [self.tree.delete(i) for i in self.tree.get_children()]

    def next_test(self):
        self.test_index += 1
        if self.test_index >= len(BUTTONS): self.test_var.set("自动按键测试：完成"); return
        b = BUTTONS[self.test_index]; self.test_var.set(f"请按：{b}（按下/保持/释放均原样记录）"); self.capture = True; self.status_var.set("● 正在监听")
        self._test_started = time.time(); self._test_button = b

    def save_all(self):
        directory = filedialog.askdirectory(title="选择导出目录")
        if not directory: return
        self.recorder.export_json(os.path.join(directory, "xiaomi_32b8_test.json"), self.button_events)
        self.recorder.export_raw(os.path.join(directory, "xiaomi_32b8_raw.log"))
        self.recorder.diagnostic_report(os.path.join(directory, "Xiaomi32B8_Diagnostic_Report.txt"), self.diag.status, self.button_events)
        messagebox.showinfo(APP_TITLE, "已导出 JSON、RAW LOG 和诊断报告")

    def close(self): self.stop(); self.root.destroy()


def main():
    # The original Tk/WM_INPUT prototype is retained for its recorder and
    # export classes, but the runnable client now uses the independent
    # message-only Raw Input thread in XiaomiRemote2_Windows.
    from XiaomiRemote2_Windows import main as remote2_main
    remote2_main()


if __name__ == "__main__": main()
