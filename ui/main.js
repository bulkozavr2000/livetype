// LiveType UI shell: Electron main process.
// The look and window behaviour replicate Type (GigaType2, a fork of OpenWhispr; both MIT).
// LiveType's Python engine does the work; it talks JSON lines over stdio (see engine.js).
//
//   electron.exe <project>\ui [--mock] [--hidden]
//     --mock    run ui/dev/mock-engine.js (simulated events) instead of Python
//     --hidden  start in the tray without opening the control panel
"use strict";
const { app, BrowserWindow, ipcMain, screen, Tray, Menu, nativeImage, clipboard, powerMonitor } = require("electron");
const fs = require("fs");
const path = require("path");
const { EngineProcess } = require("./engine");
const W = require("./windows");
const icons = require("./icon");

const ARGV = process.argv.slice(1);
const IS_MOCK = ARGV.includes("--mock");
const START_HIDDEN = ARGV.includes("--hidden");
const MOCK_ARGS = ARGV.filter((a) => a.startsWith("--mock-"));
const PROJECT_ROOT = path.resolve(__dirname, "..");
const APP_ID = "com.livetype.app";

// Same rule as the engine (livetype/config.py): LIVETYPE_HOME > %APPDATA%\LiveType\home.txt > %LOCALAPPDATA%\LiveType
function resolveDataDir() {
  if (process.env.LIVETYPE_HOME) return process.env.LIVETYPE_HOME;
  try {
    const pointer = path.join(process.env.APPDATA || "", "LiveType", "home.txt");
    const text = fs.readFileSync(pointer, "utf8").trim();
    if (text) return text;
  } catch {
    /* no pointer */
  }
  return path.join(process.env.LOCALAPPDATA || app.getPath("appData"), "LiveType");
}
const DATA_DIR = resolveDataDir();
// development: LIVETYPE_PYTHON, else a .venv in the repository, else one next to the models
const REPO_PYTHON = path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe");
const PYTHON =
  process.env.LIVETYPE_PYTHON ||
  (fs.existsSync(REPO_PYTHON) ? REPO_PYTHON : path.join(DATA_DIR, ".venv", "Scripts", "python.exe"));
// installed app: the engine is a self-contained exe next to the UI (resources\engine)
const PACKAGED_ENGINE = app.isPackaged ? path.join(process.resourcesPath, "engine", "livetype-engine.exe") : null;
const UI_VERSION = require("./package.json").version;

// Keep Chromium's profile next to the models (big data stays on the drive the user chose).
try {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  app.setPath("userData", path.join(DATA_DIR, "ui-data"));
} catch {
  /* default location */
}

// ---------------------------------------------------------------------------------------------------
// log: events and timings only, never dictated text
// ---------------------------------------------------------------------------------------------------
const LOG_FILE = path.join(DATA_DIR, "logs", "ui.log");
try {
  fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
  if (fs.existsSync(LOG_FILE) && fs.statSync(LOG_FILE).size > 2 * 1024 * 1024) {
    fs.renameSync(LOG_FILE, `${LOG_FILE}.1`);
  }
} catch {
  /* logging is best effort */
}
function log(message) {
  const line = `${new Date().toISOString()} ${message}\n`;
  try {
    fs.appendFileSync(LOG_FILE, line);
  } catch {
    /* ignore */
  }
}

// Cues are played by the hidden capsule page without a user gesture.
app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    log("second instance: opening the control panel");
    if (app.isReady()) showControlPanel();
  });
  // the windows only show our own local pages: no navigation, no new windows
  app.on("web-contents-created", (_event, contents) => {
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    contents.on("will-navigate", (event) => event.preventDefault());
  });
  app.whenReady().then(onReady);
}

