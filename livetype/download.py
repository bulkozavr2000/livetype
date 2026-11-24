"""Downloads of models and the llama.cpp runtime: pinned versions, checked size and SHA-256.

A file only gets its final name after both checks pass, so a dropped connection or a changed
file on the server never leaves a broken model behind.
"""

from __future__ import annotations

import hashlib
import urllib.request
from collections.abc import Callable
from pathlib import Path

from livetype import __version__

CHUNK = 1 << 20


class DownloadError(OSError):
    pass


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 24):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, dst: Path, size: int, sha256: str,
          progress: Callable[[int], None] | None = None, timeout: float = 60) -> None:
    """Download `url` to `dst`; `progress(bytes_so_far)` is called after every chunk."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    h = hashlib.sha256()
    got = 0
    req = urllib.request.Request(url, headers={"User-Agent": f"LiveType/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as out:
            while chunk := r.read(CHUNK):
                out.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if progress:
                    progress(got)
        # urllib returns a short read silently when the connection drops: check the length
        if got != size:
            raise DownloadError(f"{dst.name}: скачано {got} из {size} байт (обрыв связи?)")
        if h.hexdigest() != sha256:
            raise DownloadError(f"{dst.name}: файл повреждён (не совпала контрольная сумма)")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dst)
