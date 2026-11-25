"""Type's own speech recognizer, used when Type is installed on this computer.

Type keeps its recognizer in a native Node module (resources/bin/gigatype_runtime.node) and its
speech model in resources/gigatype-model. We run exactly that module with exactly that model, the
same way Type's own worker does (open with features ["llm_off"], startRecording, pushAudio,
stopRecording, the text arrives in onAsrText), in a small Node process: Electron's own binary with
ELECTRON_RUN_AS_NODE=1. The module needs only Windows' own DLLs.

LiveType keeps its own copy of these two things in <data folder>	ype-runtimeesources (made once,
from Type's installation, on the first start that finds Type), so it keeps working after Type is
removed. Type's own files are never changed.

TypeAsr.text(audio) -> the text Type would type for that audio.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from livetype.config import DATA_DIR, SAMPLE_RATE

log = logging.getLogger("livetype")

MIN_SAMPLES = int(0.2 * SAMPLE_RATE)
RELEASE_ID = "type.v1.019.20260917"  # Type's model release (gigatype-model/meta.json)

# the Node side: the same calls as Type's workers/gigatypeWorker.js, one request at a time
SIDECAR_JS = r"""
const [addonPath, modelRoot] = process.argv.slice(2);
const out = (o) => process.stdout.write("@@" + JSON.stringify(o) + "\n");
let rt = null, onAsr = null, onTxt = null;
const timeout = (p, ms) => Promise.race([p, new Promise((_, no) => setTimeout(() => no(new Error("timeout")), ms))]);
async function handle(line) {
  let req;
  try { req = JSON.parse(line); } catch { return; }
  const t0 = performance.now();
  try {
    const buf = Buffer.from(req.pcm, "base64");
    const samples = new Float32Array(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length));
    const asr = new Promise((r) => (onAsr = r));
    const txt = new Promise((r) => (onTxt = r));
    if (!rt.startRecording()) throw new Error("busy");
    for (let i = 0; i < samples.length; i += 16000) rt.pushAudio(samples.subarray(i, i + 16000));
    rt.stopRecording();
    const text = await timeout(asr, 30000 + samples.length / 16);
    await timeout(txt, 2000).catch(() => {});
    out({ id: req.id, text, ms: Math.round(performance.now() - t0) });
  } catch (e) {
    try { if (rt.isRecording()) rt.cancelRecording(); } catch {}
    out({ id: req.id, error: String((e && e.message) || e) });
  }
}
(async () => {
  try {
    rt = await require(addonPath).open({
      modelRoot, features: ["llm_off"],
      onAsrText: (t) => { const f = onAsr; onAsr = null; if (f) f(t); },
      onText: (t) => { const f = onTxt; onTxt = null; if (f) f(t); },
    });
    out({ ready: true, features: rt.features() });
  } catch (e) {
    out({ ready: false, error: String((e && e.message) || e) });
    process.exit(1);
  }
  let chain = Promise.resolve();
  const rl = require("readline").createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on("line", (line) => { chain = chain.then(() => handle(line)); });
  rl.on("close", async () => { await chain; try { await rt.close(); } catch {} process.exit(0); });
})();
"""


LOCAL = DATA_DIR / "type-runtime" / "resources"  # LiveType's own copy
ADDON = Path("bin") / "gigatype_runtime.node"
MODEL = Path("gigatype-model")


def _complete(base: Path) -> bool:
    return (base / ADDON).is_file() and (base / MODEL / "asr").is_dir() and (base / MODEL / "core.tar").is_file()


def installed_type() -> Path | None:
    """Type's resources folder if Type is installed."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Type" / "resources"
    return base if _complete(base) else None