// ---------------------------------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------------------------------
const DEFAULT_SETTINGS = {
  hotkey: "ctrl+alt+d",
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
const DEFAULT_MODELS = {
  light: { label: "Лёгкая", installed: false, size_mb: 563 },
  accurate: { label: "Точная", installed: false, size_mb: 2497 },
};
const DICTATION_STATES = new Set(["idle", "listening", "processing", "done", "error"]);
const ENGINE_COMMANDS = new Set([
  "toggle",
  "cancel",
  "get_settings",
  "set_settings",
  "list_devices",
  "capture_hotkey",
  "cancel_capture",
  "download_model",
  "get_history",
]);

const state = {
  engine: "starting", // starting | running | restarting | failed | stopped
  engineInfo: {},
  ready: false,
  version: null,
  hotkeyOk: true,
  settings: structuredClone(DEFAULT_SETTINGS),
  devices: [],
  models: structuredClone(DEFAULT_MODELS),
  dictation: "idle",
  detail: null,
  modelProgress: {}, // model -> {downloaded_mb, total_mb, done, error}
  pendingModel: null, // model the user picked that is still downloading
  history: null,
  historyLoading: false,
  hotkeyCapture: false,
};
let tentative = "";

let engine = null;
let capsuleWin = null;
let previewWin = null;
let toastWin = null;
let panelWin = null;
let tray = null;
let quitting = false;

function normalizeSettings(s) {
  const out = { ...s };
  out.activation_mode = out.activation_mode === "hold" ? "hold" : "toggle";
  out.text_rules = Array.isArray(out.text_rules)
    ? out.text_rules.filter((r) => r && typeof r === "object" && r.id != null)
    : [];
  if (out.microphone === "" || out.microphone === undefined) out.microphone = null;
  return out;
}

function mergeModels(models) {
  const out = structuredClone(state.models);
  if (models && typeof models === "object") {
    for (const [id, info] of Object.entries(models)) {
      if (info && typeof info === "object") out[id] = { ...(out[id] || {}), ...info };
    }
  }
  return out;
}

// Last known settings/devices/models, so the panel is right before the engine says "ready".
const CACHE_FILE = path.join(app.getPath("userData"), IS_MOCK ? "last-state-mock.json" : "last-state.json");
function loadCache() {
  try {
    const c = JSON.parse(fs.readFileSync(CACHE_FILE, "utf8"));
    if (c.settings) state.settings = normalizeSettings({ ...DEFAULT_SETTINGS, ...c.settings });
    if (Array.isArray(c.devices)) state.devices = c.devices;
    if (c.models) state.models = mergeModels(c.models);
  } catch {
    /* first run */
  }
}
let cacheTimer = null;
function saveCache() {
  clearTimeout(cacheTimer);
  cacheTimer = setTimeout(() => {
    try {
      fs.mkdirSync(path.dirname(CACHE_FILE), { recursive: true });
      fs.writeFileSync(
        CACHE_FILE,
        JSON.stringify({ settings: state.settings, devices: state.devices, models: state.models }, null, 1)
      );
    } catch (error) {
      log(`cache write failed: ${error.message}`);
    }
  }, 400);
}

function snapshot() {
  return {
    engine: state.engine,
    engineInfo: state.engineInfo,
    ready: state.ready,
    version: state.version,
    hotkeyOk: state.hotkeyOk,
    settings: state.settings,
    devices: state.devices,
    models: state.models,
    dictation: state.dictation,
    modelProgress: state.modelProgress,
    pendingModel: state.pendingModel,
    history: state.history,
    historyLoading: state.historyLoading,
    hotkeyCapture: state.hotkeyCapture,
    mock: IS_MOCK,
    uiVersion: UI_VERSION,
    enginePid: engine ? engine.pid : null,
  };
}

let broadcastTimer = null;
function broadcast() {
  if (broadcastTimer) return;
  broadcastTimer = setTimeout(() => {
    broadcastTimer = null;
    sendTo(panelWin, "lt:state", snapshot());
  }, 16);
}

function sendTo(win, channel, data) {
  if (win && !win.isDestroyed() && !win.webContents.isDestroyed()) win.webContents.send(channel, data);
}

// ---------------------------------------------------------------------------------------------------
// engine
// ---------------------------------------------------------------------------------------------------
function createEngine() {
  // LIVETYPE_UI_PID: the engine never types into our own windows (panel, capsule)
  const env = { ...process.env, PYTHONUNBUFFERED: "1", LIVETYPE_UI_PID: String(process.pid) };
  let command;
  let args;
  if (IS_MOCK) {
    command = process.execPath; // electron.exe acting as plain Node
    args = [path.join(__dirname, "dev", "mock-engine.js"), ...MOCK_ARGS];
    env.ELECTRON_RUN_AS_NODE = "1";
  } else {
    if (PACKAGED_ENGINE && fs.existsSync(PACKAGED_ENGINE)) {
      command = PACKAGED_ENGINE;
      args = ["--ipc"];
    } else {
      command = PYTHON;
      args = ["-X", "utf8", "-m", "livetype", "--ipc"];
    }
    // tests: extra engine arguments as a JSON array, e.g. ["--source", "C:\\...\\speech.wav"]
    if (process.env.LIVETYPE_ENGINE_ARGS) {
      try {
        args.push(...JSON.parse(process.env.LIVETYPE_ENGINE_ARGS));
      } catch (error) {
        log(`bad LIVETYPE_ENGINE_ARGS: ${error.message}`);
      }
    }
    delete env.ELECTRON_RUN_AS_NODE;
  }
  const cwd = command === PACKAGED_ENGINE ? path.dirname(PACKAGED_ENGINE) : PROJECT_ROOT;
  log(`engine: ${IS_MOCK ? "mock" : command} ${args.join(" ")} (cwd ${cwd})`);
  engine = new EngineProcess({ command, args, cwd, env, log });
  engine.on("event", (event) => {
    try {
      onEngineEvent(event);
    } catch (error) {
      log(`engine event handler failed (${event.event}): ${error.stack || error.message}`);
    }
  });
  engine.on("status", onEngineStatus);
  engine.on("stderr", (line) => log(`engine stderr: ${line.slice(0, 300)}`));
  engine.start();
}

function onEngineStatus(status, info) {
  log(`engine status: ${status} ${JSON.stringify(info || {})}`);
  const wasReady = state.ready;
  state.engine = status;
  state.engineInfo = info || {};
  if (status !== "running") {
    state.ready = false;
    state.hotkeyCapture = false;
    state.historyLoading = false;
    state.modelProgress = {};
    state.pendingModel = null;
    tentative = "";
    if (state.dictation !== "idle") setDictation("idle", null);
  }
  if (status === "failed") {
    const panelOpen = panelWin && !panelWin.isDestroyed() && panelWin.isVisible();
    showToast({
      title: "Распознавание не запустилось",
      body: panelOpen
        ? "Нажмите «Повторить» в панели управления."
        : "Откройте панель управления и нажмите «Повторить».",
      kind: "error",
    });
  } else if (status === "restarting" && wasReady) {
    showToast({ title: "Перезапускаю распознавание", body: "Движок остановился, запускаю заново.", kind: "warning" });
  }
  syncOverlays();
  broadcast();
}

function onEngineEvent(ev) {
  switch (ev.event) {
    case "ready":
      log(`engine ready v${ev.version} hotkey_ok=${ev.hotkey_ok}`);
      state.ready = true;
      state.version = ev.version ?? null;
      state.hotkeyOk = ev.hotkey_ok !== false;
      if (ev.settings && typeof ev.settings === "object") {
        state.settings = normalizeSettings({ ...state.settings, ...ev.settings });
      }
      if (Array.isArray(ev.devices)) state.devices = ev.devices;
      if (ev.models) state.models = mergeModels(ev.models);
      tentative = "";
      setDictation("idle", null);
      saveCache();
      break;
    case "state":
      setDictation(String(ev.state), ev.detail ?? null);
      return;
    case "level":
      if (capsuleWanted) {
        const v = Number(ev.value);
        sendTo(capsuleWin, "lt:level", Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0);
      }
      return;
    case "tentative":
      tentative = typeof ev.text === "string" ? ev.text : "";
      syncPreview();
      return;
    case "notice":
      log(`notice (${ev.kind}): ${String(ev.title || "").slice(0, 80)}`);
      showToast({ title: ev.title, body: ev.body, kind: ev.kind });
      return;
    case "settings":
      if (ev.settings && typeof ev.settings === "object") {
        state.settings = normalizeSettings({ ...state.settings, ...ev.settings });
      }
      // not in the protocol, accepted if the engine ever sends them here
      if (typeof ev.hotkey_ok === "boolean") state.hotkeyOk = ev.hotkey_ok;
      if (Array.isArray(ev.devices)) state.devices = ev.devices;
      saveCache();
      syncOverlays();
      break;
    case "hotkey_captured":
      clearTimeout(hotkeyCaptureTimer);
      state.hotkeyCapture = false;
      if (typeof ev.hotkey === "string" && ev.hotkey.trim()) {
        state.settings = { ...state.settings, hotkey: ev.hotkey };
        engine.send({ cmd: "set_settings", settings: { hotkey: ev.hotkey } });
        saveCache();
      }
      break;
    case "model_progress":
      onModelProgress(ev);
      break;
    case "history":
      state.history = Array.isArray(ev.items) ? ev.items : [];
      state.historyLoading = false;
      break;
    case "devices": // not in the protocol yet: the reply to list_devices
      if (Array.isArray(ev.devices)) {
        state.devices = ev.devices;
        saveCache();
      }
      break;
    case "error":
      log(`engine error: ${String(ev.message || "").slice(0, 300)}`);
      showToast({ title: "Ошибка", body: ev.message, kind: "error" });
      return;
    default:
      log(`unknown engine event: ${ev.event}`);
      return;
  }
  broadcast();
}

function onModelProgress(ev) {
  const model = String(ev.model || "");
  if (!model) return;
  const entry = {
    downloaded_mb: Number(ev.downloaded_mb) || 0,
    total_mb: Number(ev.total_mb) || (state.models[model] && state.models[model].size_mb) || 0,
    done: Boolean(ev.done),
    error: ev.error ? String(ev.error) : null,
  };
  if (!entry.done) {
    state.modelProgress = { ...state.modelProgress, [model]: entry };
    return;
  }
  const rest = { ...state.modelProgress };
  delete rest[model];
  if (!entry.error) {
    log(`model ${model} downloaded`);
    state.modelProgress = rest;
    // "speech" = first-start download of the recognition models, not a text-processing model
    if (model !== "speech") state.models = mergeModels({ [model]: { installed: true } });
    if (state.pendingModel === model) {
      state.pendingModel = null;
      state.settings = { ...state.settings, text_model: model };
      engine.send({ cmd: "set_settings", settings: { text_model: model } });
    }
  } else {
    log(`model ${model} download failed: ${entry.error.slice(0, 200)}`);
    if (state.pendingModel === model) state.pendingModel = null;
    state.modelProgress = { ...rest, [model]: entry }; // the panel shows the error for a while
    setTimeout(() => {
      if (state.modelProgress[model] && state.modelProgress[model].error) {
        const next = { ...state.modelProgress };
        delete next[model];
        state.modelProgress = next;
        broadcast();
      }
    }, 6000);
  }
  saveCache();
}

function setDictation(next, detail) {
  if (!DICTATION_STATES.has(next)) {
    log(`unknown dictation state: ${next}`);
    return;
  }
  const prev = state.dictation;
  state.dictation = next;
  state.detail = detail ?? null;
  if (prev !== next) {
    log(`state: ${prev} -> ${next}`);
    if (state.settings.sounds !== false) {
      if (next === "listening") playSound("start");
      else if (prev === "listening") playSound("stop");
    }
    if (next !== "listening" && next !== "processing") tentative = "";
  }
  syncOverlays();
  broadcast();
}

function playSound(name) {
  sendTo(capsuleWin, "lt:sound", name);
}

let hotkeyCaptureTimer = null;
function onUiCommand(command) {
  if (!command || typeof command !== "object" || typeof command.cmd !== "string") return;
  const cmd = command.cmd;
  const allowed = ENGINE_COMMANDS.has(cmd) || (IS_MOCK && cmd.startsWith("mock_"));
  if (!allowed) {
    log(`ui command rejected: ${cmd}`);
    return;
  }
  switch (cmd) {
    case "set_settings": {
      const s = command.settings;
      if (!s || typeof s !== "object") return;
      state.settings = normalizeSettings({ ...state.settings, ...s }); // optimistic; the engine echoes "settings"
      saveCache();
      syncOverlays();
      broadcast();
      break;
    }
    case "capture_hotkey":
      state.hotkeyCapture = true;
      clearTimeout(hotkeyCaptureTimer);
      hotkeyCaptureTimer = setTimeout(() => {
        // the listener gives up after 20 s on its own; make sure it is not left swallowing keys
        state.hotkeyCapture = false;
        if (engine) engine.send({ cmd: "cancel_capture" });
        broadcast();
      }, 21000);
      broadcast();
      break;
    case "cancel_capture":
      clearTimeout(hotkeyCaptureTimer);
      state.hotkeyCapture = false;
      broadcast();
      break;
    case "download_model": {
      const model = String(command.model || "");
      if (!state.models[model]) return;
      state.pendingModel = model;
      state.modelProgress = {
        ...state.modelProgress,
        [model]: { downloaded_mb: 0, total_mb: state.models[model].size_mb || 0, done: false, error: null },
      };
      broadcast();
      break;
    }
    case "get_history":
      state.historyLoading = true;
      broadcast();
      break;
    default:
      break;
  }
  if (!engine || !engine.send(command)) log(`engine not running, dropped: ${cmd}`);
}

// ---------------------------------------------------------------------------------------------------
// overlay windows (capsule, live preview, toast): never focusable, click-through
// ---------------------------------------------------------------------------------------------------
function cursorDisplay() {
  return screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
}

function overlayDisplay() {
  if (capsuleWin && !capsuleWin.isDestroyed() && capsuleWin.isVisible()) {
    return screen.getDisplayMatching(capsuleWin.getBounds());
  }
  return cursorDisplay();
}

// Where the capsule is, or would be on `display`.
function capsuleAnchorBounds(display) {
  if (capsuleWin && !capsuleWin.isDestroyed() && capsuleWin.isVisible()) return capsuleWin.getBounds();
  return W.WindowPositionUtil.getMainWindowPosition(display);
}

function createOverlayWindow(config, page, display) {
  const win = new BrowserWindow({
    ...config,
    webPreferences: { ...config.webPreferences },
    ...W.WindowPositionUtil.getMainWindowPosition(display, { width: config.width, height: config.height }),
  });
  // Windows: no mouse-move forwarding needed; the overlays have no controls.
  win.setIgnoreMouseEvents(true);
  W.WindowPositionUtil.setupAlwaysOnTop(win);
  win.on("page-title-updated", (e) => e.preventDefault());
  win.webContents.on("render-process-gone", (_e, details) => {
    log(`${page} renderer gone: ${details.reason}`);
    setTimeout(() => {
      if (!win.isDestroyed()) win.webContents.reload();
    }, 500);
  });
  win.loadFile(path.join(__dirname, "overlay", page));
  return win;
}

function createOverlayWindows() {
  const display = cursorDisplay();
  capsuleWin = createOverlayWindow(W.MAIN_WINDOW_CONFIG, "capsule.html", display);
  previewWin = createOverlayWindow(W.TRANSCRIPTION_PREVIEW_CONFIG, "preview.html", display);
  toastWin = createOverlayWindow(W.NOTIFICATION_WINDOW_CONFIG, "toast.html", display);
  capsuleWin.webContents.on("did-finish-load", () => {
    // a reloaded capsule page must catch up with the current state
    if (capsuleWanted) {
      const phase = state.dictation === "processing" ? "transcribing" : "listening";
      sendTo(capsuleWin, "lt:capsule", { visible: true, phase, gen: capsuleGen });
    }
  });
}

function syncOverlays() {
  syncCapsule();
  syncPreview();
  if (toastVisible) positionToast();
}

// --- capsule (Type: showDictationPanel / hideDictationPanel) ---
let capsuleWanted = false;
let capsuleGen = 0;
let capsuleHideTimer = null;
const raiseTimers = new Set();

function syncCapsule() {
  const st = state.dictation;
  const want = (st === "listening" || st === "processing") && state.settings.show_capsule !== false;
  const phase = st === "processing" ? "transcribing" : "listening";
  if (want) {
    const firstShow = !capsuleWanted;
    capsuleWanted = true;
    if (firstShow) {
      capsuleGen += 1;
      clearTimeout(capsuleHideTimer);
      capsuleHideTimer = null;
    }
    sendTo(capsuleWin, "lt:capsule", { visible: true, phase, gen: capsuleGen });
    if (firstShow) showCapsuleWindow();
  } else if (capsuleWanted) {
    capsuleWanted = false;
    capsuleGen += 1;
    const gen = capsuleGen;
    // The page unmounts the renderer, waits for an empty frame and calls capsuleHidden(gen).
    sendTo(capsuleWin, "lt:capsule", { visible: false, gen });
    clearTimeout(capsuleHideTimer);
    capsuleHideTimer = setTimeout(() => finishCapsuleHide(gen), 250);
  }
}

function finishCapsuleHide(gen) {
  if (gen !== capsuleGen || capsuleWanted) return;
  clearTimeout(capsuleHideTimer);
  capsuleHideTimer = null;
  clearRaiseTimers();
  if (capsuleWin && !capsuleWin.isDestroyed()) capsuleWin.hide();
}

function clearRaiseTimers() {
  for (const t of raiseTimers) clearTimeout(t);
  raiseTimers.clear();
}

function repositionCapsule() {
  if (!capsuleWin || capsuleWin.isDestroyed()) return;
  capsuleWin.setBounds(W.WindowPositionUtil.getMainWindowPosition(cursorDisplay()));
  repaint(capsuleWin);
}

// A transparent window shown with showInactive() or moved with setBounds() can keep presenting its
// previous (empty) surface until something forces a frame; ask for a full repaint every time.
function repaint(win) {
  if (win && !win.isDestroyed() && !win.webContents.isDestroyed()) win.webContents.invalidate();
}

function raiseCapsuleWithoutFocus() {
  if (!capsuleWin || capsuleWin.isDestroyed()) return;
  W.WindowPositionUtil.setupAlwaysOnTop(capsuleWin);
  if (capsuleWin.isVisible()) {
    try {
      capsuleWin.moveTop();
    } catch (error) {
      log(`capsule raise failed: ${error.message}`);
    }
  }
}

function showCapsuleWindow() {
  if (!capsuleWin || capsuleWin.isDestroyed()) return;
  clearRaiseTimers();
  // Recompute even on the same display: disconnects/scaling changes can leave old bounds off-screen.
  if (!capsuleWin.isVisible()) repositionCapsule();
  W.WindowPositionUtil.setupAlwaysOnTop(capsuleWin);
  if (!capsuleWin.isVisible()) capsuleWin.showInactive();
  repaint(capsuleWin);
  raiseCapsuleWithoutFocus();
  // Explorer can reassert the Start menu's z-order just after the capsule is shown: re-raise
  // during that short shell transition, without activating the capsule.
  for (const delayMs of [0, 75, 250]) {
    const timer = setTimeout(() => {
      raiseTimers.delete(timer);
      raiseCapsuleWithoutFocus();
    }, delayMs);
    raiseTimers.add(timer);
  }
}

// --- live preview bubble (Type: transcription preview) ---
let previewWanted = false;
let previewHeight = W.TRANSCRIPTION_PREVIEW_SIZE_LIMITS.defaultHeight;
let previewHideTimer = null;
let previewGraceTimer = null;

function syncPreview() {
  const st = state.dictation;
  const recording = st === "listening" || st === "processing";
  const text = tentative.trim();
  if (recording && text && previewWin && !previewWin.isDestroyed()) {
    clearTimeout(previewGraceTimer);
    previewGraceTimer = null;
    clearTimeout(previewHideTimer);
    previewHideTimer = null;
    sendTo(previewWin, "lt:preview", { visible: true, text, mode: st === "processing" ? "cleanup" : "live" });
    if (!previewWanted) {
      previewWanted = true;
      positionPreview();
      if (!previewWin.isVisible()) previewWin.showInactive();
      W.WindowPositionUtil.setupAlwaysOnTop(previewWin);
      previewWin.moveTop();
      if (toastVisible) positionToast();
    }
    repaint(previewWin);
    return;
  }
  if (!previewWanted) return;
  if (recording) {
    // text cleared between phrases: a short grace period keeps the bubble from flickering
    if (!previewGraceTimer) {
      previewGraceTimer = setTimeout(() => {
        previewGraceTimer = null;
        hidePreview();
      }, 300);
    }
  } else {
    hidePreview();
  }
}

function hidePreview() {
  clearTimeout(previewGraceTimer);
  previewGraceTimer = null;
  if (!previewWanted) return;
  previewWanted = false;
  sendTo(previewWin, "lt:preview", { visible: false });
  clearTimeout(previewHideTimer);
  // Type: the page animates out for 200 ms, then the window hides
  previewHideTimer = setTimeout(() => {
    previewHideTimer = null;
    if (!previewWanted && previewWin && !previewWin.isDestroyed()) previewWin.hide();
    if (toastVisible) positionToast();
  }, 220);
}

function positionPreview() {
  if (!previewWin || previewWin.isDestroyed()) return;
  const L = W.TRANSCRIPTION_PREVIEW_SIZE_LIMITS;
  const extra = W.PREVIEW_PAD.top + W.PREVIEW_PAD.bottom - 16; // beyond Type's 2 x 8 px
  const display = overlayDisplay();
  const width = W.PREVIEW_WINDOW_WIDTH;
  const height = Math.max(L.minHeight + extra, Math.min(Math.round(previewHeight), L.maxHeight + extra));
  previewWin.setBounds(
    W.WindowPositionUtil.getTranscriptionPreviewPosition(
      display,
      capsuleAnchorBounds(display),
      { width, height },
      W.PREVIEW_GAP
    )
  );
  repaint(previewWin);
}

// --- toast notices ---
let toastSeq = 0;
let toastHeight = W.NOTIFICATION_WINDOW_CONFIG.height;
let toastVisible = false;
const TOAST_KINDS = new Set(["info", "success", "warning", "error"]);

function showToast({ title, body, kind }) {
  if (!toastWin || toastWin.isDestroyed()) return;
  const t = String(title ?? "").trim();
  const b = String(body ?? "").trim();
  if (!t && !b) return;
  toastSeq += 1;
  sendTo(toastWin, "lt:toast", {
    id: toastSeq,
    title: t,
    body: b,
    kind: TOAST_KINDS.has(kind) ? kind : "info",
    duration: 4000,
  });
  toastVisible = true;
  positionToast();
  if (!toastWin.isVisible()) toastWin.showInactive();
  W.WindowPositionUtil.setupAlwaysOnTop(toastWin);
  toastWin.moveTop();
  repaint(toastWin);
}

function positionToast() {
  if (!toastWin || toastWin.isDestroyed()) return;
  const display = overlayDisplay();
  const anchor = capsuleAnchorBounds(display);
  // The toast window has 18 px of transparent room under the toast; `bottom` is the window's lower edge.
  let bottom;
  if (previewWanted && previewWin && previewWin.isVisible()) {
    bottom = previewWin.getBounds().y + W.PREVIEW_PAD.top - 8 + 18; // 8 px above the preview card
  } else if (capsuleWin && capsuleWin.isVisible()) {
    bottom = anchor.y + 30; // ~16 px above the capsule glass
  } else {
    bottom = anchor.y + anchor.height - 10; // where the capsule glass would end
  }
  toastWin.setBounds(
    W.WindowPositionUtil.getToastPosition(display, anchor, bottom, {
      width: W.NOTIFICATION_WINDOW_CONFIG.width,
      height: Math.round(toastHeight),
    })
  );
  repaint(toastWin);
}

// ---------------------------------------------------------------------------------------------------
// control panel
// ---------------------------------------------------------------------------------------------------
let panelReveal = null;
let windowIcon = null;

function showControlPanel() {
  if (panelWin && !panelWin.isDestroyed()) {
    if (panelWin.isMinimized()) panelWin.restore();
    // show() even when visible: a window behind other apps is "visible" and focus() alone
    // does not bring it forward.
    panelWin.show();
    panelWin.moveTop();
    panelWin.focus();
    return;
  }
  const display = cursorDisplay();
  const wa = display.workArea;
  const width = W.CONTROL_PANEL_CONFIG.width;
  const height = Math.min(W.CONTROL_PANEL_CONFIG.height, wa.height);
  panelWin = new BrowserWindow({
    ...W.CONTROL_PANEL_CONFIG,
    webPreferences: { ...W.CONTROL_PANEL_CONFIG.webPreferences },
    icon: windowIcon,
    width,
    height,
    x: Math.round(wa.x + (wa.width - width) / 2),
    y: Math.round(wa.y + (wa.height - height) / 2),
  });
  const win = panelWin;
  win.on("page-title-updated", (e) => e.preventDefault());
  win.on("close", (e) => {
    if (!quitting) {
      e.preventDefault();
      win.hide(); // Type hides the panel to the tray
    }
  });
  win.on("closed", () => {
    if (panelWin === win) panelWin = null;
  });
  win.webContents.on("render-process-gone", (_e, details) => {
    log(`panel renderer gone: ${details.reason}`);
    setTimeout(() => {
      if (!win.isDestroyed()) win.webContents.reload();
    }, 1000);
  });
  // Show once the page has sized the window to its content (fallback: shortly after ready-to-show).
  let shown = false;
  let firstResize = true;
  panelReveal = () => {
    if (shown || win.isDestroyed()) return;
    shown = true;
    panelReveal = null;
    win.show();
    win.focus();
  };
  win.once("ready-to-show", () => setTimeout(() => panelReveal && panelReveal(), 400));
  win.__firstResize = () => {
    const was = firstResize;
    firstResize = false;
    return was;
  };
  win.loadFile(path.join(__dirname, "panel", "index.html"));
}

function resizePanel(contentHeight) {
  const win = panelWin;
  if (!win || win.isDestroyed()) return;
  const bounds = win.getBounds();
  const wa = screen.getDisplayMatching(bounds).workArea;
  const height = Math.max(W.CONTROL_PANEL_CONFIG.minHeight, Math.min(Math.ceil(Number(contentHeight) || 0), wa.height));
  const first = win.__firstResize ? win.__firstResize() : false;
  let y = first ? Math.round(wa.y + (wa.height - height) / 2) : bounds.y;
  y = Math.max(wa.y, Math.min(y, wa.y + wa.height - height));
  if (bounds.height !== height || bounds.y !== y) {
    win.setBounds({ x: bounds.x, y, width: W.CONTROL_PANEL_CONFIG.width, height });
  }
}

// ---------------------------------------------------------------------------------------------------
// IPC from the pages
// ---------------------------------------------------------------------------------------------------
function registerIpc() {
  ipcMain.handle("lt:get-state", () => snapshot());
  ipcMain.on("lt:engine", (_e, command) => onUiCommand(command));
  ipcMain.on("lt:capsule-hidden", (_e, gen) => finishCapsuleHide(gen));
  ipcMain.on("lt:preview-resize", (_e, height) => {
    const h = Number(height);
    if (!Number.isFinite(h) || h <= 0) return;
    previewHeight = h;
    if (previewWanted) positionPreview();
  });
  ipcMain.on("lt:toast-resize", (_e, height) => {
    const h = Number(height);
    if (!Number.isFinite(h) || h <= 0) return;
    toastHeight = Math.max(40, Math.min(h, 400));
    if (toastVisible) positionToast();
  });
  ipcMain.on("lt:toast-done", (_e, id) => {
    if (id !== toastSeq || !toastWin || toastWin.isDestroyed()) return;
    toastVisible = false;
    toastWin.hide();
  });
  ipcMain.on("lt:panel-resize", (e, height) => {
    if (!panelWin || e.sender !== panelWin.webContents) return;
    resizePanel(height);
    if (panelReveal) panelReveal();
  });
  ipcMain.on("lt:window", (e, action) => {
    if (!panelWin || e.sender !== panelWin.webContents) return;
    if (action === "minimize") panelWin.minimize();
    else if (action === "close") panelWin.hide();
  });
  ipcMain.on("lt:restart-engine", () => {
    log("engine restart requested from the panel");
    if (engine) engine.restart();
  });
  ipcMain.on("lt:quit", () => quitApp());
  ipcMain.handle("lt:copy", (_e, text) => {
    clipboard.writeText(String(text ?? ""));
    return true;
  });
  ipcMain.on("lt:log", (_e, entry) => {
    if (!entry) return;
    const meta = entry.meta ? ` ${JSON.stringify(entry.meta).slice(0, 300)}` : "";
    log(`renderer ${entry.level}: ${String(entry.message).slice(0, 300)}${meta}`);
  });
}

// ---------------------------------------------------------------------------------------------------
// tray, lifecycle
// ---------------------------------------------------------------------------------------------------
function createTray() {
  tray = new Tray(icons.trayIcon(nativeImage));
  const tooltip = IS_MOCK ? "LiveType — демо-режим" : "LiveType";
  const menu = Menu.buildFromTemplate([
    { label: "Открыть панель управления", click: () => showControlPanel() },
    { type: "separator" },
    { label: "Выход", click: () => quitApp() },
  ]);
  tray.setToolTip(tooltip);
  tray.setContextMenu(menu);
  tray.on("click", () => showControlPanel());
  log("tray ready");
  // ui/dev/verify.js drives the tray through the main-process inspector (demo mode only)
  if (IS_MOCK) global.__livetypeTest = { tray, menu, tooltip };
}

async function quitApp() {
  if (quitting) return;
  quitting = true;
  log("quit");
  try {
    if (engine) await engine.stop(2000);
  } catch (error) {
    log(`engine stop failed: ${error.message}`);
  }
  if (tray) {
    tray.destroy();
    tray = null;
  }
  app.quit();
}

function onReady() {
  log(`LiveType UI ${UI_VERSION} start${IS_MOCK ? " (mock engine)" : ""}, electron ${process.versions.electron}`);
  app.setAppUserModelId(APP_ID);
  windowIcon = icons.windowIcon(nativeImage);
  loadCache();
  registerIpc();
  createOverlayWindows();
  createTray();
  createEngine();
  if (!START_HIDDEN) showControlPanel();

  const reposition = () => {
    if (capsuleWin && !capsuleWin.isDestroyed() && capsuleWin.isVisible()) repositionCapsule();
    if (previewWanted) positionPreview();
    if (toastVisible) positionToast();
  };
  screen.on("display-metrics-changed", reposition);
  screen.on("display-added", reposition);
  screen.on("display-removed", reposition);
  powerMonitor.on("resume", () => {
    log("system resumed");
    if (!capsuleWanted && capsuleWin && !capsuleWin.isDestroyed()) capsuleWin.hide();
  });
}

if (gotLock) {
  app.on("before-quit", (e) => {
    if (!quitting) {
      e.preventDefault();
      quitApp();
    }
  });
  app.on("window-all-closed", () => {
    /* keep running in the tray */
  });
  app.on("will-quit", () => {
    if (engine) engine.killNow();
  });
  app.on("session-end", () => {
    if (engine) engine.killNow();
  });
  process.on("exit", () => {
    if (engine) engine.killNow();
  });
}
