"""Download the speech models once (~455 MB); after that LiveType works offline.

The app does this by itself on first start. Models go to <data folder>\\models
(LIVETYPE_HOME > %APPDATA%\\LiveType\\home.txt > %LOCALAPPDATA%\\LiveType).

GigaAM v3 e2e (Sber, MIT): CTC (live pass) + RNNT (final check); Silero VAD (MIT).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from livetype.asr import ensure_models, models_missing  # noqa: E402
from livetype.config import Config  # noqa: E402


def main() -> int:
    models = Config().models_dir
    if not models_missing(models):
        print(f"models already in {models}")
        return 0
    ensure_models(models, lambda d, t: print(f"\r{d:6.1f} / {t:.0f} MB", end="", flush=True))
    print(f"\ndone: {models}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
