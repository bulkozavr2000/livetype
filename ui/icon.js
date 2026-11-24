// LiveType app/tray icon: the logo (sound-wave bars turning into a text cursor), from assets/icons.
// livetype.ico holds 16…256 px, so Windows picks a sharp size for the tray, taskbar and title bar.
"use strict";

const path = require("path");

const ICO = path.join(__dirname, "assets", "icons", "livetype.ico");

function trayIcon(nativeImage) {
  return nativeImage.createFromPath(ICO);
}

function windowIcon(nativeImage) {
  return nativeImage.createFromPath(ICO);
}

module.exports = { trayIcon, windowIcon, ICO };
