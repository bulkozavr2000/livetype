"""Fixes from the pre-release review: downloads, hold mode, lost cursor, own windows, settings files."""

import hashlib
import http.server
import json
import os
import queue
import threading

import pytest

from livetype import app as app_mod
from livetype import config, win
from livetype.app import App, Typer
from livetype.download import DownloadError, fetch


# --------------------------------------------------------------------------- downloads
class _Handler(http.server.BaseHTTPRequestHandler):
    body = b"x" * 5000
    claim = 5000  # Content-Length we announce

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(self.claim))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/f"
    srv.shutdown()


def test_download_checks_size_and_hash(server, tmp_path):
    body = _Handler.body
    good = hashlib.sha256(body).hexdigest()
    dst = tmp_path / "m.onnx"
    seen = []
    fetch(server, dst, len(body), good, seen.append)
    assert dst.read_bytes() == body and seen[-1] == len(body)
    # a changed file on the server: refused, nothing left behind
    dst2 = tmp_path / "m2.onnx"
    with pytest.raises(DownloadError):
        fetch(server, dst2, len(body), "0" * 64)
    assert not dst2.exists() and not (tmp_path / "m2.onnx.part").exists()


def test_download_detects_dropped_connection(server, tmp_path, monkeypatch):
    # the server promises more than it sends and closes: urllib returns a short read silently
    monkeypatch.setattr(_Handler, "claim", 8000)
    dst = tmp_path / "m.onnx"
    with pytest.raises(DownloadError, match="5000 из 8000"):
        fetch(server, dst, 8000, "0" * 64)
    assert not dst.exists()


# --------------------------------------------------------------------------- hold mode
def _bare_app(mode="hold"):
    a = App.__new__(App)  # no models: only the hotkey logic
    a.cfg = config.Config()
    a.cfg.activation_mode = mode
    a.cmds = queue.Queue()
    a.phase = "idle"
    a.hold_down = False
    return a


def test_hold_short_press_stops_even_while_starting():
    a = _bare_app()
    a.hotkey_down()
    assert a.cmds.get_nowait() == "toggle" and a.hold_down
    a.phase = "starting"  # the worker took it, microphone not open yet
    a.hotkey_up()
    assert a.cmds.get_nowait() == "toggle" and not a.hold_down
    a.phase = "processing"  # releasing during the final check does nothing
    a.hotkey_up()
    assert a.cmds.empty()


def test_toggle_mode_ignores_release():
    a = _bare_app("toggle")
    a.hotkey_down()
    a.phase = "listening"
    a.hotkey_up()
    assert a.cmds.qsize() == 1


# --------------------------------------------------------------------------- lost cursor
class _Ui:
    def __init__(self):
        self.notices = []

    def notice(self, title, subtitle="", **kw):
        self.notices.append(title)


class _Field:
    """A text field with the focus; `same` = it is the field where the dictation started."""

    def __init__(self, same: bool = True, selected: str = ""):
        self.same, self.el, self.selected = same, object(), selected

    def same_as(self, el):
        return self.same

    def selection(self):
        return None  # an empty field: nothing before or after the cursor

    def selected_text(self):
        return self.selected


class _Uia:
    def __init__(self, field):
        self.field = field

    def text_field(self):
        return self.field


class _LostAnchor:
    text = "начало "

    def __init__(self, field=None):
        self.field = field or _Field()

    def find(self):
        return "", None, None  # our text is no longer found in the field


def test_lost_place_goes_to_clipboard_not_keystrokes(monkeypatch):
    typed, clip = [], []
    monkeypatch.setattr(win, "type_text", lambda t, *a, **k: typed.append(t))
    monkeypatch.setattr(win, "set_clipboard", lambda t: clip.append(t))
    monkeypatch.setattr(win, "window_process", lambda h: "notepad.exe")
    ui = _Ui()
    t = Typer(config.Config(), 1, None, ui, lambda: None)
    # the cursor is in another field now (a search box...): typing there would be wrong
    t.mode, t.anchor, t.uia = "uia", _LostAnchor(), _Uia(_Field(same=False))
    t.base, t._user_moved = "начало ", True
    t._type("и конец")
    assert typed == [] and t.mode == "lost" and ui.notices
    t._type(" ещё")
    assert typed == []
    assert t._final("Начало и конец ещё.", "") == "clipboard_lost"
    assert clip == ["Начало и конец ещё."]


