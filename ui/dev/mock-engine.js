// Mock LiveType engine: speaks the UI protocol over stdio with simulated events, so the UI can be
// developed and checked without Python. Started by main.js with `--mock` (electron.exe in Node mode).
//
// Protocol commands it answers: toggle, cancel, get_settings, set_settings, list_devices,
// capture_hotkey, download_model, get_history, quit.
// Test-only commands (never sent by the real UI): mock_notice {title, body, kind}, mock_state {state},
// mock_tentative {text}, mock_crash.
// Flags: --mock-crash-after=<seconds>  exit with code 3 after that time (restart tests)
//        --mock-ready-delay=<ms>       delay before "ready" (default 700)
//        --mock-processing-ms=<ms>     how long the final check takes (default 2600)
"use strict";
const readline = require("readline");

function flag(name, fallback) {
  const hit = process.argv.find((a) => a.startsWith(`${name}=`));
  return hit ? hit.slice(name.length + 1) : fallback;
}

const READY_DELAY_MS = Number(flag("--mock-ready-delay", 700));
const PROCESSING_MS = Number(flag("--mock-processing-ms", 2600));
const CRASH_AFTER_S = Number(flag("--mock-crash-after", 0));

let settings = {
  hotkey: "RightAlt",
  activation_mode: "toggle",
  microphone: null,
  live_typing: true,
  final_check: true,
  text_processing: true,
  text_model: "light",
  text_rules: [
    { id: "remove-fillers", label: "Без «ну» и «короче»", enabled: true },
    { id: "lowercase-start", label: "С маленькой буквы", enabled: false },
    { id: "final-period", label: "Точка в конце", enabled: false },
    { id: "shorter", label: "Писать коротко", enabled: false },
  ],
  show_capsule: true,
  sounds: true,
};
const devices = [
  { name: "Микрофон (Realtek(R) Audio)", default: true },
  { name: "Гарнитура (AirPods Pro)", default: false },
  { name: "Микрофон (USB Audio Device)", default: false },
];
const models = {
  light: { label: "Лёгкая", installed: true, size_mb: 563 },
  accurate: { label: "Точная", installed: false, size_mb: 2497 },
};
const history = [
  { time: isoMinutesAgo(3), text: "Созвон перенесли на четверг, в 15:00. Захвати, пожалуйста, договор." },
  { time: isoMinutesAgo(47), text: "Купить молоко, хлеб и батарейки для мышки." },
  { time: isoMinutesAgo(60 * 26), text: "Привет! Скинь, пожалуйста, ссылку на вчерашнюю презентацию." },
];
const HOTKEYS = ["ctrl+shift+space", "f9", "RightAlt"];
const PHRASES = [
  "Привет! Это LiveType — слова появляются прямо во время речи,",
  "а в конце вся запись проверяется заново и текст исправляется на месте.",
  "Можно диктовать в любое поле: мессенджер, браузер или документ.",
];

let state = "idle";
let levelTimer = null;
let textTimers = [];
let downloadTimers = {};
let hotkeyIndex = 0;

function isoMinutesAgo(minutes) {
  return new Date(Date.now() - minutes * 60000).toISOString();
}

function emit(obj) {
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}

function prettyHotkey(spec) {
  const names = { rightalt: "Right Alt", rightctrl: "Right Ctrl", rightshift: "Right Shift" };
  if (names[spec.toLowerCase()]) return names[spec.toLowerCase()];
  return spec
    .split("+")
    .map((p) => (p.length > 1 ? p[0].toUpperCase() + p.slice(1) : p.toUpperCase()))
    .join("+");
}

function setState(next, detail) {
  state = next;
  emit(detail ? { event: "state", state: next, detail } : { event: "state", state: next });
}

// --- speech-like microphone level, ~30 Hz ---
function startLevels() {
  stopLevels();
  let phase = "gap";
  let left = 0.15;
  let dur = 0.15;
  let amp = 0;
  let syllablesLeft = 6;
  levelTimer = setInterval(() => {
    const dt = 1 / 30;
    left -= dt;
    if (left <= 0) {
      if (phase === "syllable") {
        syllablesLeft -= 1;
        phase = syllablesLeft <= 0 ? "pause" : "gap";
        dur = phase === "pause" ? 0.35 + Math.random() * 0.4 : 0.04 + Math.random() * 0.08;
        if (phase === "pause") syllablesLeft = 4 + Math.floor(Math.random() * 6);
      } else {
        phase = "syllable";
        dur = 0.12 + Math.random() * 0.14;
        amp = 0.35 + Math.random() * 0.6;
      }
      left = dur;
    }
    let value;
    if (phase === "syllable") {
      const x = 1 - left / dur;
      value = amp * Math.sin(Math.PI * x) * (0.85 + Math.random() * 0.15);
    } else {
      value = 0.015 + Math.random() * 0.04;
    }
    emit({ event: "level", value: Math.round(Math.max(0, Math.min(1, value)) * 1000) / 1000 });
  }, 1000 / 30);
}

