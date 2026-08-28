"""First-stage Windows 11 UI prototype for Xiaomi Remote 2 PC.

This module owns the first usable mapping workflow. BLE remains outside this
phase, while Raw Input and scoped keyboard output are enabled only after the
user explicitly starts listening.
"""
from __future__ import annotations

import json
import threading
import queue
from pathlib import Path
import ctypes
from ctypes import wintypes
import re
import sys
import time
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, Canvas, Entry, Frame, Label, StringVar, Tk, Button, PhotoImage, messagebox
from tkinter import ttk
from xiaomi_remote2_ble import ATVVVoiceController, PCMOutput, list_paired_remotes_sync

from XiaomiRemote2_Windows import BUTTON_LABELS, REMOTE_BUTTONS, RawInputListener, enumerate_device_paths, display_device_path, _send_key


BG = "#f5f5f7"
SIDEBAR = "#ededf0"
CARD = "#ffffff"
TEXT = "#1d1d1f"
MUTED = "#707078"
BLUE = "#007aff"
LINE = "#dedee3"
GREEN = "#34c759"
FONT = "Microsoft YaHei UI"
POWER_LONG_PRESS_MS = 800
F5_LONG_PRESS_SECONDS = 0.5

VK_NAMES = {
    "ctrl": 0x11, "control": 0x11, "左ctrl": 0xA2, "右ctrl": 0xA3,
    "win": 0x5B, "windows": 0x5B, "左win": 0x5B, "右win": 0x5C,
    "shift": 0x10, "左shift": 0xA0, "右shift": 0xA1,
    "alt": 0x12, "左alt": 0xA4, "右alt": 0xA5,
    "enter": 0x0D, "return": 0x0D, "确定": 0x0D,
    "backspace": 0x08, "退格": 0x08, "返回": 0x08,
    "escape": 0x1B, "esc": 0x1B, "空格": 0x20, "space": 0x20,
    "tab": 0x09, "home": 0x24, "主页": 0x24, "end": 0x23,
    "up": 0x26, "上": 0x26, "down": 0x28, "下": 0x28,
    "left": 0x25, "左": 0x25, "right": 0x27, "右": 0x27,
    "delete": 0x2E, "insert": 0x2D, "pageup": 0x21, "pagedown": 0x22,
    "volume_up": 0xAF, "volume+": 0xAF, "音量+": 0xAF,
    "volume_down": 0xAE, "volume-": 0xAE, "音量-": 0xAE,
    "mute": 0xAD, "静音": 0xAD, "play_pause": 0xB3,
}
VK_NAMES.update({f"f{i}": 0x6F + i for i in range(1, 25)})
VK_NAMES.update({str(i): 0x30 + i for i in range(10)})
VK_NAMES.update({chr(ord("a") + i): 0x41 + i for i in range(26)})


def parse_key_combo(text: str) -> tuple[tuple[int, ...], int | None]:
    """Parse a display string such as ``Ctrl + Win`` or ``Ctrl + Shift + S``."""
    tokens = [token.strip().casefold() for token in re.split(r"\s*\+\s*|[,，]", text or "") if token.strip()]
    if not tokens:
        raise ValueError("映射不能为空")
    codes = []
    for token in tokens:
        key = VK_NAMES.get(token) or VK_NAMES.get(token.replace(" ", ""))
        if key is None and token.startswith("vk_"):
            try: key = int(token[3:], 16)
            except ValueError: key = None
        if key is None:
            raise ValueError(f"无法识别按键：{token}")
        codes.append(key)
    modifiers = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
    modifier_codes = tuple(code for code in codes if code in modifiers)
    mains = [code for code in codes if code not in modifiers]
    if len(mains) > 1:
        raise ValueError("一个组合键只能有一个主键")
    return modifier_codes, mains[0] if mains else None


def send_combo_down(modifiers: tuple[int, ...], main: int | None):
    if main is None:
        for code in modifiers: _send_key(code, False)
    else:
        _send_key(main, False, modifiers)


def send_combo_up(modifiers: tuple[int, ...], main: int | None):
    if main is None:
        for code in reversed(modifiers): _send_key(code, True)
    else:
        _send_key(main, True, modifiers)


def send_combo_tap(modifiers: tuple[int, ...], main: int | None):
    send_combo_down(modifiers, main)
    send_combo_up(modifiers, main)


