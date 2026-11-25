"""Controller: hotkey -> listen -> type words live -> final check -> fix the text in place."""

from __future__ import annotations

import difflib
import json
import logging
import math
import os
import queue
import struct
import threading
import time
import wave
import winsound
from collections.abc import Callable
from dataclasses import dataclass, field

from livetype import win
from livetype.asr import Recognizer, StreamingVad
from livetype.audio import Source
from livetype.config import DATA_DIR, HISTORY_PATH, Config
from livetype.engine import DictationEngine
from livetype.textops import common_affixes, merge_user_edits, plan_replacement

log = logging.getLogger("livetype")

HISTORY_KEEP = 200  # dictations kept in history.jsonl

# terminals: input goes to the prompt only, Shift+Left does not select text, Ctrl+End deletes in
# cmd/PowerShell. Recognized by the window class (classic consoles report the client process,
# e.g. powershell.exe, which can also own ordinary GUI windows) or by the terminal app's process.
TERMINAL_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "mintty", "PuTTY", "VirtualConsoleClass"}
TERMINAL_PROCESSES = {
    "windowsterminal.exe", "openconsole.exe", "conhost.exe", "mintty.exe", "putty.exe", "kitty.exe",
    "alacritty.exe", "wezterm-gui.exe", "conemu.exe", "conemu64.exe", "tabby.exe", "hyper.exe",
}


def is_terminal(hwnd: int) -> bool:
    return win.window_class(hwnd) in TERMINAL_CLASSES or win.window_process(hwnd) in TERMINAL_PROCESSES


def count_changes(old: str, new: str) -> int:
    sm = difflib.SequenceMatcher(a=old.split(), b=new.split(), autojunk=False)
    return sum(1 for op in sm.get_opcodes() if op[0] != "equal")


