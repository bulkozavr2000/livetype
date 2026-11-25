"""Engine mode for the Electron UI (python -m livetype --ipc).

stdout: one JSON event per line; stdin: one JSON command per line (see ui/ and the protocol
in docs). Nothing else may be printed to stdout in this mode.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time

from livetype import __version__
from livetype.config import HISTORY_PATH, PANEL_KEYS, Config

log = logging.getLogger("livetype")


class IpcUi:
    """Same interface as the Tk overlay, but sends events to the Electron UI."""

    wants_unit_level = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._level_t = 0.0
        self.closed = threading.Event()
        self._done_timer: threading.Timer | None = None

    def emit(self, event: str, **data: object) -> None:
        line = json.dumps({"event": event, **data}, ensure_ascii=False)
        with self._lock:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except (OSError, ValueError):
                self.closed.set()

    # level is set ~31 times per second by the engine thread
    @property
    def level(self) -> float:
        return 0.0

    @level.setter
    def level(self, value: float) -> None:
        now = time.monotonic()
        if now - self._level_t >= 0.03:
            self._level_t = now
            self.emit("level", value=round(float(value), 3))

    def _state(self, state: str, detail: dict | None = None) -> None:
        if self._done_timer:
            self._done_timer.cancel()
            self._done_timer = None
        self.emit("state", state=state, **({"detail": detail} if detail else {}))

    def listening(self) -> None:
        self._state("listening")

    def processing(self) -> None:
        self._state("processing")

    def done(self, title: str = "Готово", subtitle: str = "", ttl: float = 1.6) -> None:
        self._state("done", {"title": title, "subtitle": subtitle})
        self._done_timer = threading.Timer(0.4, lambda: self.emit("state", state="idle"))
        self._done_timer.start()

    def notice(self, title: str, subtitle: str = "", color: str | None = None, ttl: float | None = None,
               state: str | None = None, kind: str | None = None) -> None:
        self.emit("notice", title=title, body=subtitle, kind=kind or "info")

    def hide(self) -> None:
        self._state("idle")

    def finish(self) -> None:
        """End of a session that ended with a notice (toast) instead of done: hide the capsule."""
        self.hide()

    def quit(self) -> None:
        self.closed.set()


def list_devices() -> list[dict]:
    try:
        import sounddevice as sd

        default = sd.default.device[0]
        mme = next((i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME"), None)
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and (mme is None or d["hostapi"] == mme):
                name = d["name"]
                if "Переназначение" in name or "Sound Mapper" in name:
                    continue
                out.append({"name": name, "default": i == default})
        return out
    except Exception:
        log.exception("device list failed")
        return []


def read_history(limit: int = 50) -> list[dict]:
    if not HISTORY_PATH.is_file():
        return []
    lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    items = []
    for line in reversed(lines):
        try:
            items.append(json.loads(line))
        except ValueError:
            pass
    return items


def run(cfg: Config, app, hotkeys, models_status, download_model) -> None:
    """Serve the UI until it sends quit or closes stdin."""
    ui: IpcUi = app.ui

    def settings() -> None:
        ui.emit("settings", settings=cfg.panel(), hotkey_ok=hotkeys.ok)

    def on_captured(spec: str) -> None:
        cfg.hotkey = spec
        cfg.save()
        app.hotkey_vk = next(iter(_vks(spec)), 0)
        ui.emit("hotkey_captured", hotkey=spec)
        settings()

    hotkeys.on_captured = on_captured
    hotkeys.on_capture_cancelled = lambda: ui.emit("hotkey_captured", hotkey=None)
    hotkeys.on_status = lambda ok, error: settings()
    ui.emit("ready", version=__version__, hotkey_ok=hotkeys.ok, settings=cfg.panel(),
            devices=list_devices(), models=models_status())

    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        cmd = msg.get("cmd")
        try:
            if cmd == "toggle":
                app.toggle()
            elif cmd == "cancel":
                app.cancel()
            elif cmd == "get_settings":
                settings()
            elif cmd == "set_settings":
                patch = {k: v for k, v in (msg.get("settings") or {}).items() if k in PANEL_KEYS}
                if "hotkey" in patch:
                    hotkeys.set(patch["hotkey"])
                    app.hotkey_vk = next(iter(_vks(patch["hotkey"])), 0)
                cfg.update(patch)
                cfg.save()
                settings()
                if "text_processing" in patch or "text_model" in patch:
                    app.on_text_settings_changed()
            elif cmd == "list_devices":
                ui.emit("devices", devices=list_devices())
            elif cmd == "capture_hotkey":
                hotkeys.capture()
            elif cmd == "cancel_capture":
                hotkeys.cancel_capture()
            elif cmd == "download_model":
                download_model(msg.get("model") or cfg.text_model)
            elif cmd == "get_history":
                ui.emit("history", items=read_history())
            elif cmd == "quit":
                break
        except Exception as e:
            log.exception("command %s failed", cmd)
            ui.emit("error", message=f"{cmd}: {e}")
    ui.closed.set()


def _vks(spec: str) -> set[int]:
    from livetype.hotkeys import main_vks

    return main_vks(spec)