def clear_all_text():
    send_combo_tap((0x11,), 0x41)  # Ctrl+A
    send_combo_tap((), 0x08)      # Backspace


class F5Suppressor:
    """Global F5 handler matching the AHK short/long-press behavior."""
    def __init__(self, root, status_callback=None):
        self.root = root
        self.status_callback = status_callback or (lambda _text: None)
        self.thread = None
        self.stop_event = threading.Event()
        self.thread_id = None
        self._hook = None
        self._down_at = None

    def start(self):
        if sys.platform != "win32" or (self.thread and self.thread.is_alive()):
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="f5-suppressor", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread_id and sys.platform == "win32":
            try: ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
            except Exception: pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self.thread = None

    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self.thread_id = kernel32.GetCurrentThreadId()
        WH_KEYBOARD_LL, WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 13, 0x0100, 0x0101, 0x0104, 0x0105
        LLKHF_INJECTED = 0x10
        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]
        HookProc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        def callback(n_code, w_param, l_param):
            if n_code >= 0:
                event = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if event.vkCode == 0x74 and not (event.flags & LLKHF_INJECTED):
                    if self._cloud_active(user32):
                        return user32.CallNextHookEx(None, n_code, w_param, l_param)
                    if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN) and self._down_at is None:
                        self._down_at = time.monotonic()
                        return 1
                    if w_param in (WM_KEYUP, WM_SYSKEYUP) and self._down_at is not None:
                        duration = time.monotonic() - self._down_at
                        self._down_at = None
                        if duration < F5_LONG_PRESS_SECONDS:
                            self.root.after(0, lambda: send_combo_tap((0xA2, 0x5B, 0xA0), None))
                        return 1
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        self._hook = HookProc(callback)
        module = kernel32.GetModuleHandleW(None)
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HookProc, wintypes.HINSTANCE, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook, module, 0)
        if not handle:
            self.status_callback(f"F5 监听失败：{ctypes.get_last_error()}")
            return
        self.status_callback("F5 短按/长按接管已启动")
        msg = wintypes.MSG()
        while not self.stop_event.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(handle)
        self.thread_id = None

    @staticmethod
    def _cloud_active(user32):
        hwnd = user32.GetForegroundWindow()
        if not hwnd: return False
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value.casefold() == "redc_wclass_33"