class Typer(threading.Thread):
    """Types settled words into the field where the dictation started.

    * Another window in front -> waits (never types into the wrong app).
    * You type or click yourself -> waits until you pause. With UI Automation the next words
      go right after *our* text and your cursor is put back; without it (rare apps) the
      typing continues at the end of the field (Ctrl+End).
    * At the end: applies the final corrections in place, keeping words you changed by hand.
    """

    def __init__(self, cfg: Config, target: int, watcher: win.InputWatcher | None, ui,
                 restore_ui: Callable[[], None], live: bool = True):
        super().__init__(name="typer", daemon=True)
        self.cfg, self.target, self.watcher, self.ui = cfg, target, watcher, ui
        self.restore_ui = restore_ui
        self.live = live  # False: keep words until the end and insert the final text once (Type's style)
        self.q: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.base = ""  # everything we typed, in order (our live text)
        self.edited = False  # keystroke mode: the user touched the field, positions are unknown
        self.user_edits = 0
        self.mode = "keys"  # "uia" | "keys" | "lost" (our place in the field is gone: rest -> clipboard)
        self.lost = ""
        self.terminal = is_terminal(target)
        self.uia = None
        self.anchor = None
        self.at_end = True
        self._user_moved = False
        self._seen = watcher.count if watcher else 0
        self._paused_note = False
        self.result = ""
        self.ready = threading.Event()
        self.done = threading.Event()

    @property
    def typed(self) -> str:
        return self.base

    def add(self, delta: str) -> None:
        if self.live:
            self.q.put(("type", delta))

    def finish(self, final_text: str | None) -> None:
        self.q.put(("finish", final_text))

    def cancel(self) -> None:
        self.q.put(("cancel", None))

    # ------------------------------------------------------------------ main loop
    def _init_uia(self) -> None:
        # terminals expose their whole screen buffer through UI Automation, but input only goes
        # to the prompt: selecting a range there does nothing, so they use plain keystrokes
        if not self.cfg.use_uia or self.terminal:
            return
        try:
            from livetype.uia import Anchor, Uia

            self.uia = Uia()
            # Chromium/Electron build their accessibility tree only after the first UIA request,
            # so the text field may appear a moment later
            field_ = None
            for _ in range(8):
                field_ = self.uia.text_field()
                if field_ is not None:
                    break
                time.sleep(0.12)
            if field_ is not None:
                self.anchor = Anchor(field_)
                self.mode = "uia"
        except Exception:
            log.exception("UI Automation is not available")

    def run(self) -> None:
        self._init_uia()
        self.ready.set()
        pending, final, finishing, deadline = "", None, False, 0.0
        while True:
            try:
                kind, arg = self.q.get(timeout=0.04)
                if kind == "type":
                    pending += arg or ""
                elif kind == "cancel":
                    self.result = "cancelled"
                    self.done.set()
                    return
                else:
                    finishing, final, deadline = True, arg, time.monotonic() + 8.0
            except queue.Empty:
                pass
            if pending and self._ready():
                self._type(pending)
                pending = ""
            if finishing and (not pending or time.monotonic() > deadline):
                try:
                    self.result = self._final(final, pending)
                except Exception:
                    log.exception("final fix failed")
                    self.result = "error"
                self.done.set()
                return

    def _ready(self) -> bool:
        if win.foreground() != self.target:
            if not self._paused_note:
                self._paused_note = True
                self.ui.notice("Пауза: открыто другое окно", "вернитесь в окно, где начали диктовку", kind="warning")
            return False
        if self._paused_note:
            self._paused_note = False
            self.restore_ui()
        w = self.watcher
        if w is not None and w.count != self._seen:
            if time.monotonic() - w.last_time < self.cfg.user_idle_s:
                return False  # you are typing/clicking right now
            self._seen = w.count
            self.user_edits += 1
            self._user_moved = True
            if self.mode == "keys":
                self.edited = True
                if self.cfg.after_user_edit == "end":
                    if not win.wait_modifiers_released(1.0):
                        return False
                    # in cmd and PowerShell Ctrl+End deletes to the end of the line
                    win.press(win.VK_END, ctrl=not self.terminal)
        return not win.modifiers_down()

    # ------------------------------------------------------------------ typing
    def _type(self, text: str) -> None:
        if self.mode == "lost":
            self.lost += text
            return
        if self.mode == "uia":
            try:
                if self._type_uia(text):
                    return
            except Exception:
                log.exception("UIA typing failed")
            # the user moved the cursor and our text is no longer found: keystrokes now would
            # land wherever the cursor is, so the rest of the dictation goes to the clipboard
            self.mode = "lost"
            self.lost += text
            self.ui.notice("Не вижу, куда дописывать", "весь текст будет в буфере обмена", kind="warning")
            return
        win.type_text(text)
        self.base += text

    def _type_uia(self, text: str) -> bool:
        from livetype.uia import Caret

        a = self.anchor
        if not self._user_moved and self.at_end:
            win.type_text(text)  # cursor is still right after our text
            a.text += text
            self.base += text
            return True
        self._user_moved = False
        end = a.end()
        if end is None:
            log.info("our text is no longer found in the field")
            return False
        user_field = self.uia.text_field()
        same = user_field is not None and user_field.same_as(a.field.el)
        caret = Caret(user_field) if user_field is not None else None
        at_our_end = bool(caret and same and caret.at(end))
        was_after = bool(caret and same and caret.after_point(end))
        if not a.field.has_focus():
            a.field.focus()
        end.Select()
        win.type_text(text)
        a.text += text
        self.base += text
        self._wait_visible()
        if caret is not None and not at_our_end:
            if not caret.restore(shifted_by=len(text), was_after_insert=was_after):
                log.info("could not put the user's cursor back")
            self.at_end = False
        else:
            self.at_end = True
        return True

    def _wait_visible(self, timeout: float = 0.6) -> bool:
        """Keystrokes are processed asynchronously: wait until our text shows up in the field."""
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            r = self.anchor.region() if self.anchor else None
            if r is not None:
                try:
                    if r.GetText(-1).endswith(self.anchor.text[-30:]):
                        return True
                except Exception:
                    pass
            time.sleep(0.02)
        return False

    # ------------------------------------------------------------------ final fix
    def _final(self, final: str | None, pending: str) -> str:
        if self.mode == "lost":
            win.set_clipboard(final or (self.base + self.lost + pending))
            return "clipboard_lost"
        if pending:  # could not finish typing: the window never came back
            win.set_clipboard(final or (self.base + pending))
            return "clipboard_unfocused"
        if not self.live:
            return self._insert_once(final or "")
        if not final or not final.strip() or final == self.base:
            return "same"
        if self.mode == "uia" and self.anchor is not None:
            try:
                res = self._final_uia(final)
                if res:
                    return res
            except Exception:
                log.exception("UIA final fix failed")
            if self.user_edits or not self.at_end:
                # the cursor is not after our text any more: arrow keys would edit the wrong place
                win.set_clipboard(final)
                return "clipboard_edited"
        return self._final_keys(final)

    def _insert_once(self, text: str) -> str:
        if not text.strip():
            return "same"
        if win.foreground() != self.target or not win.wait_modifiers_released(3.0):
            win.set_clipboard(text)
            return "clipboard_unfocused"
        win.type_text(text)
        self.base = text
        return "inserted"

    def _final_uia(self, final: str) -> str | None:
        from livetype.uia import CHAR, END, START, Caret, point

        if win.foreground() != self.target or not win.wait_modifiers_released(3.0):
            return None
        a = self.anchor
        region = a.region()
        if region is None:
            return None
        current = region.GetText(-1)
        merged = merge_user_edits(self.base, current, final)
        if len(merged) < 0.5 * len(current):
            return "kept_live"
        if merged == current:
            return "same"
        p, s = common_affixes(current, merged)
        sub = region.Clone()
        sub.MoveEndpointByUnit(START, CHAR, p)
        sub.MoveEndpointByUnit(END, CHAR, -s)
        if sub.GetText(-1) != current[p : len(current) - s]:
            sub, p, s = region, 0, 0
        user_field = self.uia.text_field()
        same = user_field is not None and user_field.same_as(a.field.el)
        caret = Caret(user_field) if user_field is not None else None
        end = point(region, END)
        at_our_end = bool(caret and same and caret.at(end))
        was_after = bool(caret and same and caret.after_point(end))
        if not a.field.has_focus():
            a.field.focus()
        sub.Select()
        insert = merged[p : len(merged) - s]
        if insert:
            win.type_text(insert)
        else:
            win.press(win.VK_BACK)
        user_kept = current != self.base
        a.text = merged
        self.base = merged
        self._wait_visible()
        if caret is not None and not at_our_end:
            caret.restore(shifted_by=len(merged) - len(current), was_after_insert=was_after)
        return "merged" if user_kept else "fixed"

    def _final_keys(self, final: str) -> str:
        if len(final) < 0.5 * len(self.base):  # something went wrong in the final pass
            return "kept_live"
        if self.edited:
            win.set_clipboard(final)
            return "clipboard_edited"
        if self.terminal:
            win.set_clipboard(final)
            return "clipboard_terminal"
        if win.foreground() != self.target or not win.wait_modifiers_released(3.0):
            win.set_clipboard(final)
            return "clipboard_unfocused"
        if self.watcher is not None and self.watcher.count != self._seen:
            win.set_clipboard(final)
            return "clipboard_edited"
        plan = plan_replacement(self.base, final)
        win.press(win.VK_LEFT, plan.left)
        win.press(win.VK_LEFT, plan.select, shift=True)
        if plan.insert:
            win.type_text(plan.insert)
        elif plan.select:
            win.press(win.VK_BACK)
        win.press(win.VK_RIGHT, plan.right)
        self.base = final
        return "fixed"