def ensure_local_copy() -> bool:
    """Copy Type's recognizer module and model into LiveType's data folder once. True if the copy is there."""
    if _complete(LOCAL):
        return True
    src = installed_type()
    if src is None:
        return False
    import shutil
    tmp = LOCAL.with_name("resources.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / ADDON.parent).mkdir(parents=True)
    shutil.copy2(src / ADDON, tmp / ADDON)
    shutil.copytree(src / MODEL, tmp / MODEL)
    shutil.rmtree(LOCAL, ignore_errors=True)
    tmp.rename(LOCAL)
    log.info("copied Type's recognizer and model to %s", LOCAL)
    return _complete(LOCAL)


def type_dir() -> Path | None:
    """Where the recognizer module and model are: LIVETYPE_TYPE_RESOURCES, LiveType's own copy,
    else Type's installation."""
    env = os.environ.get("LIVETYPE_TYPE_RESOURCES")
    if env:
        return Path(env) if _complete(Path(env)) else None
    if _complete(LOCAL):
        return LOCAL
    return installed_type()


def node_exe() -> Path | None:
    """A binary that runs Node: LIVETYPE_NODE (the UI passes its own Electron binary), else the
    development Electron."""
    for cand in (os.environ.get("LIVETYPE_NODE"), os.environ.get("LIVETYPE_ELECTRON"),
                 str(DATA_DIR / "electron" / "node_modules" / "electron" / "dist" / "electron.exe")):
        if cand and Path(cand).is_file():
            return Path(cand)
    return None


def available() -> bool:
    return type_dir() is not None and node_exe() is not None


def _write_if_changed(path: Path, text: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TypeAsr:
    """Type's recognizer in a Node process. text() is thread-safe (one request at a time)."""

    def __init__(self, timeout_s: float = 90.0):
        try:
            ensure_local_copy()
        except OSError:
            log.exception("could not copy Type's recognizer, using it from Type's folder")
        res, node = type_dir(), node_exe()
        if res is None or node is None:
            raise RuntimeError("no copy of Type's recognizer" if res is None else "no Node runtime")
        root = DATA_DIR / "type-runtime"
        model = res / MODEL
        meta = {"release_id": RELEASE_ID, "core": str(model / "core.tar"),
                "parts": {"asr": str(model / "asr"), "llm_passthrough": str(model / "llm_passthrough")}}
        _write_if_changed(root / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        script = root / "type_asr.js"
        _write_if_changed(script, SIDECAR_JS)
        env = {**os.environ, "ELECTRON_RUN_AS_NODE": "1"}
        self._proc = subprocess.Popen(
            [str(node), str(script), str(res / ADDON), str(root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._lock = threading.Lock()
        self._replies: queue.Queue[dict] = queue.Queue()
        self._next = 0
        threading.Thread(target=self._read_out, name="type-asr-out", daemon=True).start()
        threading.Thread(target=self._read_err, name="type-asr-err", daemon=True).start()
        try:
            first = self._replies.get(timeout=timeout_s)
        except queue.Empty:
            self.close()
            raise RuntimeError("Type's recognizer did not start") from None
        if not first.get("ready"):
            self.close()
            raise RuntimeError(f"Type's recognizer failed: {first.get('error')}")
        self.decode_ms: list[int] = []
        log.info("Type recognizer ready (%s)", res)

    def _read_out(self) -> None:
        for raw in self._proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("@@"):
                try:
                    self._replies.put(json.loads(line[2:]))
                except ValueError:
                    pass
        self._replies.put({"closed": True})

    def _read_err(self) -> None:
        for raw in self._proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                log.info("type runtime: %s", line[:300])

    def text(self, audio: np.ndarray) -> str:
        if len(audio) < MIN_SAMPLES:
            return ""
        pcm = base64.b64encode(np.ascontiguousarray(audio, dtype="<f4").tobytes()).decode("ascii")
        with self._lock:
            self._next += 1
            rid = self._next
            self._proc.stdin.write((json.dumps({"id": rid, "pcm": pcm}) + "\n").encode("ascii"))
            self._proc.stdin.flush()
            deadline = time.monotonic() + 60 + len(audio) / SAMPLE_RATE
            while True:
                try:
                    r = self._replies.get(timeout=max(0.1, deadline - time.monotonic()))
                except queue.Empty:
                    raise RuntimeError("Type's recognizer did not answer") from None
                if r.get("closed"):
                    raise RuntimeError("Type's recognizer stopped")
                if r.get("id") == rid:
                    break
        if "error" in r:
            raise RuntimeError(f"Type's recognizer: {r['error']}")
        self.decode_ms.append(int(r.get("ms", 0)))
        return str(r.get("text") or "").strip()

    def close(self) -> None:
        p = getattr(self, "_proc", None)
        if p is None or p.poll() is not None:
            return
        try:
            p.stdin.close()
            p.wait(5)
        except Exception:
            p.kill()
