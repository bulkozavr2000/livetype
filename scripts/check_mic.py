"""Проверка микрофона: python scripts/check_mic.py [секунды]
Во время проверки скажите что-нибудь. Ничего не записывается на диск — только уровень звука."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

from livetype.audio import MicSource  # noqa: E402


def db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-9)) if len(x) else -180.0


def main(seconds: float) -> int:
    dev = sd.query_devices(kind="input")
    print(f"микрофон: {dev['name']}")
    print("скажите что-нибудь…")
    src = MicSource()
    src.start()
    t0, chunks, window = time.time(), [], []
    while time.time() - t0 < seconds:
        try:
            c = src.q.get(timeout=0.5)
        except Exception:
            continue
        chunks.append(c)
        window.append(c)
        if sum(len(w) for w in window) >= 4000:  # every 0.25 s
            lvl = db(np.concatenate(window))
            bar = "#" * int(max(0, min(40, (lvl + 80) / 2)))
            print(f"{lvl:7.1f} dB |{bar}")
            window = []
    src.stop()
    x = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    loud = max((db(x[i : i + 4000]) for i in range(0, len(x), 4000)), default=-180.0)
    print(f"получено {len(x) / 16000:.1f} с, самый громкий момент {loud:.1f} dB")
    if len(x) < 0.8 * seconds * 16000:
        print("ПРОБЛЕМА: звук не идёт с микрофона")
        return 1
    if loud < -75:
        print("ТИШИНА: микрофон выключен (кнопка на ноутбуке / Параметры → Система → Звук) или вы молчали")
        return 1
    print("МИКРОФОН РАБОТАЕТ" + (" (тихо — говорите ближе)" if loud < -45 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(float(sys.argv[1]) if len(sys.argv) > 1 else 5.0))