class Sounds:
    """Fallback cues for the standalone (no Electron UI) mode; the UI plays Type's own sounds."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.dir = DATA_DIR / "sounds"
        if enabled:
            self._make("start.wav", [(620, 0.06), (880, 0.07)])
            self._make("stop.wav", [(880, 0.06), (620, 0.07)])

    def _make(self, name: str, notes: list[tuple[float, float]]) -> None:
        path = self.dir / name
        if path.exists():
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        sr, frames = 22050, bytearray()
        for freq, dur in notes:
            n = int(sr * dur)
            for i in range(n):
                env = min(1.0, i / (0.006 * sr), (n - i) / (0.012 * sr))
                frames += struct.pack("<h", int(0.22 * 32767 * env * math.sin(2 * math.pi * freq * i / sr)))
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(bytes(frames))

    def play(self, name: str) -> None:
        if self.enabled:
            winsound.PlaySound(str(self.dir / name), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)


@dataclass
class SessionReport:
    live_text: str = ""
    final_text: str = ""
    outcome: str = ""
    user_edits: int = 0
    mode: str = ""
    seconds: float = 0.0
    final_pass_s: float = 0.0
    decode_avg_ms: float = 0.0
    commit_log: list[tuple[float, str]] = field(default_factory=list)


def level_01(rms: float) -> float:
    return min(1.0, max(0.0, (20 * math.log10(rms + 1e-9) + 55) / 40))


class App:
    def __init__(self, cfg: Config, ui, source_factory: Callable[[], Source], *,
                 auto: bool = False, once: bool = False, debug: bool = False, play_sounds: bool = True):
        self.cfg, self.ui, self.source_factory = cfg, ui, source_factory
        self.auto, self.once, self.debug = auto, once, debug
        self.cmds: queue.Queue[str] = queue.Queue()
        self.phase = "idle"
        self.last: SessionReport | None = None
        self.text_processor = None  # set when the language-model pass is available
        self.on_text_settings_changed: Callable[[], None] = lambda: None
        self.arm_cancel: Callable[[bool], None] = lambda on: None  # Esc cancels while dictating
        t = time.perf_counter()
        self.live = Recognizer(cfg.live_model, cfg.models_dir, cfg.quantization, cfg.live_threads)
        self.final = Recognizer(cfg.final_model, cfg.models_dir, cfg.quantization, cfg.final_threads)
        self.vad = StreamingVad(cfg.models_dir)
        log.info("models loaded in %.1f s", time.perf_counter() - t)
        self.hotkey_vk = 0
        self.hold_down = False  # hold mode: the hotkey is held right now
        self.sounds = Sounds(cfg.sounds and play_sounds)
        self.thread = threading.Thread(target=self._worker, name="controller", daemon=True)

    def start(self) -> None:
        self.thread.start()

    # hotkey events: toggle mode uses press only; hold mode starts on press, stops on release
    def toggle(self) -> None:
        self.cmds.put("toggle")

    def hotkey_down(self) -> None:
        if self.cfg.activation_mode == "hold":
            self.hold_down = True
            if self.phase == "idle":
                self.cmds.put("toggle")
        else:
            self.cmds.put("toggle")

    def hotkey_up(self) -> None:
        if self.cfg.activation_mode == "hold":
            self.hold_down = False
            # a short press may end before the microphone is even open ("starting")
            if self.phase in ("starting", "listening"):
                self.cmds.put("toggle")

    def cancel(self) -> None:
        self.cmds.put("cancel")

    def quit(self) -> None:
        self.cmds.put("quit")

    def _event(self, msg: str) -> None:
        log.info(msg)
        if self.debug:
            print(msg, flush=True)

    def _worker(self) -> None:
        while True:
            cmd = self.cmds.get()
            if cmd == "quit":
                return
            if cmd != "toggle":
                continue
            try:
                self.phase = "starting"
                self._session()
            except Exception as e:  # keep the app alive
                log.exception("session failed")
                self.ui.notice("Ошибка", str(e)[:120], kind="error")
            self.phase = "idle"
            # presses made while the final check was running must not start a new dictation
            if "quit" in self._drain(self.cmds, 0):
                return
            if self.once:
                time.sleep(1.8)
                self.ui.quit()
                return

    def _restore_ui(self) -> None:
        if self.phase == "listening":
            self.ui.listening()
        elif self.phase == "processing":
            self.ui.processing()

    def _set_level(self, rms: float) -> None:
        self.ui.level = level_01(rms) if getattr(self.ui, "wants_unit_level", False) else rms

    @staticmethod
    def _drain(q: queue.Queue, timeout: float) -> list:
        items = []
        try:
            items.append(q.get(timeout=timeout) if timeout else q.get_nowait())
            while True:
                items.append(q.get_nowait())
        except queue.Empty:
            pass
        return items

    def _session(self) -> None:
        cfg = self.cfg
        target = win.foreground()
        if not target or win.is_own_window(target):
            self.ui.notice("Куда печатать?", "кликните в поле для текста и нажмите клавишу снова", kind="warning")
            self._event("EVENT refused target=self")
            return
        watcher = typer = engine = source = None
        ended = False  # the capsule was put back to idle (done/hide) by the normal path
        try:
            watcher = win.InputWatcher({self.hotkey_vk} if self.hotkey_vk else set())
            watcher.start()
            watcher.ready.wait(1.0)
            typer = Typer(cfg, target, watcher, self.ui, self._restore_ui, live=cfg.live_typing)
            typer.start()
            typer.ready.wait(2.0)
            self._event(f"EVENT start target={win.window_process(target)} mode={typer.mode}")
            engine = DictationEngine(cfg, self.live, self.vad, self.final,
                                     on_commit=typer.add,
                                     on_level=self._set_level)
            source = self.source_factory()
            self.phase = "listening"
            self.ui.listening()
            self.sounds.play("start.wav")
            if cfg.activation_mode == "hold" and not self.hold_down and not self.auto:
                self.cmds.put("toggle")  # released while we were starting
            t0 = time.monotonic()
            cancelled = False
            source.start()
            self.arm_cancel(True)
            try:
                while True:
                    cmds = self._drain(self.cmds, 0) if not self.cmds.empty() else []
                    if "quit" in cmds:
                        self.cmds.put("quit")
                        break
                    if "cancel" in cmds:
                        cancelled = True
                        break
                    if "toggle" in cmds:
                        break
                    for chunk in self._drain(source.q, 0.03):
                        engine.feed(chunk)
                    engine.poll()
                    if self.auto and source.finished.is_set() and source.q.empty():
                        break
            finally:
                source.stop()
                self.arm_cancel(False)
            if cancelled:
                typer.cancel()
                self.ui.hide()
                ended = True
                self._event("EVENT cancelled")
                return
            for chunk in self._drain(source.q, 0):
                engine.feed(chunk)
            self.phase = "processing"
            self.ui.processing()
            self.sounds.play("stop.wav")
            self._event("EVENT stop")
            t1 = time.monotonic()
            final = engine.finish()  # without the final check this is the live text
            if cfg.text_processing and self.text_processor is not None and final.strip():
                try:
                    final = self.text_processor.process(final, cfg.text_rules)
                except Exception:
                    log.exception("text processing failed")
            final_s = time.monotonic() - t1
            typer.finish(final)
            typer.done.wait(20)
            outcome = typer.result or "timeout"
            changes = count_changes(engine.live_text, final)
            if outcome in ("fixed", "merged"):
                sub = f"исправлено мест: {changes}" + (" · ваши правки сохранены" if outcome == "merged" else "")
                self.ui.done("Текст проверен", sub)
            elif outcome in ("same", "kept_live", "inserted"):
                self.ui.done("Готово", "ошибок не найдено" if outcome == "same" else "")
            elif outcome.startswith("clipboard"):
                why = {"clipboard_edited": "вы правили текст во время диктовки",
                       "clipboard_terminal": "в терминале правка на месте отключена",
                       "clipboard_lost": "не нашёл в поле продиктованный текст",
                       "clipboard_unfocused": "окно было неактивно"}.get(outcome, "")
                self.ui.notice("Исправленный текст — в буфере (Ctrl+V)", why, kind="success")
                self.ui.finish()
            else:
                self.ui.notice("Не удалось применить правку", outcome, kind="warning")
                self.ui.finish()
            ended = True
            dec = engine.decode_times
            self.last = SessionReport(
                live_text=engine.live_text, final_text=final, outcome=outcome, user_edits=typer.user_edits,
                mode=typer.mode, seconds=time.monotonic() - t0, final_pass_s=final_s,
                decode_avg_ms=1000 * sum(dec) / len(dec) if dec else 0.0, commit_log=list(engine.commit_log),
            )
            self._save_history(final or engine.live_text)
            self._event(
                f"EVENT done outcome={outcome} mode={typer.mode} edits={typer.user_edits} audio={engine.audio_time:.1f}s "
                f"final_pass={final_s:.2f}s decode_avg={self.last.decode_avg_ms:.0f}ms changes={changes}"
            )
            if self.debug:
                print("LIVE: " + engine.live_text, flush=True)
                print("FINAL: " + final, flush=True)
        finally:
            # whatever happened (a microphone error, a crash in the engine), leave nothing running
            # and never leave the capsule stuck on screen
            if source is not None:
                source.stop()
            self.arm_cancel(False)
            if typer is not None and not typer.done.is_set():
                typer.cancel()
            if watcher is not None and watcher.is_alive():
                watcher.stop()
            if engine is not None:
                engine.close()
            if not ended:
                self.ui.finish()

    @staticmethod
    def _save_history(text: str, keep: int = HISTORY_KEEP) -> None:
        """History lives only on this computer and keeps the last `keep` dictations."""
        if not text.strip():
            return
        try:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with HISTORY_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "text": text}, ensure_ascii=False) + "\n")
            lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
            if len(lines) > keep + 20:
                tmp = HISTORY_PATH.with_suffix(".tmp")
                tmp.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
                os.replace(tmp, HISTORY_PATH)
        except OSError:
            log.exception("history write failed")