function stopLevels() {
  clearInterval(levelTimer);
  levelTimer = null;
}

// --- tentative text: the phrase being recognised grows word by word ---
function startText() {
  stopText();
  let t = 500;
  for (const phrase of PHRASES) {
    const words = phrase.split(" ");
    for (let i = 1; i <= words.length; i += 1) {
      const text = words.slice(0, i).join(" ");
      textTimers.push(setTimeout(() => state === "listening" && emit({ event: "tentative", text }), t));
      t += 260 + Math.random() * 120;
    }
    t += 1600; // the phrase stays on screen, then it is "typed" and cleared
    textTimers.push(setTimeout(() => state === "listening" && emit({ event: "tentative", text: "" }), t));
    t += 700;
  }
}

function stopText() {
  for (const timer of textTimers) clearTimeout(timer);
  textTimers = [];
}

function toggle() {
  if (state === "idle" || state === "done" || state === "error") {
    setState("listening");
    startLevels();
    startText();
    return;
  }
  if (state === "listening") {
    stopLevels();
    stopText();
    emit({ event: "tentative", text: "" });
    setState("processing");
    textTimers.push(
      setTimeout(() => {
        setState("done");
        history.unshift({ time: new Date().toISOString(), text: PHRASES.join(" ") });
        if (settings.final_check) {
          emit({ event: "notice", title: "Текст проверен", body: "Исправлено 2 места", kind: "success" });
        }
        textTimers.push(setTimeout(() => state === "done" && setState("idle"), 900));
      }, PROCESSING_MS)
    );
  }
}

function cancel() {
  stopLevels();
  stopText();
  emit({ event: "tentative", text: "" });
  setState("idle");
}

function setSettings(partial) {
  if (!partial || typeof partial !== "object") return;
  const known = new Set(Object.keys(settings));
  for (const [key, value] of Object.entries(partial)) {
    if (known.has(key)) settings[key] = value;
  }
  emit({ event: "settings", settings });
}

function downloadModel(model) {
  const info = models[model];
  if (!info || downloadTimers[model]) return;
  let done = 0;
  const total = info.size_mb;
  downloadTimers[model] = setInterval(() => {
    done = Math.min(total, done + total * (0.015 + Math.random() * 0.02));
    const finished = done >= total;
    emit({
      event: "model_progress",
      model,
      downloaded_mb: Math.round(done),
      total_mb: total,
      done: finished,
    });
    if (finished) {
      clearInterval(downloadTimers[model]);
      delete downloadTimers[model];
      info.installed = true;
    }
  }, 150);
}

function handle(msg) {
  switch (msg.cmd) {
    case "toggle":
      return toggle();
    case "cancel":
      return cancel();
    case "get_settings":
      return emit({ event: "settings", settings });
    case "set_settings":
      return setSettings(msg.settings);
    case "list_devices":
      // The protocol has no reply event for list_devices yet; this is the proposed one.
      return emit({ event: "devices", devices });
    case "capture_hotkey":
      return setTimeout(() => {
        const hotkey = HOTKEYS[hotkeyIndex % HOTKEYS.length];
        hotkeyIndex += 1;
        emit({ event: "hotkey_captured", hotkey });
      }, 1600);
    case "download_model":
      return downloadModel(String(msg.model || ""));
    case "get_history":
      return setTimeout(() => emit({ event: "history", items: history.slice(0, 50) }), 150);
    case "quit":
      stopLevels();
      stopText();
      return process.stdout.write("", () => process.exit(0));
    // test-only
    case "mock_notice":
      return emit({ event: "notice", title: msg.title, body: msg.body, kind: msg.kind || "info" });
    case "mock_state":
      return setState(String(msg.state), msg.detail);
    case "mock_tentative":
      return emit({ event: "tentative", text: String(msg.text || "") });
    case "mock_crash":
      return process.exit(3);
    default:
      return emit({ event: "error", message: `Неизвестная команда: ${msg.cmd}` });
  }
}

readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const text = line.trim();
  if (!text) return;
  let msg;
  try {
    msg = JSON.parse(text);
  } catch {
    return emit({ event: "error", message: "Команда не в формате JSON" });
  }
  if (msg && typeof msg.cmd === "string") handle(msg);
});
process.stdin.on("close", () => process.exit(0)); // the UI is gone

setTimeout(() => {
  emit({ event: "ready", version: "0.2.0-mock", hotkey_ok: true, settings, devices, models });
  emit({
    event: "notice",
    title: "LiveType готов",
    body: `${prettyHotkey(settings.hotkey)} — начать и закончить диктовку`,
    kind: "success",
  });
}, READY_DELAY_MS);

if (CRASH_AFTER_S > 0) setTimeout(() => process.exit(3), CRASH_AFTER_S * 1000);