def test_lost_place_in_the_same_field_goes_on_at_the_cursor(monkeypatch):
    typed = []
    monkeypatch.setattr(win, "type_text", lambda t, *a, **k: typed.append(t))
    monkeypatch.setattr(win, "window_process", lambda h: "notepad.exe")
    t = Typer(config.Config(), 1, None, _Ui(), lambda: None)
    field = _Field()  # e.g. the message was sent and the composer is empty again
    t.mode, t.anchor, t.uia = "uia", _LostAnchor(field), _Uia(field)
    t.base, t._user_moved = "начало", True
    t._type(" и конец")
    assert typed == ["и конец"] and t.mode == "uia"  # no leading space in an empty field
    assert t.base == "начало и конец" and t.base[t.seg_start:] == "и конец"
    assert t.anchor.field is field and t.anchor.text == "и конец" and t.at_end
    t._type(" ещё")  # the next words simply follow at the cursor
    assert typed == ["и конец", " ещё"] and t.base[t.seg_start:] == "и конец ещё"


def test_lost_place_never_types_over_a_selection(monkeypatch):
    typed = []
    monkeypatch.setattr(win, "type_text", lambda t, *a, **k: typed.append(t))
    monkeypatch.setattr(win, "window_process", lambda h: "notepad.exe")
    t = Typer(config.Config(), 1, None, _Ui(), lambda: None)
    field = _Field(selected="текст, который вы выделили")
    t.mode, t.anchor, t.uia = "uia", _LostAnchor(field), _Uia(field)
    t.base, t._user_moved = "начало", True
    t._type(" и конец")
    assert typed == [] and t.mode == "lost"


def test_terminal_detection(monkeypatch):
    def window(cls, proc):
        monkeypatch.setattr(win, "window_class", lambda h: cls)
        monkeypatch.setattr(win, "window_process", lambda h: proc)

    window("ConsoleWindowClass", "powershell.exe")  # classic console
    assert app_mod.is_terminal(1)
    window("HwndWrapper[target_wpf;;1]", "powershell.exe")  # a WPF window owned by powershell.exe
    assert not app_mod.is_terminal(1)
    window("CASCADIA_HOSTING_WINDOW_CLASS", "windowsterminal.exe")
    t = Typer(config.Config(), 1, None, _Ui(), lambda: None)
    t._init_uia()
    assert t.terminal and t.mode == "keys" and t.uia is None


# --------------------------------------------------------------------------- own windows
def test_ui_windows_count_as_own(monkeypatch):
    import ctypes
    from ctypes import wintypes

    shell = ctypes.windll.user32.GetShellWindow()  # explorer's desktop window: another process
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(shell, ctypes.byref(pid))
    monkeypatch.delenv("LIVETYPE_UI_PID", raising=False)
    assert not win.is_own_window(shell)
    monkeypatch.setenv("LIVETYPE_UI_PID", str(pid.value))
    assert win.is_own_window(shell)


# --------------------------------------------------------------------------- settings files
def test_config_save_is_atomic_and_keeps_default_models_dir_out(tmp_path):
    path = tmp_path / "config.json"
    c = config.Config()
    c.hotkey = "RightAlt"
    c.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hotkey"] == "RightAlt" and "models_dir" not in data
    assert not path.with_suffix(".tmp").exists()
    c.models_dir = str(tmp_path / "elsewhere")
    c.save(path)
    assert config.Config.load(path).models_dir == str(tmp_path / "elsewhere")


def test_history_keeps_the_last_entries(tmp_path, monkeypatch):
    hist = tmp_path / "history.jsonl"
    monkeypatch.setattr(app_mod, "HISTORY_PATH", hist)
    for i in range(30):
        App._save_history(f"текст {i}", keep=5)
    lines = hist.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 25 and json.loads(lines[-1])["text"] == "текст 29"


# --------------------------------------------------------------------------- microphone by name
def test_microphone_name_resolves_to_mme_index():
    from livetype.audio import resolve_input_device
    from livetype.ipc import list_devices

    devices = list_devices()
    assert resolve_input_device("нет такого микрофона") is None  # unplugged -> default
    assert resolve_input_device(None) is None and resolve_input_device(3) == 3
    if not devices:
        pytest.skip("no microphones")
    import sounddevice as sd

    idx = resolve_input_device(devices[0]["name"])
    assert isinstance(idx, int)
    d = sd.query_devices(idx)
    assert d["name"] == devices[0]["name"] and sd.query_hostapis(d["hostapi"])["name"] == "MME"


def test_env_untouched():
    assert "LIVETYPE_UI_PID" not in os.environ or os.environ["LIVETYPE_UI_PID"].isdigit()
