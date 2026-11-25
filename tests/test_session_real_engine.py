"""App._session with the real engine and models (today's engine contract: on_commit only and
finish() without a budget), a WAV as the microphone and a simulated field. No desktop, no real
keystrokes: livetype.win's input functions are replaced by typer_sim.FakeWin."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import pytest
from typer_sim import FakeUi, FakeUia, FakeWatcher, FakeWin, Field

from livetype import app as app_mod
from livetype import config, win
from livetype.app import App, Typer
from livetype.asr import Recognizer, StreamingVad
from livetype.audio import load_wav
from livetype.textops import one_line, wer
from livetype.uia import Anchor

DATA = Path(__file__).parent / "data"
SPEED = 3.0  # the WAV is played 3x faster than real time


class FastSource:
    """The WAV in 32 ms blocks, SPEED times faster than the microphone would give them."""

    def __init__(self, path: Path):
        self.audio = load_wav(path)
        self.q: queue.Queue = queue.Queue()
        self.finished = threading.Event()
        self._stop = threading.Event()

    def start(self) -> None:
        def run():
            t0 = time.perf_counter()
            for i in range(0, len(self.audio), 512):
                if self._stop.is_set():
                    break
                delay = t0 + i / 16000 / SPEED - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                self.q.put(self.audio[i : i + 512])
            self.finished.set()

        threading.Thread(target=run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


@pytest.fixture(scope="module")
def models():
    cfg = config.Config()
    return (Recognizer(cfg.live_model, cfg.models_dir, cfg.quantization, cfg.live_threads),
            Recognizer(cfg.final_model, cfg.models_dir, cfg.quantization, cfg.final_threads),
            StreamingVad(cfg.models_dir))


@pytest.mark.parametrize("use_uia", [True, False])
def test_real_engine_dictation_ends_as_the_final_text(models, monkeypatch, tmp_path, use_uia):
    wav = DATA / "ru_sample.wav"
    if not wav.exists():
        pytest.skip("tests/data/ru_sample.wav is missing")
    cfg = config.Config()
    cfg.use_uia = use_uia
    field = Field("Заметка: ")
    fw = FakeWin(monkeypatch, field)
    typers: list[Typer] = []
    monkeypatch.setattr(win, "InputWatcher", FakeWatcher)
    monkeypatch.setattr(app_mod, "HISTORY_PATH", tmp_path / "history.jsonl")
    orig_init = Typer.__init__

    def init(self, *a, **k):
        orig_init(self, *a, **k)
        typers.append(self)

    monkeypatch.setattr(Typer, "__init__", init)
    if use_uia:
        def init_uia(self):
            self.uia = FakeUia(field)
            self.anchor = Anchor(self.uia.field)
            self.mode = "uia"

        monkeypatch.setattr(Typer, "_init_uia", init_uia)
    a = App.__new__(App)
    a.cfg, a.ui = cfg, FakeUi()
    a.source_factory = lambda: FastSource(wav)
    a.auto, a.once, a.debug = True, False, False
    a.cmds, a.phase, a.last = queue.Queue(), "idle", None
    a.text_processor, a.polish_job = None, None
    a.on_text_settings_changed, a.arm_cancel = (lambda: None), (lambda on: None)
    a.live, a.final, a.vad = models
    a.hotkey_vk, a.hold_down = 0, False
    a.sounds = app_mod.Sounds(False)

    a._session()

    r, typer = a.last, typers[0]
    ref = (DATA / "ru_sample.txt").read_text(encoding="utf-8").replace("|", " ").strip()
    print(f"\nmode={typer.mode} outcome={r.outcome}/{r.detail} revisions={r.revisions_applied}/"
          f"{r.revisions_skipped} stop_to_done={r.stop_to_done_s:.2f}s final_pass={r.final_pass_s:.2f}s "
          f"skip={typer.skip_reason!r}")
    assert fw.returns == 0 and fw.clipboard == "the user's own clipboard"
    assert field.text.startswith("Заметка: ")
    if use_uia or r.outcome == "done":
        assert r.outcome == "done"
        assert field.text == "Заметка: " + one_line(r.final_text)
        assert wer(ref, field.text[len("Заметка: "):]) <= 0.05
    else:
        # today's engine corrects the whole text at stop: over 250 characters from the first
        # change, Backspace is not used and the live text stays as it was typed
        assert r.outcome == "partial" and "too long" in typer.skip_reason
        assert field.text == "Заметка: " + one_line(r.live_text)
    assert r.stop_to_done_s < 6.0  # the last segment, then the whole recording once more (~10% of its length)
