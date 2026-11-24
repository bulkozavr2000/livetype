"""Settings. Stored in %APPDATA%\\LiveType\\config.json (the control panel edits them)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "LiveType"
CONFIG_PATH = CONFIG_DIR / "config.json"
HISTORY_PATH = CONFIG_DIR / "history.jsonl"


def _data_dir() -> Path:
    """Where models, logs and the language-model runtime live (they are big).

    LIVETYPE_HOME env var > the folder written in %APPDATA%\\LiveType\\home.txt > %LOCALAPPDATA%\\LiveType.
    """
    env = os.environ.get("LIVETYPE_HOME")
    if env:
        return Path(env)
    pointer = CONFIG_DIR / "home.txt"
    if pointer.is_file():
        text = pointer.read_text(encoding="utf-8").strip()
        if text:
            return Path(text)
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LiveType"


DATA_DIR = _data_dir()

SAMPLE_RATE = 16_000

# the same presets as Type's «Обработка текста» (gigaEditPrompts)
DEFAULT_RULES = [
    {"id": "remove-fillers", "label": "Без «ну» и «короче»", "enabled": True},
    {"id": "lowercase-start", "label": "С маленькой буквы", "enabled": False},
    {"id": "final-period", "label": "Точка в конце", "enabled": False},
    {"id": "shorter", "label": "Писать коротко", "enabled": False},
]

# settings the control panel may change (everything else is advanced, edit config.json)
PANEL_KEYS = (
    "hotkey", "activation_mode", "microphone", "live_typing", "final_check", "text_processing",
    "text_model", "text_rules", "show_capsule", "sounds",
)


@dataclass
class Config:
    # --- control panel (same sections as in Type) ---
    hotkey: str = "ctrl+alt+d"  # also single keys like "RightAlt"; Ctrl+Alt+Space is taken on this PC
    activation_mode: str = "toggle"  # "toggle" (Старт-стоп) | "hold" (Удерживать хоткей)
    microphone: str | None = None  # None = system default, else device name
    live_typing: bool = True  # type words while speaking (our live format)
    final_check: bool = True  # re-recognize the whole recording at the end and fix the text
    text_processing: bool = False  # extra pass by a local language model (Обработка текста)
    text_model: str = "light"  # "light" (Лёгкая) | "accurate" (Точная)
    text_rules: list = field(default_factory=lambda: [dict(r) for r in DEFAULT_RULES])
    show_capsule: bool = True
    sounds: bool = True

    # --- models ---
    models_dir: str = str(DATA_DIR / "models")
    live_model: str = "gigaam-v3-e2e-ctc"  # fast, re-run every ~0.3 s while you speak
    final_model: str = "gigaam-v3-e2e-rnnt"  # more accurate, final check of the whole recording
    quantization: str = "int8"
    live_threads: int = 4
    final_threads: int = 2

    # --- live pass: splitting speech into phrases ---
    vad_threshold: float = 0.5
    vad_neg_threshold: float = 0.35
    live_min_silence_s: float = 0.45  # a pause this long closes a phrase
    decode_interval_s: float = 0.3
    max_live_segment_s: float = 7.0  # longer phrases are cut at a word boundary
    preroll_s: float = 0.25
    tail_pad_s: float = 0.2

    # --- final pass: bigger chunks = more context for words and punctuation ---
    chunk_gap_s: float = 1.2  # pause that closes a chunk
    chunk_soft_max_s: float = 22.0

    # --- typing ---
    use_uia: bool = True  # find our text in the field via UI Automation (keeps words together)
    after_user_edit: str = "end"  # fields without UI Automation: "end" (Ctrl+End) or "caret"
    user_idle_s: float = 0.8  # wait this long after your own typing/clicks before continuing

    @property
    def final_pass(self) -> bool:  # name used by the engine
        return self.final_check

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> Config:
        cfg = cls()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            cfg.update(data)
        return cfg

    def update(self, data: dict) -> None:
        known = {f.name for f in fields(self)}
        for k, v in data.items():
            if k in known:
                setattr(self, k, v)

    def panel(self) -> dict:
        return {k: getattr(self, k) for k in PANEL_KEYS}

    def save(self, path: Path = CONFIG_PATH) -> None:
        data = asdict(self)
        if data.get("models_dir") == str(DATA_DIR / "models"):
            del data["models_dir"]  # the default follows the data folder if it moves
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")  # write-then-rename: a crash never leaves half a file
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
