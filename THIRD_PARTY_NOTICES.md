# Third-party notices

LiveType includes or downloads the following third-party work. Thank you to their authors.

## Included in the source / installer

| Component | Used for | License |
|---|---|---|
| **Type / GigaType2** (gigatype.app) — Copyright (c) 2026 Disrupt Builders | dictation capsule renderer (`ui/overlay/capsule.js`), dictation sounds (`ui/assets/sounds`), control-panel layout, text-processing presets | MIT |
| **OpenWhispr** (github.com/OpenWhispr/openwhispr) — Copyright (c) 2024 OpenWhispr Team | upstream of Type (window configuration, cleanup prompt wording) | MIT |
| **Electron** (with Chromium and its components) | desktop shell | MIT; Chromium's licenses are installed as `LICENSES.chromium.html` |
| **Inter** typeface (Rasmus Andersson) | UI font (`ui/assets/fonts`) | SIL Open Font License 1.1 |
| **Python 3.12** (Python Software Foundation) with the libraries of its Windows build: OpenSSL 3, libffi, bzip2, xz, zlib, SQLite, expat, Microsoft Visual C++ runtime | the engine's interpreter (bundled by PyInstaller) | PSF License; OpenSSL: Apache-2.0; others: see `licenses/python/LICENSE.txt` |
| **onnx-asr** (Ilya Stupakov) | speech model runtime wrapper | MIT |
| **ONNX Runtime** (Microsoft) and the components listed in its ThirdPartyNotices | neural network inference | MIT; see `licenses/onnxruntime/` |
| **NumPy** with the bundled **OpenBLAS** and GCC runtime | audio arrays | BSD-3-Clause; OpenBLAS: BSD-3-Clause; GCC runtime: GPL-3.0 with the GCC Runtime Library Exception |
| **sounddevice** with **PortAudio** | microphone input | MIT; PortAudio: MIT-style (see `licenses/sounddevice/portaudio-README.md`) |
| **comtypes**, **cffi**, **pycparser**, **flatbuffers**, **protobuf**, **packaging** | UI Automation, libraries used by the above | MIT / MIT / BSD-3-Clause / Apache-2.0 / BSD-3-Clause / Apache-2.0 or BSD-2-Clause |
| **PyInstaller** bootloader | packaging the engine | GPL-2.0 with the bootloader exception |

The installer puts every license text in `resources\licenses` inside the installation folder
(built by `packaging/collect_licenses.py`). **Pillow** and **pystray** (MIT-CMU, LGPL-3.0) are used
only by the developer-mode tray icon (`python -m livetype`) and are not part of the installer.

## Downloaded on first use (not bundled)

| Component | Used for | License |
|---|---|---|
| **GigaAM v3** (Sber, salute-developers) — ONNX export by onnx-asr (istupakov/gigaam-v3-onnx) | speech recognition (live CTC + final RNNT) | MIT |
| **Silero VAD** (Silero Team) — ONNX by onnx-asr (istupakov/silero-vad-onnx) | detecting speech and pauses | MIT |
| **llama.cpp** (ggml-org) | runtime for the optional text-processing model | MIT |
| **Qwen3.5-0.8B-GEC-KAZ-RUS-ENG** (Loqira Labs, base: Qwen) | text processing, "Light" model | Apache-2.0 |
| **Qwen3-4B-Instruct-2507** (Qwen team; GGUF by Unsloth) | text processing, "Accurate" model | Apache-2.0 |

## MIT license text (Type / GigaType2, OpenWhispr)

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

LiveType is not affiliated with Type, GigaType, OpenWhispr, Sber or SberDevices. Their names and logos
belong to their owners and are not used in LiveType's branding.
