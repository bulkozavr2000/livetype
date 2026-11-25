"""On-screen indicator: a small pill at the bottom of the screen.

It never takes keyboard focus and lets clicks pass through, so typing keeps
going into your window. Other threads talk to it only through `post()`.
"""

from __future__ import annotations

import math
import queue
import random
import time
import tkinter as tk
import tkinter.font as tkfont

from livetype import win
from livetype.win import user32

KEY = "#010203"  # transparent color
BG = "#1c1c1e"
EDGE = "#3a3a3c"
FG = "#f5f5f7"
DIM = "#a1a1a6"
RED, AMBER, GREEN, ORANGE = "#ff453a", "#ffd60a", "#30d158", "#ff9f0a"

GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000


class Overlay:
    def __init__(self, hotkey_label: str = "Ctrl+Alt+D"):
        self.hotkey_label = hotkey_label
        self.q: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.level = 0.0  # written by the audio thread, read by the UI tick
        self.state = "hidden"
        self.title = ""
        self.subtitle = ""
        self.color = RED
        self.hide_at: float | None = None
        self._phase = 0.0
        self._bars = [0.0] * 5
        self.hwnd = 0

    # ------------------------------------------------------------- thread-safe API
    def post(self, kind: str, **kw: object) -> None:
        self.q.put((kind, kw))

    def listening(self) -> None:
        self.post("show", state="listening", title="Слушаю", subtitle="", color=RED)

    def processing(self) -> None:
        self.post("show", state="processing", title="Проверяю текст…", subtitle="финальная проверка всей записи", color=AMBER)

    def done(self, title: str = "Готово", subtitle: str = "", ttl: float = 1.6) -> None:
        self.post("show", state="done", title=title, subtitle=subtitle, color=GREEN, ttl=ttl)

    def notice(self, title: str, subtitle: str = "", color: str | None = None, ttl: float | None = None,
               state: str | None = None, kind: str | None = None) -> None:
        color = color or {"success": GREEN, "error": RED}.get(kind or "", ORANGE)
        state = state or ("done" if kind == "success" else "notice")
        self.post("show", state=state, title=title, subtitle=subtitle, color=color, ttl=ttl or 4.0)

    def hide(self) -> None:
        self.post("hide")

    def finish(self) -> None:
        """End of a session that showed a notice: the notice hides itself after its ttl."""

    def quit(self) -> None:
        self.post("quit")

    # ------------------------------------------------------------- UI thread
    def run(self) -> None:
        self._build()
        self.root.after(33, self._tick)
        self.root.mainloop()

    def _build(self) -> None:
        prev_foreground = user32.GetForegroundWindow()
        self.root = root = tk.Tk()
        root.withdraw()
        root.title("LiveType")
        scale = root.winfo_fpixels("1i") / 96.0
        self.s = scale
        self.W, self.H = int(470 * scale), int(56 * scale)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=KEY)
        root.attributes("-transparentcolor", KEY)
        root.attributes("-alpha", 0.0)
        self.canvas = tk.Canvas(root, width=self.W, height=self.H, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.f_title = tkfont.Font(root=root, family="Segoe UI Semibold", size=10)
        self.f_sub = tkfont.Font(root=root, family="Segoe UI", size=9)

        # bottom center of the work area (above the taskbar)
        left, _top, right, bottom = _work_area()
        x = left + (right - left - self.W) // 2
        y = bottom - self.H - int(28 * scale)
        root.geometry(f"{self.W}x{self.H}+{x}+{y}")
        self._draw_static()
        root.update_idletasks()
        self.hwnd = user32.GetParent(root.winfo_id())
        ex = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            self.hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOPMOST
        )
        root.deiconify()
        root.update()
        # Tk activates its window once while creating it: give the focus straight back
        if prev_foreground and win.is_own_window(user32.GetForegroundWindow()):
            user32.SetForegroundWindow(prev_foreground)

    def _draw_static(self) -> None:
        c, W, H = self.canvas, self.W, self.H
        r = H // 2 - 1
        pts = []
        for cx, a0 in ((W - r - 1, -90), (r + 1, 90)):
            for k in range(0, 181, 10):
                a = math.radians(a0 + k)
                pts += [cx + r * math.cos(a), H / 2 - r * math.sin(a)]
        c.create_polygon(pts, fill=BG, outline=EDGE, width=1)
        self.t_title = c.create_text(H + 2, int(H * 0.36), anchor="w", fill=FG, font=self.f_title, text="")
        self.t_sub = c.create_text(H + 2, int(H * 0.68), anchor="w", fill=DIM, font=self.f_sub, text="")

    def _set_visible(self, on: bool) -> None:
        self.root.attributes("-alpha", 0.96 if on else 0.0)

    def _tick(self) -> None:
        try:
            while True:
                kind, kw = self.q.get_nowait()
                if kind == "quit":
                    self.root.destroy()
                    return
                self._handle(kind, kw)
        except queue.Empty:
            pass
        if self.hide_at is not None and time.monotonic() >= self.hide_at:
            self.hide_at = None
            self.state = "hidden"
            self._set_visible(False)
        if self.state != "hidden":
            self._animate()
        self.root.after(33, self._tick)

    def _handle(self, kind: str, kw: dict) -> None:
        if kind == "show":
            self.state = str(kw["state"])
            self.title = str(kw["title"])
            self.subtitle = str(kw.get("subtitle") or "")
            self.color = str(kw.get("color") or RED)
            ttl = kw.get("ttl")
            self.hide_at = time.monotonic() + float(ttl) if ttl else None
            self._set_visible(True)
        elif kind == "hide":
            self.state = "hidden"
            self.hide_at = None
            self._set_visible(False)

    def _fit_tail(self, text: str, width: int) -> str:
        if self.f_sub.measure(text) <= width:
            return text
        while text and self.f_sub.measure("…" + text) > width:
            text = text[1:]
        return "…" + text

    def _animate(self) -> None:
        c, H, W, s = self.canvas, self.H, self.W, self.s
        self._phase += 0.12
        c.delete("dyn")
        cx, cy = H // 2 + int(4 * s), H // 2
        if self.state == "listening":
            lvl = min(1.0, max(0.0, (20 * math.log10(self.level + 1e-9) + 55) / 40))
            ring = 8 * s + (3 + 4 * lvl) * s * (0.5 + 0.5 * math.sin(self._phase))
            c.create_oval(cx - ring, cy - ring, cx + ring, cy + ring, outline="#7a2a26", width=max(1, int(2 * s)), tags="dyn")
            c.create_oval(cx - 7 * s, cy - 7 * s, cx + 7 * s, cy + 7 * s, fill=RED, outline="", tags="dyn")
            x0 = W - H // 2 - int(30 * s)
            for i in range(5):
                target = lvl * (0.55 + 0.45 * random.random())
                self._bars[i] += (target - self._bars[i]) * 0.5
                h = max(2 * s, self._bars[i] * 22 * s)
                x = x0 + i * 7 * s
                c.create_rectangle(x, cy - h / 2, x + 4 * s, cy + h / 2, fill="#d1d1d6", outline="", tags="dyn")
            sub = self.subtitle or f"Говорите…   {self.hotkey_label} — закончить"
            avail = int(W - (H + 2) - 52 * s - 16 * s)
        elif self.state == "processing":
            a = (self._phase * 90) % 360
            c.create_arc(cx - 8 * s, cy - 8 * s, cx + 8 * s, cy + 8 * s, start=-a, extent=110, style="arc",
                         outline=AMBER, width=max(2, int(3 * s)), tags="dyn")
            sub, avail = self.subtitle, int(W - (H + 2) - 20 * s)
        else:
            col = self.color
            c.create_oval(cx - 9 * s, cy - 9 * s, cx + 9 * s, cy + 9 * s, fill=col, outline="", tags="dyn")
            if self.state == "done":
                c.create_line(cx - 4 * s, cy, cx - 1 * s, cy + 3.5 * s, cx + 4.5 * s, cy - 3.5 * s, fill=BG,
                              width=max(2, int(2.4 * s)), capstyle="round", joinstyle="round", tags="dyn")
            else:
                c.create_text(cx, cy, text="!", fill=BG, font=self.f_title, tags="dyn")
            sub, avail = self.subtitle, int(W - (H + 2) - 20 * s)
        c.itemconfigure(self.t_title, text=self.title)
        c.itemconfigure(self.t_sub, text=self._fit_tail(sub, avail), fill=DIM if self.state != "notice" else "#e5e5ea")


def _work_area() -> tuple[int, int, int, int]:
    import ctypes
    from ctypes import wintypes

    r = wintypes.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.left, r.top, r.right, r.bottom
