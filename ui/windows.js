// Window configurations and positions. Values follow Type's src/helpers/windowConfig.js
// (GigaType2 / OpenWhispr, MIT) for Windows; the toast reuses Type's notification window size.
"use strict";
const path = require("path");

const PRELOAD = path.join(__dirname, "preload.js");

// Golos' compact SiriAI surface needs transparent side gutters for its glass rim, glow and spring overshoot.
const DICTATION_PILL_WINDOW = { width: 168, height: 119 };
const MAIN_WINDOW_BOTTOM_OFFSET = 115;
const LIGHT_CONTROL_PANEL_BACKGROUND = "#f5f5f5";

const overlayWebPreferences = {
  preload: PRELOAD,
  nodeIntegration: false,
  contextIsolation: true,
  sandbox: true,
  // The overlay is normally unfocused and stays hidden between recordings.
  backgroundThrottling: false,
  spellcheck: false,
};

// Dictation capsule
const MAIN_WINDOW_CONFIG = {
  width: DICTATION_PILL_WINDOW.width,
  height: DICTATION_PILL_WINDOW.height,
  title: "LiveType — капсула",
  webPreferences: overlayWebPreferences,
  frame: false,
  alwaysOnTop: true,
  resizable: false,
  transparent: true,
  backgroundColor: "#00000000",
  show: false,
  skipTaskbar: true,
  focusable: false,
  fullscreenable: false,
  hasShadow: false,
  acceptsFirstMouse: true,
  type: "normal",
};

// Control panel
const CONTROL_PANEL_CONFIG = {
  width: 500,
  height: 560,
  minHeight: 360,
  backgroundColor: LIGHT_CONTROL_PANEL_BACKGROUND,
  webPreferences: {
    preload: PRELOAD,
    nodeIntegration: false,
    contextIsolation: true,
    sandbox: true,
    webSecurity: true,
    spellcheck: false,
    backgroundThrottling: false,
  },
  title: "LiveType",
  resizable: false,
  show: false,
  // Frameless; the page draws its own window controls.
  frame: false,
  transparent: false,
  minimizable: true,
  maximizable: false,
  closable: true,
  fullscreenable: false,
  skipTaskbar: false,
  alwaysOnTop: false,
  type: "normal",
};

// Toast notices: Type's NOTIFICATION_WINDOW_CONFIG size, placed at the capsule instead of the corner
const NOTIFICATION_WINDOW_CONFIG = {
  width: 392,
  height: 92,
  title: "LiveType — уведомление",
  frame: false,
  transparent: true,
  backgroundColor: "#00000000",
  alwaysOnTop: true,
  skipTaskbar: true,
  resizable: false,
  focusable: false,
  hasShadow: false,
  show: false,
  webPreferences: overlayWebPreferences,
  type: "normal",
};

class WindowPositionUtil {
  static getMainWindowPosition(display, customSize = null, options = {}) {
    const { width, height } = customSize || DICTATION_PILL_WINDOW;
    const bounds = display.bounds || display.workArea;

    const x = Math.max(
      bounds.x,
      Math.min(Math.round(bounds.x + (bounds.width - width) / 2), bounds.x + bounds.width - width)
    );
    const y =
      options.placement === "top"
        ? bounds.y + MAIN_WINDOW_BOTTOM_OFFSET
        : Math.max(bounds.y, bounds.y + bounds.height - height - MAIN_WINDOW_BOTTOM_OFFSET);

    return { x, y, width, height };
  }

  // Toast: centred over the capsule. `bottom` is the y its lower edge should sit at.
  static getToastPosition(display, anchorBounds, bottom, size) {
    const workArea = display.workArea || display.bounds;
    let x = Math.round(anchorBounds.x + (anchorBounds.width - size.width) / 2);
    let y = Math.round(bottom - size.height);
    x = Math.max(workArea.x, Math.min(x, workArea.x + workArea.width - size.width));
    y = Math.max(workArea.y, Math.min(y, workArea.y + workArea.height - size.height));
    return { x, y, width: size.width, height: size.height };
  }

  static setupAlwaysOnTop(window) {
    // The Start menu and other Windows shell surfaces can cover "pop-up-menu" windows even though
    // Electron still reports them as always-on-top. Use the strongest non-focus-stealing level.
    window.setAlwaysOnTop(true, "screen-saver");
  }
}

module.exports = {
  MAIN_WINDOW_CONFIG,
  CONTROL_PANEL_CONFIG,
  NOTIFICATION_WINDOW_CONFIG,
  DICTATION_PILL_WINDOW,
  WindowPositionUtil,
};
