"""Entry point.

python -m livetype --ipc     engine for the Electron UI (ui/), JSON lines on stdin/stdout
python -m livetype           standalone mode with the simple Tk indicator (and for tests)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading

from livetype import __version__, win
from livetype.config import CONFIG_PATH, DATA_DIR, Config


def pretty_hotkey(spec: str) -> str:
    return "+".join(p if any(c.isupper() for c in p) else (p.capitalize() if len(p) > 1 else p.upper())
                    for p in spec.split("+"))


def setup_logging(console: bool) -> None:
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(DATA_DIR / "logs" / "livetype.log", encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def attach_text_processor(app, cfg: Config) -> None:
    """Language-model pass (Обработка текста): start llama-server when it is switched on."""
    from livetype import llm

    app.text_processor = llm.TextProcessor()

    def apply() -> None:
        if cfg.text_processing and app.text_processor.ready(cfg.text_model):
            threading.Thread(target=app.text_processor.start, args=(cfg.text_model,), daemon=True).start()
        elif not cfg.text_processing:
            app.text_processor.stop()

    app.on_text_settings_changed = apply
    apply()


def run_ipc(args: argparse.Namespace) -> int:
    from livetype import ipc, llm
    from livetype.app import App
    from livetype.audio import FileSource, MicSource
    from livetype.hotkeys import Hotkeys, main_vks

    # the UI talks UTF-8; a packaged exe would otherwise use the Windows code page (garbled Cyrillic)
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    setup_logging(console=False)
    cfg = Config.load()
    ui = ipc.IpcUi()

    from livetype import asr

    def speech_progress(d: float, t: float) -> None:
        ui.emit("model_progress", model="speech", downloaded_mb=round(d, 1), total_mb=round(t, 1), done=False)

    def speech_done(error: str | None = None) -> None:
        ui.emit("model_progress", model="speech", downloaded_mb=asr.DOWNLOAD_MB, total_mb=asr.DOWNLOAD_MB,
                done=True, **({"error": error} if error else {}))

    if asr.models_missing(cfg.models_dir):  # first start: download the speech models once
        try:
            asr.ensure_models(cfg.models_dir, speech_progress)
            speech_done()
        except Exception as e:
            logging.exception("speech model download failed")
            speech_done(str(e))
            ui.emit("error", message=f"Не удалось скачать модель распознавания: {e}")
            return 1

    def source():
        return FileSource(args.source, realtime=True) if args.source else MicSource(cfg.microphone or None)

    try:
        app = App(cfg, ui, source, debug=False, play_sounds=False)
    except Exception:
        # a damaged model file (e.g. the disk filled up during an old download): fetch it again once
        logging.exception("speech models failed to load; checking the files")
        if not asr.repair_models(cfg.models_dir, speech_progress):
            raise
        speech_done()
        app = App(cfg, ui, source, debug=False, play_sounds=False)
    attach_text_processor(app, cfg)
    hotkeys = Hotkeys(cfg.hotkey, app.hotkey_down, app.hotkey_up)
    hotkeys.on_esc, app.arm_cancel = app.cancel, hotkeys.arm_esc
    hotkeys.ready.wait(3)
    app.hotkey_vk = next(iter(main_vks(cfg.hotkey)), 0)
    app.start()

    def download_model(model: str) -> None:
        def work() -> None:
            try:
                llm.download(model, lambda d, t: ui.emit("model_progress", model=model, downloaded_mb=round(d, 1),
                                                         total_mb=round(t, 1), done=False))
                ui.emit("model_progress", model=model, downloaded_mb=llm.MODELS[model]["size_mb"],
                        total_mb=llm.MODELS[model]["size_mb"], done=True)
                app.on_text_settings_changed()
            except Exception as e:
                logging.exception("model download failed")
                ui.emit("model_progress", model=model, downloaded_mb=0, total_mb=0, done=True, error=str(e))

        threading.Thread(target=work, name="download", daemon=True).start()

    try:
        ipc.run(cfg, app, hotkeys, llm.status, download_model)
    finally:
        app.quit()
        hotkeys.stop()
        if app.text_processor:
            app.text_processor.stop()
    return 0


def start_tray(overlay, cfg: Config):
    import pystray
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((22, 6, 42, 38), radius=10, fill=(255, 69, 58))
    d.arc((14, 18, 50, 48), 0, 180, fill=(235, 235, 240), width=4)
    d.line((32, 48, 32, 56), fill=(235, 235, 240), width=4)
    d.line((22, 57, 42, 57), fill=(235, 235, 240), width=4)

    def open_settings() -> None:
        if not CONFIG_PATH.exists():
            cfg.save()
        os.startfile(CONFIG_PATH)  # noqa: S606

    def quit_app(icon) -> None:  # noqa: ANN001
        icon.stop()
        overlay.quit()

    menu = pystray.Menu(
        pystray.MenuItem(f"Диктовка: {pretty_hotkey(cfg.hotkey)}", None, enabled=False),
        pystray.MenuItem("Настройки (config.json)", lambda: open_settings()),
        pystray.MenuItem("Выход", quit_app),
    )
    icon = pystray.Icon("LiveType", img, f"LiveType {__version__} — офлайн-диктовка", menu)
    threading.Thread(target=icon.run, name="tray", daemon=True).start()
    return icon


def run_standalone(args: argparse.Namespace) -> int:
    from livetype.app import App
    from livetype.audio import FileSource, MicSource
    from livetype.hotkeys import Hotkeys, main_vks
    from livetype.overlay import GREEN, RED, Overlay

    setup_logging(console=args.debug)
    cfg = Config.load()
    overlay = Overlay(hotkey_label=pretty_hotkey(cfg.hotkey))

    def source():
        return FileSource(args.source, realtime=True) if args.source else MicSource(cfg.microphone or None)

    app = App(cfg, overlay, source, auto=args.auto, once=args.once, debug=args.debug)
    attach_text_processor(app, cfg)
    hotkeys = Hotkeys(cfg.hotkey, app.hotkey_down, app.hotkey_up)
    hotkeys.on_esc, app.arm_cancel = app.cancel, hotkeys.arm_esc
    hotkeys.ready.wait(3)
    app.hotkey_vk = next(iter(main_vks(cfg.hotkey)), 0)
    app.start()
    tray = None if args.no_tray else start_tray(overlay, cfg)

    if not hotkeys.ok:
        overlay.notice("Горячая клавиша не работает", hotkeys.error or cfg.hotkey, color=RED, ttl=8)
    elif args.auto:
        app.toggle()
    else:
        overlay.notice("LiveType готов", f"{pretty_hotkey(cfg.hotkey)} — начать и закончить диктовку",
                       color=GREEN, ttl=2.5, state="done")
    if args.debug:
        print(f"READY hotkey_ok={hotkeys.ok}", flush=True)

    overlay.run()  # until "Выход" in the tray (or --once finished)
    hotkeys.stop()
    app.quit()
    if app.text_processor:
        app.text_processor.stop()
    if tray is not None:
        tray.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--keyhook"]:  # packaged app: the same exe doubles as the hotkey listener
        from livetype import keyhook

        keyhook.main(argv[1] if len(argv) > 1 else None)
        return 0
    p = argparse.ArgumentParser(prog="livetype", description="Офлайн-диктовка: слова печатаются прямо во время речи")
    p.add_argument("--ipc", action="store_true", help="движок для интерфейса (ui/): JSON по stdin/stdout")
    p.add_argument("--source", help="WAV-файл вместо микрофона (для проверки)")
    p.add_argument("--auto", action="store_true", help="сразу начать диктовку и закончить по концу файла")
    p.add_argument("--once", action="store_true", help="выйти после одной диктовки")
    p.add_argument("--debug", action="store_true", help="печатать события и тексты в консоль")
    p.add_argument("--no-tray", action="store_true", help="без значка в трее")
    args = p.parse_args(argv)
    if getattr(sys, "frozen", False) and not args.ipc:  # the installed engine only serves the UI
        print("Это движок LiveType. Запускайте LiveType.exe", file=sys.stderr)
        return 2

    win.set_dpi_aware()
    instance = win.SingleInstance()
    if instance.already_running:
        print("LiveType уже запущен", file=sys.stderr)
        return 1
    return run_ipc(args) if args.ipc else run_standalone(args)


if __name__ == "__main__":
    sys.exit(main())
