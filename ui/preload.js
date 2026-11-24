// Preload shared by every LiveType window (sandboxed, context-isolated).
// Exposes window.livetype: a small, explicit API; nothing from Node leaks into the pages.
"use strict";
const { contextBridge, ipcRenderer } = require("electron");

// main -> renderer channels a page may subscribe to
const CHANNELS = new Set(["state", "capsule", "level", "sound", "preview", "toast"]);

contextBridge.exposeInMainWorld("livetype", {
  // Subscribe to a main-process channel. Returns an unsubscribe function.
  on(channel, callback) {
    if (!CHANNELS.has(channel)) throw new Error(`Unknown channel: ${channel}`);
    const handler = (_event, data) => callback(data);
    ipcRenderer.on(`lt:${channel}`, handler);
    return () => ipcRenderer.removeListener(`lt:${channel}`, handler);
  },

  // Engine command, e.g. {cmd: "set_settings", settings: {sounds: false}}.
  send(command) {
    ipcRenderer.send("lt:engine", command);
  },
  // Snapshot of the UI state (engine status, settings, devices, models, ...).
  getState() {
    return ipcRenderer.invoke("lt:get-state");
  },

  // overlays
  capsuleHidden(gen) {
    ipcRenderer.send("lt:capsule-hidden", gen);
  },
  previewResize(height) {
    ipcRenderer.send("lt:preview-resize", height);
  },
  toastResize(height) {
    ipcRenderer.send("lt:toast-resize", height);
  },
  toastDone(id) {
    ipcRenderer.send("lt:toast-done", id);
  },

  // control panel
  panelResize(height) {
    ipcRenderer.send("lt:panel-resize", height);
  },
  windowMinimize() {
    ipcRenderer.send("lt:window", "minimize");
  },
  windowClose() {
    ipcRenderer.send("lt:window", "close");
  },
  restartEngine() {
    ipcRenderer.send("lt:restart-engine");
  },
  quitApp() {
    ipcRenderer.send("lt:quit");
  },
  copyText(text) {
    return ipcRenderer.invoke("lt:copy", String(text ?? ""));
  },

  log(level, message, meta) {
    ipcRenderer.send("lt:log", { level: String(level || "info"), message: String(message || ""), meta: meta ?? null });
  },
});