def _rounded_rect(canvas: Canvas, x1, y1, x2, y2, radius=14, **kwargs):
    points = [x1 + radius, y1, x2 - radius, y1, x2, y1 + radius, x2, y2 - radius, x2 - radius, y2, x1 + radius, y2, x1, y2 - radius, x1, y1 + radius]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class RemotePrototypeApp:
    def __init__(self, root: Tk):
        self.root = root
        root.title("小米遥控器 2 PC")
        root.geometry("1120x760")
        root.minsize(980, 680)
        root.configure(bg=BG)
        self.page = "remote"
        self.current_button = StringVar(value="尚未选择按键")
        self.status = StringVar(value="已连接")
        self.listener_status = StringVar(value="监听未启动")
        self.device_var = StringVar(value="自动选择设备")
        self.mapping_file = Path(__file__).with_name("xiaomi_remote2_ui_mapping.json")
        self.bindings = self._load_bindings()
        self.mapping_vars: dict[str, StringVar] = {}
        self.device_paths: list[str] = []
        self.held_combos: dict[str, tuple[tuple[int, ...], int | None]] = {}
        self.power_long_press_job: str | None = None
        self.power_long_press_triggered = False
        self.listener = RawInputListener(self._on_remote_raw, self._on_listener_status)
        self.voice = ATVVVoiceController(self._on_voice_status)
        self.voice_status = StringVar(value="语音：未连接")
        self.audio_var = StringVar(value="请选择音频输出设备")
        self.ble_device_var = StringVar(value="正在扫描 BLE 遥控器")
        self.ble_devices = []
        self.listening = False
        self.nav_buttons: dict[str, Button] = {}
        self.content: Frame | None = None
        self.f5_status_queue = queue.Queue()
        self.f5_suppressor = F5Suppressor(root, self.f5_status_queue.put)
        self.f5_suppressor.start()
        self.root.after(100, self._poll_f5_status)
        self._setup_style()
        self._build_shell()
        self.show_page("remote")
        root.protocol("WM_DELETE_WINDOW", self.close)

    def _setup_style(self):
        style = ttk.Style(self.root)
        try: style.theme_use("clam")
        except Exception: pass
        style.configure("Prototype.TScrollbar", troughcolor=BG, background="#c7c7cc", bordercolor=BG, arrowcolor=MUTED)

    def _poll_f5_status(self):
        try:
            while True:
                self.listener_status.set(self.f5_status_queue.get_nowait())
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_f5_status)

    def _load_bindings(self):
        originals = {button: BUTTON_LABELS[button] for button in REMOTE_BUTTONS}
        try:
            saved = json.loads(self.mapping_file.read_text(encoding="utf-8"))
            for button in REMOTE_BUTTONS:
                value = saved.get(button, {}) if isinstance(saved, dict) else {}
                if isinstance(value, dict) and isinstance(value.get("mapping"), str):
                    originals[button] = value["mapping"]
        except (OSError, ValueError):
            pass
        return {button: {"original": BUTTON_LABELS[button], "mapping": originals[button]} for button in REMOTE_BUTTONS}

    def _build_shell(self):
        self.sidebar = Frame(self.root, bg=SIDEBAR, width=228)
        self.sidebar.pack(side=LEFT, fill=Y)
        self.sidebar.pack_propagate(False)
        Label(self.sidebar, text="小米遥控器 2 PC", bg=SIDEBAR, fg=TEXT, font=(FONT, 16, "bold"), anchor="w").pack(fill=X, padx=22, pady=(28, 4))
        Label(self.sidebar, text="Windows 11 控制中心", bg=SIDEBAR, fg=MUTED, font=(FONT, 9), anchor="w").pack(fill=X, padx=23, pady=(0, 28))
        for key, label in (("remote", "◉  遥控器"), ("mapping", "⌨  按键映射"), ("settings", "⚙  设置")):
            button = Button(self.sidebar, text="  " + label, command=lambda k=key: self.show_page(k), relief="flat", bd=0, anchor="w", bg=SIDEBAR, activebackground="#dcdce1", activeforeground=TEXT, fg=MUTED, font=(FONT, 11), padx=12, pady=10, cursor="hand2")
            button.pack(fill=X, padx=12, pady=2)
            self.nav_buttons[key] = button
        status = Frame(self.sidebar, bg=SIDEBAR)
        status.pack(side="bottom", fill=X, padx=20, pady=24)
        Label(status, text="●", fg=GREEN, bg=SIDEBAR, font=(FONT, 13)).pack(side=LEFT)
        Label(status, textvariable=self.status, fg=TEXT, bg=SIDEBAR, font=(FONT, 10)).pack(side=LEFT, padx=7)
        Label(status, text="小米遥控器 2 PC", fg=MUTED, bg=SIDEBAR, font=(FONT, 9)).pack(side=LEFT, padx=8)
        self.main = Frame(self.root, bg=BG)
        self.main.pack(side=LEFT, fill=BOTH, expand=True)

    def _header(self, title, subtitle):
        header = Frame(self.content, bg=BG)
        header.pack(fill=X, padx=42, pady=(34, 22))
        Label(header, text=title, bg=BG, fg=TEXT, font=(FONT, 25, "bold"), anchor="w").pack(fill=X)
        Label(header, text=subtitle, bg=BG, fg=MUTED, font=(FONT, 10), anchor="w").pack(fill=X, pady=(8, 0))

    def _card(self, parent, **kwargs):
        frame = Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1, bd=0, **kwargs)
        return frame

    def show_page(self, page):
        self.page = page
        if self.content is not None: self.content.destroy()
        self.content = Frame(self.main, bg=BG)
        self.content.pack(fill=BOTH, expand=True)
        for key, button in self.nav_buttons.items():
            button.configure(bg="#dcdce1" if key == page else SIDEBAR, fg=TEXT if key == page else MUTED)
        if page == "remote": self._remote_page()
        elif page == "mapping": self._mapping_page()
        else: self._settings_page()

    def _remote_page(self):
        self._header("遥控器", "小米遥控器 2 PC · 已连接")
        body = Frame(self.content, bg=BG)
        body.pack(fill=BOTH, expand=True, padx=42, pady=(0, 32))
        visual = self._card(body); visual.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 18))
        Label(visual, text="设备预览", bg=CARD, fg=MUTED, font=(FONT, 10), anchor="w").pack(fill=X, padx=24, pady=(22, 0))
        photo_path = Path(__file__).with_name("assets") / "remote_photo.png"
        if photo_path.exists():
            self.remote_photo = PhotoImage(file=str(photo_path))
            Label(visual, image=self.remote_photo, bg=CARD).pack(fill=BOTH, expand=True, padx=12, pady=(8, 2))
        else:
            canvas = Canvas(visual, bg=CARD, highlightthickness=0, width=470, height=520)
            canvas.pack(fill=BOTH, expand=True, padx=12, pady=10)
            self._draw_remote(canvas)
        shortcut_bar = Frame(visual, bg=CARD); shortcut_bar.pack(fill=X, padx=18, pady=(2, 18))
        for button in ("power", "voice", "up", "ok", "down", "back", "home", "menu", "tv", "volume_plus", "volume_minus"):
            Button(shortcut_bar, text=BUTTON_LABELS[button], command=lambda b=button: self._select_button(b), bg="#f1f1f4", activebackground="#e3e3e8", fg=TEXT, relief="flat", bd=0, padx=6, pady=4, font=(FONT, 8), cursor="hand2").pack(side=LEFT, padx=2)
        details = self._card(body, width=270); details.pack(side=RIGHT, fill=Y)
        details.pack_propagate(False)
        Label(details, text="当前按键", bg=CARD, fg=MUTED, font=(FONT, 10), anchor="w").pack(fill=X, padx=24, pady=(28, 6))
        Label(details, textvariable=self.current_button, bg=CARD, fg=TEXT, font=(FONT, 18, "bold"), anchor="w", wraplength=210).pack(fill=X, padx=24)
        Frame(details, bg=LINE, height=1).pack(fill=X, padx=24, pady=25)
        Label(details, text="连接状态", bg=CARD, fg=MUTED, font=(FONT, 10), anchor="w").pack(fill=X, padx=24, pady=(0, 6))
        Label(details, text="● 已连接", bg=CARD, fg=GREEN, font=(FONT, 12), anchor="w").pack(fill=X, padx=24)
        Label(details, textvariable=self.listener_status, bg=CARD, fg=MUTED, font=(FONT, 9), justify="left", anchor="w", wraplength=210).pack(fill=X, padx=24, pady=(14, 0))
        self.device_combo = ttk.Combobox(details, textvariable=self.device_var, state="readonly", width=26)
        self.device_combo.pack(fill=X, padx=24, pady=(18, 6))
        controls = Frame(details, bg=CARD); controls.pack(fill=X, padx=24)
        Button(controls, text="刷新设备", command=self.refresh_devices, bg="#f1f1f4", activebackground="#e3e3e8", fg=TEXT, relief="flat", bd=0, font=(FONT, 9), padx=7, pady=6).pack(side=LEFT, expand=True, fill=X, padx=(0, 4))
        Button(controls, text="开始监听", command=self.start_listener, bg=BLUE, activebackground="#006de0", fg="white", relief="flat", bd=0, font=(FONT, 9), padx=7, pady=6).pack(side=LEFT, expand=True, fill=X, padx=(4, 0))
        Button(details, text="启动小米遥控器 2", command=self.start_all, bg=BLUE, fg="white", relief="flat", bd=0, padx=7, pady=6).pack(fill=X, padx=24, pady=(6, 0))
        Button(details, text="停止监听", command=self.stop_listener, bg="#f1f1f4", activebackground="#e3e3e8", fg=MUTED, relief="flat", bd=0, font=(FONT, 9), padx=7, pady=6).pack(fill=X, padx=24, pady=(6, 0))
        self.refresh_devices()
        self._build_voice_controls(details)

    def _build_voice_controls(self, parent):
        Frame(parent, bg=LINE, height=1).pack(fill=X, padx=24, pady=20)
        Label(parent, text="语音输入（程序选 CABLE Input；麦克风测试选 CABLE Output）", bg=CARD, fg=MUTED, font=(FONT, 10), anchor="w", wraplength=230).pack(fill=X, padx=24)
        Label(parent, textvariable=self.voice_status, bg=CARD, fg=TEXT, font=(FONT, 9), anchor="w", wraplength=210).pack(fill=X, padx=24, pady=(6, 8))
        self.ble_device_combo = ttk.Combobox(parent, textvariable=self.ble_device_var, state="readonly", width=25)
        self.ble_device_combo.pack(fill=X, padx=24, pady=(0, 6))
        self.audio_combo = ttk.Combobox(parent, textvariable=self.audio_var, state="readonly", width=25)
        self.audio_combo.pack(fill=X, padx=24, pady=(0, 6))
        self.audio_combo.bind("<<ComboboxSelected>>", self._audio_output_selected)
        self.input_var = StringVar(value="CABLE Output（系统输入）")
        self.input_combo = ttk.Combobox(parent, textvariable=self.input_var, state="readonly", width=25)
        self.input_combo.pack(fill=X, padx=24, pady=(0, 6))
        controls = Frame(parent, bg=CARD); controls.pack(fill=X, padx=24)
        Button(controls, text="刷新设备", command=self.refresh_voice_devices, bg="#f1f1f4", relief="flat", bd=0, padx=6, pady=5).pack(side=LEFT, expand=True, fill=X, padx=(0, 3))
        Button(controls, text="连接语音", command=self.connect_voice, bg=BLUE, fg="white", relief="flat", bd=0, padx=6, pady=5).pack(side=LEFT, expand=True, fill=X, padx=(3, 0))
        self.refresh_audio_outputs()
        self.refresh_input_devices()
        self.refresh_ble_devices()

    def refresh_voice_devices(self):
        self.refresh_audio_outputs()
        self.refresh_input_devices()
        self.refresh_ble_devices()

    def refresh_ble_devices(self):
        self.ble_device_var.set("正在扫描 BLE 遥控器")
        def worker():
            try:
                devices = list_paired_remotes_sync()
                self.root.after(0, lambda: self._set_ble_devices(devices))
            except Exception as exc:
                self.root.after(0, lambda: self.ble_device_var.set(f"BLE 扫描失败：{exc}"))
        threading.Thread(target=worker, name="xiaomi-ble-scan", daemon=True).start()

    def _set_ble_devices(self, devices):
        self.ble_devices = devices
        values = [f"{device.name} · {device.device_id[-17:]}" for device in devices]
        self.ble_device_combo["values"] = values
        if values:
            self.ble_device_combo.current(0)
            self.voice_status.set(f"BLE 语音：发现 {len(values)} 个遥控器")
        else:
            self.ble_device_var.set("未找到已配对 BLE 遥控器")

    def _audio_output_selected(self, _event=None):
        selected = self.audio_var.get()
        if selected and not selected.startswith(("未", "请")):
            self.voice.output.device_name = selected

    def refresh_input_devices(self):
        try:
            import sounddevice as sd
            names = [str(d["name"]) for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
            self.input_combo["values"] = names
            self.input_var.set(next((n for n in names if n.casefold().startswith("cable output")), names[0] if names else "未找到系统输入设备"))
        except Exception:
            self.input_combo["values"] = []

    def start_all(self):
        self.refresh_devices()
        if len(self.device_paths) == 1:
            self.device_combo.current(1)
        self.start_listener()
        self.refresh_audio_outputs()
        self.connect_voice()

    def _on_voice_status(self, text):
        self.root.after(0, lambda: self.voice_status.set(text))

    def refresh_audio_outputs(self):
        try:
            names = PCMOutput.list_devices()
            self.audio_combo["values"] = names
            preferred = next((n for n in names if n.strip().casefold().startswith("cable input")), names[0] if names else "")
            if preferred:
                self.audio_var.set(preferred); self.voice.output.device_name = preferred
        except Exception as exc:
            self.audio_combo["values"] = []
            self.audio_var.set("未找到音频输出设备")
            self.voice_status.set(f"语音：音频不可用（{exc}）")

    def connect_voice(self):
        selected = self.audio_var.get()
        if selected and not selected.startswith("未") and not selected.startswith("请"):
            self.voice.output.device_name = selected
        index = self.ble_device_combo.current() if hasattr(self, "ble_device_combo") else -1
        device_id = self.ble_devices[index].device_id if 0 <= index < len(self.ble_devices) else None
        def worker():
            try:
                self.voice.connect(device_id)
            except Exception as exc:
                self._on_voice_status(f"BLE 语音：连接失败（{exc}）")
        threading.Thread(target=worker, name="xiaomi-voice-connect", daemon=True).start()

    def _draw_remote(self, canvas):
        _rounded_rect(canvas, 115, 26, 355, 500, radius=38, fill="#202124", outline="#383a40", width=2)
        canvas.create_oval(184, 48, 286, 150, fill="#34363b", outline="#555860", width=1)
        self._remote_button(canvas, "power", 235, 78, "⏻", 24, "#ff453a")
        self._remote_button(canvas, "voice", 235, 119, "●", 17, "#f5f5f7")
        self._remote_button(canvas, "up", 235, 202, "▲", 18)
        self._remote_button(canvas, "left", 193, 244, "◀", 18)
        self._remote_button(canvas, "ok", 235, 244, "OK", 13, BLUE)
        self._remote_button(canvas, "right", 277, 244, "▶", 18)
        self._remote_button(canvas, "down", 235, 286, "▼", 18)
        self._remote_button(canvas, "back", 174, 342, "‹", 23)
        self._remote_button(canvas, "home", 215, 342, "⌂", 20)
        self._remote_button(canvas, "menu", 256, 342, "≡", 20)
        self._remote_button(canvas, "tv", 297, 342, "TV", 11)
        self._remote_button(canvas, "volume_minus", 174, 402, "−", 22)
        self._remote_button(canvas, "volume_plus", 297, 402, "+", 20)
        canvas.create_text(235, 456, text="MI", fill="#777b84", font=("Segoe UI", 10, "bold"))

    def _remote_button(self, canvas, button, x, y, label, size, color="#d5d7dc"):
        radius = 20 if button in {"up", "down", "left", "right", "ok"} else 16
        fill = "#42454c" if button != "ok" else BLUE
        oval = canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=fill, outline="#5b5e66", width=1, tags=(button,))
        text = canvas.create_text(x, y, text=label, fill=color, font=("Segoe UI", size, "bold"), tags=(button,))
        canvas.tag_bind(oval, "<Button-1>", lambda _event, b=button: self._select_button(b))
        canvas.tag_bind(text, "<Button-1>", lambda _event, b=button: self._select_button(b))

    def _select_button(self, button):
        self.current_button.set(BUTTON_LABELS[button])

    def refresh_devices(self):
        try:
            self.device_paths = enumerate_device_paths()
            values = ["自动选择设备"] + [display_device_path(path, index + 1) for index, path in enumerate(self.device_paths)]
            self.device_combo["values"] = values
            self.device_combo.current(0)
            self.listener_status.set(f"发现 {len(self.device_paths)} 个遥控器设备")
        except Exception as exc:
            self.device_paths = []
            self.device_combo["values"] = ["自动选择设备"]
            self.device_combo.current(0)
            self.listener_status.set(f"设备扫描失败：{exc}")

    def _selected_device_path(self):
        index = self.device_combo.current()
        return self.device_paths[index - 1] if index > 0 and index <= len(self.device_paths) else None

    def start_listener(self):
        if self.listening: return
        try:
            self.listener.start(self._selected_device_path())
            self.listening = True
            self.status.set("监听中")
            self.listener_status.set("监听中 · 只处理所选遥控器")
        except Exception as exc:
            messagebox.showerror("启动监听失败", str(exc))
            self.listener_status.set(f"监听启动失败：{exc}")

    def stop_listener(self):
        try: self.listener.stop()
        except Exception as exc: self.listener_status.set(f"停止监听失败：{exc}")
        self._release_held()
        self.listening = False
        self.status.set("已连接")
        self.listener_status.set("监听已停止")

    def _on_listener_status(self, text):
        self._append_live_log(f"STATUS\t{text}")
        self.root.after(0, lambda: self.listener_status.set(text))

    def _on_remote_raw(self, data, edge, button, details):
        self._append_live_log(f"EVENT\t{edge}\t{button}\t{data.hex(' ').upper()}\t{details}")
        if button not in REMOTE_BUTTONS:
            self.root.after(0, lambda e=edge, d=details: self.listener_status.set(f"未识别信号 {e} · {d}"))
            return
        if button == "voice" and edge in ("DOWN", "UP"):
            (self.voice.press if edge == "DOWN" else self.voice.release)()
        self.root.after(0, lambda b=button, e=edge: self._apply_mapping(b, e))
        self.root.after(0, lambda b=button: self.current_button.set(BUTTON_LABELS[b]))

    def _append_live_log(self, text):
        try:
            with Path(__file__).with_name("xiaomi_remote2_live.log").open("a", encoding="utf-8") as log:
                log.write(text + "\n")
        except OSError:
            pass

    def _apply_mapping(self, button, edge):
        if edge == "REPEAT": return
        if button == "power" and self._power_uses_backspace():
            self._apply_power_backspace(edge)
            return
        if edge == "DOWN":
            text = self.mapping_vars.get(button, StringVar(value=self.bindings[button]["mapping"])).get()
            try: combo = parse_key_combo(text)
            except ValueError as exc:
                self.listener_status.set(f"{BUTTON_LABELS[button]} 映射无效：{exc}")
                return
            try:
                send_combo_down(*combo)
                self.held_combos[button] = combo
                self.listener_status.set(f"{BUTTON_LABELS[button]} → {text}")
            except Exception as exc:
                self.listener_status.set(f"发送映射失败：{exc}")
        elif edge == "UP":
            combo = self.held_combos.pop(button, None)
            if combo is not None:
                try: send_combo_up(*combo)
                except Exception as exc: self.listener_status.set(f"释放映射失败：{exc}")

    def _power_uses_backspace(self):
        text = self.mapping_vars.get("power", StringVar(value=self.bindings["power"]["mapping"])).get()
        try:
            return parse_key_combo(text) == ((), 0x08)
        except ValueError:
            return False

    def _apply_power_backspace(self, edge):
        if edge == "DOWN":
            if self.power_long_press_job is not None:
                return
            self.power_long_press_triggered = False
            self.power_long_press_job = self.root.after(POWER_LONG_PRESS_MS, self._clear_text_from_power_hold)
            self.listener_status.set("电源 → 短按删除，长按清空")
        elif edge == "UP":
            if self.power_long_press_job is not None:
                self.root.after_cancel(self.power_long_press_job)
                self.power_long_press_job = None
            if not self.power_long_press_triggered:
                send_combo_tap((), 0x08)
            self.power_long_press_triggered = False

    def _clear_text_from_power_hold(self):
        self.power_long_press_job = None
        self.power_long_press_triggered = True
        clear_all_text()
        self.listener_status.set("电源长按 → 已清空文字")

    def _release_held(self):
        if self.power_long_press_job is not None:
            self.root.after_cancel(self.power_long_press_job)
            self.power_long_press_job = None
        self.power_long_press_triggered = False
        for combo in tuple(self.held_combos.values()):
            try: send_combo_up(*combo)
            except Exception: pass
        self.held_combos.clear()

    def _mapping_page(self):
        self._header("按键映射", "自定义遥控器上的按键行为。原始按键保持不变，你可以为每个按键指定新的操作。")
        outer = Frame(self.content, bg=BG)
        outer.pack(fill=BOTH, expand=True, padx=42, pady=(0, 32))
        card = self._card(outer); card.pack(fill=BOTH, expand=True)
        top = Frame(card, bg=CARD); top.pack(fill=X, padx=26, pady=(22, 12))
        Label(top, text="原始功能", bg=CARD, fg=MUTED, font=(FONT, 9, "bold"), width=25, anchor="w").pack(side=LEFT)
        Label(top, text="自定义功能", bg=CARD, fg=MUTED, font=(FONT, 9, "bold"), width=38, anchor="w").pack(side=LEFT)
        Label(top, text="恢复", bg=CARD, fg=MUTED, font=(FONT, 9, "bold"), anchor="w").pack(side=RIGHT, padx=8)
        Frame(card, bg=LINE, height=1).pack(fill=X, padx=26)
        scroll_host = Frame(card, bg=CARD); scroll_host.pack(fill=BOTH, expand=True, padx=20, pady=(6, 4))
        scroll_canvas = Canvas(scroll_host, bg=CARD, highlightthickness=0)
        scrollbar = ttk.Scrollbar(scroll_host, orient="vertical", command=scroll_canvas.yview)
        scroll_canvas.configure(yscrollcommand=scrollbar.set)
        scroll_canvas.pack(side=LEFT, fill=BOTH, expand=True); scrollbar.pack(side=RIGHT, fill=Y)
        rows = Frame(scroll_canvas, bg=CARD)
        window_id = scroll_canvas.create_window((0, 0), window=rows, anchor="nw")
        rows.bind("<Configure>", lambda _event: scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all")))
        scroll_canvas.bind("<Configure>", lambda event: scroll_canvas.itemconfigure(window_id, width=event.width))
        scroll_canvas.bind_all("<MouseWheel>", lambda event: scroll_canvas.yview_scroll(int(-event.delta / 120), "units") if self.page == "mapping" else None)
        self.mapping_vars = {}
        for button in REMOTE_BUTTONS:
            row = Frame(rows, bg=CARD, height=42); row.pack(fill=X, pady=2); row.pack_propagate(False)
            Label(row, text=self.bindings[button]["original"], bg=CARD, fg=TEXT, font=(FONT, 10), width=25, anchor="w").pack(side=LEFT)
            var = StringVar(value=self.bindings[button]["mapping"]); self.mapping_vars[button] = var
            entry = Entry(row, textvariable=var, bg="#fafafa", fg=TEXT, insertbackground=BLUE, relief="flat", highlightthickness=1, highlightbackground=LINE, highlightcolor=BLUE, font=(FONT, 10))
            entry.pack(side=LEFT, fill=X, expand=True, padx=(0, 18), ipady=7)
            entry.bind("<KeyRelease>", lambda _event, b=button: self._mapping_changed(b))
            Button(row, text="恢复默认", command=lambda b=button: self._reset_mapping(b), bg="#f2f2f5", activebackground="#e3e3e8", fg=MUTED, relief="flat", bd=0, padx=10, pady=5, font=(FONT, 9), cursor="hand2").pack(side=RIGHT)
        bottom = Frame(card, bg=CARD); bottom.pack(fill=X, padx=26, pady=(4, 20))
        Label(bottom, text="修改只影响右侧自定义功能，左侧原始功能不会变化。", bg=CARD, fg=MUTED, font=(FONT, 9), anchor="w").pack(side=LEFT)
        Button(bottom, text="保存映射", command=self.save, bg="#f1f1f4", activebackground="#e3e3e8", fg=TEXT, relief="flat", bd=0, padx=14, pady=7, font=(FONT, 9), cursor="hand2").pack(side=RIGHT, padx=(0, 8))
        Button(bottom, text="恢复全部默认", command=self._reset_all, bg=BLUE, fg="white", activebackground="#006de0", relief="flat", bd=0, padx=14, pady=7, font=(FONT, 9), cursor="hand2").pack(side=RIGHT)

    def _mapping_changed(self, button):
        self.bindings[button]["mapping"] = self.mapping_vars[button].get()

    def _reset_mapping(self, button):
        self.mapping_vars[button].set(self.bindings[button]["original"])
        self.bindings[button]["mapping"] = self.bindings[button]["original"]

    def _reset_all(self):
        for button in REMOTE_BUTTONS: self._reset_mapping(button)

    def _settings_page(self):
        self._header("设置", "应用外观和原型行为设置。第一阶段暂不连接真实硬件。")
        body = Frame(self.content, bg=BG); body.pack(fill=BOTH, expand=True, padx=42, pady=(0, 32))
        for title, detail in (("外观", "跟随 Windows 11 浅色外观\n后续可加入深色模式和主题选择"), ("遥控器", "小米遥控器 2 PC\n13 个已确认按键"), ("语音输入", "先安装 WinRT BLE、sounddevice 和 VB-CABLE，再在遥控器页选择音频设备并连接语音"), ("数据", "映射数据保存在独立结构中\noriginal 与 mapping 分离")):
            card = self._card(body); card.pack(fill=X, pady=6); Label(card, text=title, bg=CARD, fg=TEXT, font=("Segoe UI", 12, "bold"), anchor="w").pack(fill=X, padx=24, pady=(18, 4)); Label(card, text=detail, bg=CARD, fg=MUTED, font=("Segoe UI", 10), justify="left", anchor="w").pack(fill=X, padx=24, pady=(0, 18))

    def save(self):
        payload = {button: {"original": data["original"], "mapping": data["mapping"]} for button, data in self.bindings.items()}
        self.mapping_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def close(self):
        self.stop_listener()
        self.f5_suppressor.stop()
        self.voice.close()
        self.root.destroy()


def main():
    root = Tk()
    RemotePrototypeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
