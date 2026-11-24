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

// Live preview bubble: Type's transcription preview window
const TRANSCRIPTION_PREVIEW_SIZE_LIMITS = {
  minWidth: 400,
  defaultWidth: 460,
  maxWidth: 640,
  minHeight: 96,
  defaultHeight: 132,
  maxHeight: 520,
};
// Type's preview window is 420 wide with 8 px padding around the card (card 404 wide, 16 px above the
// capsule window). That padding clips the card's 24 px shadow, so ours keeps the card in the same place
// but gives the transparent, click-through window room for the shadow.
const PREVIEW_PAD = { top: 16, side: 24, bottom: 32 };
const PREVIEW_CARD_WIDTH = 420 - 16;
const PREVIEW_WINDOW_WIDTH = PREVIEW_CARD_WIDTH + PREVIEW_PAD.side * 2;
// Type: window bottom 8 px above the capsule window, card 8 px inside -> card bottom 16 px above.
const PREVIEW_GAP = 16 - PREVIEW_PAD.bottom;

const TRANSCRIPTION_PREVIEW_CONFIG = {
  width: TRANSCRIPTION_PREVIEW_SIZE_LIMITS.defaultWidth,
  height: TRANSCRIPTION_PREVIEW_SIZE_LIMITS.defaultHeight,
  title: "LiveType — превью",
  frame: false,
  transparent: true,
  backgroundColor: "#00000000",
  alwaysOnTop: true,
  skipTaskbar: true,
  resizable: false,
  focusable: false,
  hasShadow: false,
  show: false,
  acceptsFirstMouse: true,
  webPreferences: overlayWebPreferences,
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

  static getTranscriptionPreviewPosition(display, mainWindowBounds, size = {}, gap = 8) {
    const width = size.width || TRANSCRIPTION_PREVIEW_CONFIG.width;
    const height = size.height || TRANSCRIPTION_PREVIEW_CONFIG.height;
    const GAP = gap;
    const workArea = display.workArea || display.bounds;

    let x = Math.round(mainWindowBounds.x + (mainWindowBounds.width - width) / 2);
    let y = mainWindowBounds.y - height - GAP;

    x = Math.max(workArea.x, Math.min(x, workArea.x + workArea.width - width));
    y = Math.max(workArea.y, Math.min(y, workArea.y + workArea.height - height));

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
  TRANSCRIPTION_PREVIEW_CONFIG,
  TRANSCRIPTION_PREVIEW_SIZE_LIMITS,
  PREVIEW_PAD,
  PREVIEW_WINDOW_WIDTH,
  PREVIEW_GAP,
  DICTATION_PILL_WINDOW,
  WindowPositionUtil,
};
