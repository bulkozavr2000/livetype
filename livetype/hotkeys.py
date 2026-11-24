"""Talks to the keyhook process (see keyhook.py)."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from livetype import keyhook

log = logging.getLogger("livetype")
ROOT = Path(__file__).resolve().parents[1]


def main_vks(spec: str) -> set[int]:
    try:
        vks = set(keyhook.parse(spec)[1])
    except ValueError:
        return set()
    # a held modifier hotkey (e.g. Right Alt in hold mode) is swallowed by the hook,
    # so it must not block typing as if the user were holding Alt
    from livetype import win

    win.IGNORED_MODIFIERS.clear()
    win.IGNORED_MODIFIERS.update(vks & keyhook.MOD_VKS)
    return vks


class Hotkeys:
    def __init__(self, spec: str, on_down: Callable[[], None], on_up: Callable[[], None],
                 on_captured: Callable[[str], None] | None = None):
        self.spec = spec
        self.on_down, self.on_up, self.on_captured = on_down, on_up, on_captured
        self.on_esc: Callable[[], None] = lambda: None
        self.on_capture_cancelled: Callable[[], None] = lambda: None
        self.on_status: Callable[[bool, str], None] = lambda ok, error: None
        self.ok = False
        self.error = ""
        self.ready = threading.Event()
        if getattr(sys, "frozen", False):  # packaged app: the engine exe runs the listener itself
            cmd, cwd = [sys.executable, "--keyhook", spec], Path(sys.executable).parent
        else:
            exe = Path(sys.executable)
            pyw = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
            cmd, cwd = [str(pyw), "-X", "utf8", "-u", "-m", "livetype.keyhook", spec], ROOT
        self.proc = subprocess.Popen(
            cmd,
            cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._read, name="hotkeys", daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            cmd, _, arg = line.strip().partition(" ")
            if cmd == "down":
                self.on_down()
            elif cmd == "up":
                self.on_up()
            elif cmd == "esc":
                self.on_esc()
            elif cmd == "ready":
                first = not self.ready.is_set()
                self.ok, self.spec, self.error = True, arg, ""
                self.ready.set()
                if not first:
                    self.on_status(True, "")
            elif cmd == "captured":
                self.spec = arg
                if self.on_captured:
                    self.on_captured(arg)
            elif cmd == "capture_cancelled":
                self.on_capture_cancelled()
            elif cmd == "error":
                self.error = arg
                log.error("hotkey: %s", arg)
                if not self.ready.is_set():
                    self.ok = False
                self.ready.set()
                self.on_status(self.ok, arg)

    def _send(self, line: str) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            log.exception("hotkey process is gone")

    def set(self, spec: str) -> None:
        keyhook.parse(spec)  # raises ValueError for a bad spec
        self._send(f"set {spec}")

    def capture(self) -> None:
        self._send("capture")

    def cancel_capture(self) -> None:
        self._send("capture off")

    def arm_esc(self, on: bool) -> None:
        self._send("esc on" if on else "esc off")

    def stop(self) -> None:
        self._send("quit")
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
