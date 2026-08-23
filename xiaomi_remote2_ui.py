"""First-stage Windows 11 UI prototype for Xiaomi Remote 2 PC.

This module intentionally contains presentation and editable mapping state only.
It does not start Raw Input, inject keyboard events, or connect BLE.
"""
from __future__ import annotations

import json
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, Canvas, Entry, Frame, Label, StringVar, Tk, Button, PhotoImage
from tkinter import ttk

from XiaomiRemote2_Windows import BUTTON_LABELS, REMOTE_BUTTONS


BG = "#f5f5f7"
SIDEBAR = "#ededf0"
CARD = "#ffffff"
TEXT = "#1d1d1f"
MUTED = "#707078"
BLUE = "#007aff"
LINE = "#dedee3"
GREEN = "#34c759"
FONT = "Microsoft YaHei UI"


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
        self.mapping_file = Path(__file__).with_name("xiaomi_remote2_ui_mapping.json")
        self.bindings = self._load_bindings()
        self.nav_buttons: dict[str, Button] = {}
        self.content: Frame | None = None
        self._setup_style()
        self._build_shell()
        self.show_page("remote")

    def _setup_style(self):
        style = ttk.Style(self.root)
        try: style.theme_use("clam")
        except Exception: pass
        style.configure("Prototype.TScrollbar", troughcolor=BG, background="#c7c7cc", bordercolor=BG, arrowcolor=MUTED)

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
        for key, label in (("remote", "遥控器"), ("mapping", "按键映射"), ("settings", "设置")):
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
        Label(details, text="按键交互已准备好\n第一阶段使用模拟状态", bg=CARD, fg=MUTED, font=(FONT, 9), justify="left", anchor="w").pack(fill=X, padx=24, pady=(14, 0))

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
        for title, detail in (("外观", "跟随 Windows 11 浅色外观\n后续可加入深色模式和主题选择"), ("遥控器", "小米遥控器 2 PC\n13 个已确认按键"), ("数据", "映射数据保存在独立结构中\noriginal 与 mapping 分离")):
            card = self._card(body); card.pack(fill=X, pady=6); Label(card, text=title, bg=CARD, fg=TEXT, font=("Segoe UI", 12, "bold"), anchor="w").pack(fill=X, padx=24, pady=(18, 4)); Label(card, text=detail, bg=CARD, fg=MUTED, font=("Segoe UI", 10), justify="left", anchor="w").pack(fill=X, padx=24, pady=(0, 18))

    def save(self):
        payload = {button: {"original": data["original"], "mapping": data["mapping"]} for button, data in self.bindings.items()}
        self.mapping_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    root = Tk()
    RemotePrototypeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
