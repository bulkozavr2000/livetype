// Bare Electron window for tests/e2e_uia.py: shows one page, like the Claude desktop app shows
// its composer (Electron + Chromium accessibility).
//   electron.exe tests/electron_shell/main.js --url=file:///.../tiptap.html --profile=<folder>
const { app, BrowserWindow } = require("electron");

const opt = (name, dflt) => {
  const a = process.argv.find((x) => x.startsWith(`--${name}=`));
  return a ? a.slice(name.length + 3) : dflt;
};
const profile = opt("profile", "");
if (profile) app.setPath("userData", profile); // a throwaway folder, never the user's %APPDATA%
const [w, h] = opt("size", "1000x320").split("x").map((n) => parseInt(n, 10));

app.whenReady().then(() => {
  const win = new BrowserWindow({
    width: w, height: h, autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, sandbox: true },
  });
  win.loadURL(opt("url", "about:blank"));
});
app.on("window-all-closed", () => app.quit());
