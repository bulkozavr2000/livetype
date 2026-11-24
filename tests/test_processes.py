"""Hotkey listener and engine IPC, run as real processes (no microphone, no typing)."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from livetype import keyhook, win

ROOT = Path(__file__).resolve().parents[1]


def test_parse_hotkeys():
    assert keyhook.parse("RightAlt") == ([], (0xA5,))
    assert keyhook.parse("ctrl+alt+d") == ([(0xA2, 0xA3), (0xA4, 0xA5)], (0x44,))
    assert keyhook.parse("ctrl+win") == ([(0xA2, 0xA3)], (0x5B, 0x5C))
    assert keyhook.parse("f9") == ([], (0x78,))
    with pytest.raises(ValueError):
        keyhook.parse("ctrl+")


def _spawn(*args, utf8_mode: bool = True):
    # utf8_mode=False runs like the packaged exe: no -X utf8, Windows code page by default
    env = {k: v for k, v in __import__("os").environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    return subprocess.Popen([sys.executable, *(["-X", "utf8"] if utf8_mode else []), "-u", *args], cwd=ROOT,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", env=env)


_readers: dict = {}


def _line(p, timeout=10.0):
    """Next stdout line or None. One reader thread per process, so a timeout never loses a line."""
    import queue
    import threading

    q = _readers.get(p.pid)
    if q is None:
        q = _readers[p.pid] = queue.Queue()
        threading.Thread(target=lambda: [q.put(x) for x in p.stdout], daemon=True).start()
    try:
        return q.get(timeout=timeout).strip()
    except queue.Empty:
        return None


def _keys(*events):
    win._send([win._key(vk, up=up, extra=0) for vk, up in events], pause=0)


def test_keyhook_process_single_key_and_capture():
    p = _spawn("-m", "livetype.keyhook", "RightAlt")
    try:
        assert _line(p) == "ready RightAlt"
        _keys((0xA5, False))
        assert _line(p) == "down"
        _keys((0xA5, False))  # auto-repeat must not fire again
        _keys((0xA5, True))
        assert _line(p) == "up"
        p.stdin.write("capture\n")
        p.stdin.flush()
        time.sleep(0.2)
        _keys((0xA2, False), (0x12, False), (0x46, False), (0x46, True), (0x12, True), (0xA2, True))
        assert _line(p) == "captured ctrl+alt+f"
        p.stdin.write("esc on\n")  # while dictating, Esc cancels
        p.stdin.flush()
        time.sleep(0.2)
        _keys((0x1B, False), (0x1B, True))
        assert _line(p) == "esc"
    finally:
        p.stdin.write("quit\n")
        p.stdin.flush()
        p.wait(5)


def _cmd(p, line):
    p.stdin.write(line + "\n")
    p.stdin.flush()
    time.sleep(0.2)


def _silent(p, seconds=0.6):
    return _line(p, seconds) is None


def test_keyhook_capture_can_be_cancelled_and_rejects_bare_keys():
    p = _spawn("-m", "livetype.keyhook", "RightAlt")
    try:
        assert _line(p) == "ready RightAlt"
        _cmd(p, "capture")
        _keys((0x1B, False), (0x1B, True))  # Esc cancels
        assert _line(p) == "capture_cancelled"
        _cmd(p, "capture")
        _keys((0x46, False), (0x46, True))  # a bare letter would hijack typing: ignored, still capturing
        assert _silent(p)
        _cmd(p, "capture off")  # the panel lost focus
        assert _line(p) == "capture_cancelled"
        _cmd(p, "capture")
        _keys((0x87, False), (0x87, True))  # F24 alone is fine
        assert _line(p) == "captured f24"
    finally:
        _cmd(p, "quit")
        p.wait(5)


def test_keyhook_trusts_real_modifier_state_and_recovers_lost_release():
    p = _spawn("-m", "livetype.keyhook", "ctrl+f24")  # F24: harmless if it reaches an app
    try:
        assert _line(p) == "ready ctrl+f24"
        # Ctrl's release is not seen by the hook (as after Ctrl+Alt+Del): F24 alone must not fire
        _keys((0xA2, False))
        win._send([win._key(0xA2, up=True, extra=win.MAGIC)], pause=0)
        time.sleep(0.1)
        _keys((0x87, False), (0x87, True))
        assert _silent(p)
        # normal press
        _keys((0xA2, False))
        time.sleep(0.05)
        _keys((0x87, False))
        assert _line(p) == "down"
        # its release is lost; the next press after a while starts over
        win._send([win._key(0x87, up=True, extra=win.MAGIC)], pause=0)
        time.sleep(1.7)
        _keys((0x87, False))
        assert _line(p) == "up" and _line(p) == "down"
        _keys((0x87, True))
        assert _line(p) == "up"
    finally:
        _keys((0xA2, True))
        _cmd(p, "quit")
        p.wait(5)


def test_engine_ipc_roundtrip():
    p = _spawn("-m", "livetype", "--ipc", utf8_mode=False)
    try:
        ready = json.loads(_line(p, 60))
        assert ready["event"] == "ready", ready
        # Cyrillic must arrive intact even without Python's UTF-8 mode (as in the packaged exe)
        assert any(r["label"] == "Без «ну» и «короче»" for r in ready["settings"]["text_rules"])
        assert {"hotkey", "activation_mode", "text_rules", "live_typing"} <= set(ready["settings"])
        assert set(ready["models"]) == {"light", "accurate"}
        assert isinstance(ready["devices"], list)
        p.stdin.write(json.dumps({"cmd": "get_history"}) + "\n")
        p.stdin.flush()
        hist = json.loads(_line(p))
        assert hist["event"] == "history" and isinstance(hist["items"], list)
        p.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
        p.stdin.flush()
        assert p.wait(15) == 0
    finally:
        if p.poll() is None:
            p.kill()
