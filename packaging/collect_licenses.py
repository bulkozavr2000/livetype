"""Build step: copy the license texts of everything the installer ships into one folder.

Usage: python packaging/collect_licenses.py <out dir>   (run with the Python that builds the engine)

The folder ends up in the installed app as resources/licenses: LiveType's own LICENSE and
THIRD_PARTY_NOTICES.md, Python's license (with the libraries of the Windows build: OpenSSL,
libffi, zlib, …) and the license files of every installed Python package except build and test
tools.
"""

from __future__ import annotations

import shutil
import sys
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUILD_ONLY = {  # not shipped: build, test and standalone-mode-only packages
    "altgraph", "pefile", "pyinstaller", "pyinstaller-hooks-contrib", "pywin32-ctypes", "pytest", "pluggy",
    "iniconfig", "pygments", "colorama", "pillow", "pystray", "six", "setuptools", "pip", "wheel",
}
MARKERS = ("LICEN", "COPYING", "NOTICE")


def main(out: Path) -> int:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copy2(REPO / "LICENSE", out / "LICENSE.txt")
    shutil.copy2(REPO / "THIRD_PARTY_NOTICES.md", out / "THIRD_PARTY_NOTICES.md")
    py = Path(sys.base_prefix) / "LICENSE.txt"
    if py.is_file():
        (out / "python").mkdir()
        shutil.copy2(py, out / "python" / "LICENSE.txt")
    count = 0
    for dist in metadata.distributions():
        name = (dist.metadata["Name"] or "").strip()
        if not name or name.lower() in BUILD_ONLY:
            continue
        files = [f for f in (dist.files or []) if any(m in f.name.upper() for m in MARKERS)]
        dst_dir = out / name
        for f in files:
            src = Path(str(f.locate()))
            if not src.is_file():
                continue
            # keep paths unique, e.g. numpy-…/licenses/numpy/_core/…/LICENSE.txt
            rel = "_".join(Path(str(f)).parts[1:]) if len(Path(str(f)).parts) > 1 else f.name
            dst_dir.mkdir(exist_ok=True)
            shutil.copy2(src, dst_dir / rel)
            count += 1
        if not files:  # no license file in the wheel: keep what the metadata says
            text = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
            if text.strip():
                dst_dir.mkdir(exist_ok=True)
                (dst_dir / "LICENSE-from-metadata.txt").write_text(f"{name}: {text.strip()}\n", encoding="utf-8")
                count += 1
        if name.lower() == "sounddevice":  # the PortAudio DLL's license is in its README
            readme = Path(str(dist.locate_file("_sounddevice_data/portaudio-binaries/README.md")))
            if readme.is_file():
                dst_dir.mkdir(exist_ok=True)
                shutil.copy2(readme, dst_dir / "portaudio-README.md")
                count += 1
    print(f"{count} license files -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
