// End-to-end UI check on the real desktop, with the mock engine.
//   node ui/dev/verify.js [--force]
// Starts a plain text window (tests/target_window.py) as "the app the user types in", starts the UI
// (--mock --hidden) with a DevTools port, drives it, takes real screenshots into tests/out/ui_*.png and
// checks that the overlays never take the foreground. Results: tests/out/ui_verify.json.
// It clicks into the test window (to make it the foreground) and opens the control panel.
"use strict";
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");
const { connectPage, connectMain } = require("./cdp");

const UI = path.resolve(__dirname, "..");
const ROOT = path.resolve(UI, "..");
// LIVETYPE_ELECTRON / LIVETYPE_PYTHON, else the copies installed in the repository
const ELECTRON =
  process.env.LIVETYPE_ELECTRON || path.join(UI, "node_modules", "electron", "dist", "electron.exe");
const PY = process.env.LIVETYPE_PYTHON || path.join(ROOT, ".venv", "Scripts", "python.exe");
const OUT = path.join(ROOT, "tests", "out");
const PORT = 9333; // renderer pages (DevTools)
const MAIN_PORT = 9339; // main process (Node inspector)
const TARGET_TITLE = "LiveType — тестовое окно";
const T = {
  capsule: "LiveType — капсула",
  toast: "LiveType — уведомление",
  panel: "LiveType",
};

const results = { started: new Date().toISOString(), checks: [], screenshots: [] };
let failures = 0;

function check(name, ok, detail) {
  results.checks.push({ name, ok: Boolean(ok), detail: detail ?? null });
  if (!ok) failures += 1;
  const d = detail === undefined ? "" : `  ${typeof detail === "string" ? detail : JSON.stringify(detail)}`;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${d}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// persistent desktop helper (ui/dev/wintool.py serve)
class Desk {
  constructor() {
    this.proc = spawn(PY, ["-X", "utf8", path.join(__dirname, "wintool.py"), "serve"], { stdio: ["pipe", "pipe", "inherit"] });
    this.seq = 0;
    this.waiters = new Map();
    readline.createInterface({ input: this.proc.stdout }).on("line", (line) => {
      const msg = JSON.parse(line);
      const w = this.waiters.get(msg.id);
      if (w) {
        this.waiters.delete(msg.id);
        w(msg.result);
      }
    });
  }
  call(cmd, ...args) {
    const id = ++this.seq;
    this.proc.stdin.write(`${JSON.stringify({ id, cmd, args })}\n`);
    return new Promise((resolve) => this.waiters.set(id, resolve));
  }
  info(title) {
    return this.call("info", title);
  }
  fg() {
    return this.call("fg");
  }
  async grab(name, box) {
    const file = path.join(OUT, name);
    await this.call("grab", file, ...box.map((v) => Math.round(v)));
    results.screenshots.push(name);
    console.log(`      screenshot ${name}${fs.existsSync(file) ? "" : " (missing!)"}`);
  }
  // pixels darker than `threshold` in the band [top, bottom) of a screenshot: did the overlay paint?
  async ink(name, threshold, top = 0, bottom = 1) {
    const r = await this.call("ink", path.join(OUT, name), threshold, top, bottom);
    return r && typeof r.ink === "number" ? r.ink : -1;
  }
  close() {
    this.proc.stdin.end();
  }
}

const pad = (r, p) => [r[0] - p, r[1] - p, r[2] + p, r[3] + p];
const union = (a, b) => [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])];

async function waitFor(fn, timeoutMs, stepMs = 50) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    const v = await fn();
    if (v) return v;
    await sleep(stepMs);
  }
  return null;
}

function electronRunning() {
  const r = spawnSync("tasklist", ["/FI", "IMAGENAME eq electron.exe", "/FO", "CSV", "/NH"], { encoding: "utf8" });
  return /electron\.exe/i.test(r.stdout || "");
}
function pidAlive(pid) {
  if (!pid) return false;
  const r = spawnSync("tasklist", ["/FI", `PID eq ${pid}`, "/FO", "CSV", "/NH"], { encoding: "utf8" });
  return (r.stdout || "").includes(`"${pid}"`);
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  if (electronRunning() && !process.argv.includes("--force")) {
    console.log("An electron.exe is already running (LiveType UI?). Quit it first, or pass --force.");
    process.exit(2);
  }
  const desk = new Desk();

  // 1. the app the user types in: a plain text window behind the capsule area
  const screen = await desk.call("screen");
  const W = 900;
  const H = 640;
  const x = Math.round((screen.width - W) / 2);
  const y = Math.max(0, screen.height - H - 40);
  const target = spawn(
    PY,
    ["-X", "utf8", path.join(ROOT, "tests", "target_window.py"), path.join(os.tmpdir(), "lt_ui_target.txt"), String(x), String(y), String(W), String(H)],
    { stdio: "ignore" }
  );
  const targetInfo = await waitFor(async () => {
    const i = await desk.info(TARGET_TITLE);
    return i && i.hwnd ? i : null;
  }, 15000);
  check("test window opened", Boolean(targetInfo));
  await sleep(600);
  const focused = await desk.call("focus", TARGET_TITLE);
  check("test window is the foreground", focused && focused.ok, focused && focused.fg && focused.fg.title);
  const targetHwnd = targetInfo && targetInfo.hwnd;
  const fgIsTarget = async (label) => {
    const f = await desk.fg();
    check(`foreground unchanged: ${label}`, f && f.hwnd === targetHwnd, f && f.title);
  };

  // 2. the UI, hidden in the tray, mock engine, DevTools port
  const t0 = Date.now();
  const ui = spawn(
    ELECTRON,
    [
      `--inspect=${MAIN_PORT}`,
      UI,
      "--mock",
      "--hidden",
      "--mock-ready-delay=2500",
      "--mock-processing-ms=5000",
      `--remote-debugging-port=${PORT}`,
    ],
    { stdio: "ignore" }
  );
  const uiExit = new Promise((resolve) => ui.once("exit", (code) => resolve(code)));
  const capsule = await connectPage(PORT, "overlay/capsule.html");
  const mainProc = await connectMain(MAIN_PORT);
  // count cue playback (start/stop sounds) in the capsule page
  await capsule.eval(
    "window.__cues = []; const p = HTMLMediaElement.prototype.play; HTMLMediaElement.prototype.play = function () { window.__cues.push(this.src.split('/').pop()); return p.call(this); }; true"
  );
  await fgIsTarget("UI started (hidden in the tray)");

  // 3. engine ready, no startup pop-up (notices only go to the log)
  const ready = await waitFor(async () => await capsule.eval("window.livetype.getState().then(s => s.ready)"), 15000);
  check("engine (mock) ready", ready, `${Date.now() - t0} ms after launch`);
  const toastUp = await waitFor(async () => (await desk.info(T.toast)).visible, 2000);
  check("no startup pop-up", !toastUp);

  // 4. dictation: listening — only the capsule, no text bubble
  const before = await desk.fg();
  await capsule.eval('window.livetype.send({cmd: "toggle"})');
  const capVisible = await waitFor(async () => (await desk.info(T.capsule)).visible, 3000);
  check("capsule shown on listening", capVisible);
  await sleep(500);
  const after = await desk.fg();
  check(
    "GetForegroundWindow before == after showing the capsule",
    before && after && before.hwnd === after.hwnd && after.hwnd === targetHwnd,
    `${before && before.title} -> ${after && after.title}`
  );
  const capInfo = await desk.info(T.capsule);
  check("capsule: never activates, click-through, topmost", capInfo.noactivate && capInfo.transparent && capInfo.topmost);
  const capInks = [];
  for (let i = 1; i <= 3; i += 1) {
    await desk.grab(`ui_capsule_listening_${i}.png`, pad(capInfo.rect, 30));
    capInks.push(await desk.ink(`ui_capsule_listening_${i}.png`, 100));
    await sleep(180);
  }
  check("capsule paints while listening (3 frames)", capInks.every((v) => v > 2000), `dark pixels ${capInks.join(", ")}`);
  await sleep(2500);
  const titles = await mainProc.eval("global.__livetypeTest.windows()");
  check(
    "only the capsule while listening: no text bubble window",
    Array.isArray(titles) && titles.every((t) => Object.values(T).includes(t)) && !titles.some((t) => /превью/i.test(t)),
    titles
  );
  await desk.grab("ui_listening_capsule_only.png", pad(capInfo.rect, 160));
  await fgIsTarget("capsule shown");

  // 5. processing: the capsule morphs into the thinking state
  await capsule.eval('window.livetype.send({cmd: "toggle"})');
  await sleep(900);
  const st = await capsule.eval("window.livetype.getState().then(s => s.dictation)");
  check("state is processing", st === "processing", st);
  const capInfo2 = await desk.info(T.capsule);
  check("capsule visible while processing", capInfo2.visible);
  await desk.grab("ui_capsule_thinking_1.png", pad(capInfo2.rect, 30));
  await sleep(400);
  await desk.grab("ui_capsule_thinking_2.png", pad(capInfo2.rect, 30));
  const thinkInk = [await desk.ink("ui_capsule_thinking_1.png", 100), await desk.ink("ui_capsule_thinking_2.png", 100)];
  check("capsule paints while thinking", thinkInk.every((v) => v > 2000), `dark pixels ${thinkInk.join(", ")}`);
  await fgIsTarget("processing");

  // 6. done: capsule hides, no pop-up (notices only go to the log, as in Type)
  const capHidden = await waitFor(async () => !(await desk.info(T.capsule)).visible, 7000);
  check("capsule hidden after processing", capHidden);
  const toast2 = await waitFor(async () => (await desk.info(T.toast)).visible, 2500);
  check("no pop-up after the dictation", !toast2);
  const cues = await capsule.eval("window.__cues");
  check("cues: start on listening, stop when it ends", JSON.stringify(cues) === '["tap_01.wav","tap_02.wav"]', cues);
  await fgIsTarget("after the whole dictation");

  // 6b. «Показывать капсулу» off: nothing on screen while listening
  const dictation = () => capsule.eval("window.livetype.getState().then(s => s.dictation)");
  await capsule.eval('window.livetype.send({cmd: "set_settings", settings: {show_capsule: false}})');
  await sleep(200);
  await capsule.eval('window.livetype.send({cmd: "toggle"})');
  const listeningWithout = await waitFor(async () => (await dictation()) === "listening", 4000);
  await sleep(1500);
  const capsuleWithout = (await desk.info(T.capsule)).visible;
  check("show_capsule=false: dictation runs, capsule stays hidden", listeningWithout && !capsuleWithout, {
    listening: Boolean(listeningWithout),
    capsule: capsuleWithout,
  });
  await capsule.eval('window.livetype.send({cmd: "cancel"})');
  await waitFor(async () => (await dictation()) === "idle", 3000);
  await capsule.eval('window.livetype.send({cmd: "set_settings", settings: {show_capsule: true}})');
  await fgIsTarget("show_capsule=false round");

  // 7. second launch -> single-instance lock -> control panel opens in the first instance
  const s0 = Date.now();
  const second = spawnSync(ELECTRON, [UI, "--mock"], { timeout: 15000 });
  check("second launch exits at once (single-instance lock)", second.status === 0, `exit ${second.status} after ${Date.now() - s0} ms`);
  const panelShown = await waitFor(async () => (await desk.info(T.panel)).visible, 6000);
  check("control panel opened by the second launch", panelShown);
  const panel = await connectPage(PORT, "panel/index.html");
  await sleep(800);
  const panelBox = async () => (await desk.info(T.panel)).frame; // without invisible resize borders
  await desk.grab("ui_panel_top.png", await panelBox());
  const headline = await panel.eval('document.getElementById("headline-title").textContent');
  check("panel headline", headline === "Всё готово", headline);

  // dictation mode changes the subtitle; microphones come from the engine; default rule chips toggle
  await panel.eval('(() => { const m = document.getElementById("mode-select"); m.value = "hold"; m.dispatchEvent(new Event("change")); return true; })()');
  await sleep(300);
  const holdText = await panel.eval('[document.getElementById("mode-label").textContent, document.getElementById("headline-desc").textContent]');
  const holdMode = await panel.eval("window.livetype.getState().then(s => s.settings.activation_mode)");
  check(
    "«Способ диктовки» → Удерживать хоткей (setting + subtitle)",
    holdMode === "hold" && holdText[0] === "Удерживать хоткей" && holdText[1].startsWith("Держите хоткей, пока говорите."),
    holdText
  );
  await panel.eval('(() => { const m = document.getElementById("mode-select"); m.value = "toggle"; m.dispatchEvent(new Event("change")); return true; })()');
  const micOptions = await panel.eval('[...document.getElementById("mic-select").options].map(o => o.textContent)');
  check("microphones: «Системный» + engine devices", micOptions.length === 4 && micOptions[0] === "Системный", micOptions);
  const modelOptions = await panel.eval('[...document.getElementById("model-select").options].filter(o => o.value).map(o => o.textContent)');
  check("model options show label and size", modelOptions[0] === "Лёгкая · 563 МБ" && modelOptions[1] === "Точная · 2,4 ГБ — скачать", modelOptions);
  await panel.eval('[...document.querySelectorAll(".chip")].find(c => c.textContent === "С маленькой буквы").click()');
  await sleep(300);
  const lowercase = await panel.eval("window.livetype.getState().then(s => s.settings.text_rules.find(r => r.id === 'lowercase-start'))");
  check("rule chip toggles text_rules[].enabled", lowercase && lowercase.enabled === true, lowercase);
  await panel.eval('[...document.querySelectorAll(".chip")].find(c => c.textContent === "С маленькой буквы").click()');
  await sleep(200);

  // 8. bottom of the page: «Внешний вид и звуки» collapsed (stacked card), then expanded
  await panel.eval('document.getElementById("scroll").scrollTop = 1e6');
  await sleep(300);
  await desk.grab("ui_panel_bottom.png", await panelBox());
  await panel.eval('document.getElementById("row-appearance").click()');
  await sleep(450);
  await panel.eval('document.getElementById("scroll").scrollTop = 1e6');
  await sleep(300);
  await desk.grab("ui_panel_expanded.png", await panelBox());
  const switches = await panel.eval(
    '[...document.querySelectorAll("#stack-body [role=switch]")].map(c => c.getAttribute("aria-label") + "=" + c.getAttribute("aria-checked"))'
  );
  check("appearance switches visible", switches.length === 2, switches);
  await panel.eval('document.querySelector("[data-setting=show_capsule]").click()');
  await sleep(400);
  const showCapsule = await panel.eval("window.livetype.getState().then(s => s.settings.show_capsule)");
  check("switch «Показывать капсулу» sends set_settings", showCapsule === false, showCapsule);
  await panel.eval('document.querySelector("[data-setting=show_capsule]").click()');
  await sleep(300);

  // 9. hotkey capture
  await panel.eval('document.getElementById("scroll").scrollTop = 0');
  await sleep(200);
  await panel.eval('document.getElementById("row-hotkey").click()');
  await sleep(200);
  const capturing = await panel.eval('document.getElementById("hotkey-label").textContent');
  check("hotkey chip shows «Нажмите клавиши…»", capturing === "Нажмите клавиши…", capturing);
  const capTitle = await panel.eval('document.getElementById("headline-title").textContent');
  check("headline explains which keys work while recording", capTitle === "Новый хоткей", capTitle);
  await desk.grab("ui_panel_hotkey.png", await panelBox());
  const captured = await waitFor(async () => {
    const t = await panel.eval('document.getElementById("hotkey-label").textContent');
    return t !== "Нажмите клавиши…" ? t : null;
  }, 5000);
  check("hotkey_captured updates the chip", captured === "Ctrl+Shift+Space", captured);

  // 10. download of the accurate model
  await panel.eval('(() => { const s = document.getElementById("model-select"); s.value = "accurate"; s.dispatchEvent(new Event("change")); return true; })()');
  await sleep(1200);
  const modelLabel = await panel.eval('document.getElementById("model-label").textContent');
  check("model row shows download progress", /^Скачиваем • \d+%$/.test(modelLabel), modelLabel);
  await desk.grab("ui_panel_download.png", await panelBox());
  const downloaded = await waitFor(async () => {
    const s = await panel.eval("window.livetype.getState().then(s => [s.models.accurate.installed, s.settings.text_model])");
    return s[0] && s[1] === "accurate" ? s : null;
  }, 20000, 300);
  check("after the download text_model = accurate", Boolean(downloaded), downloaded);

  // 11. custom rule dialog
  await panel.eval('document.querySelector(".chip-add").click()');
  await sleep(200);
  await panel.eval('(() => { const i = document.getElementById("dialog-input"); i.value = "Менять GitHub на гитхаб"; i.dispatchEvent(new Event("input")); return true; })()');
  await sleep(200);
  await desk.grab("ui_panel_dialog.png", await panelBox());
  await panel.eval('document.getElementById("dialog-submit").click()');
  await sleep(400);
  const rules = await panel.eval("window.livetype.getState().then(s => s.settings.text_rules)");
  const custom = rules.find((r) => r.custom === true);
  check("custom rule appended", custom && /^custom-\d+$/.test(custom.id) && custom.enabled === true, custom);

  // 12. history
  await panel.eval('document.getElementById("row-history").click()');
  await sleep(600);
  await desk.grab("ui_panel_history.png", await panelBox());
  const entries = await panel.eval('document.querySelectorAll(".history-entry").length');
  check("history list shows the engine items", entries >= 3, entries);
  await panel.eval('document.getElementById("history-back").click()');
  await sleep(300);

  // 13. tray: menu, tooltip, click and «Открыть панель управления» bring the hidden panel back
  const trayInfo = await mainProc.eval(
    "(() => { const t = global.__livetypeTest; return { labels: t.menu.items.map(i => i.type === 'separator' ? '—' : i.label), tooltip: t.tooltip, destroyed: t.tray.isDestroyed() }; })()"
  );
  check(
    "tray icon with menu «Открыть панель управления» / «Выход»",
    !trayInfo.destroyed && JSON.stringify(trayInfo.labels) === '["Открыть панель управления","—","Выход"]',
    trayInfo
  );
  await panel.eval("window.livetype.windowClose()");
  const panelHidden = await waitFor(async () => !(await desk.info(T.panel)).visible, 3000);
  check("× hides the panel to the tray", panelHidden);
  await mainProc.eval("global.__livetypeTest.tray.emit('click'); true");
  const backByClick = await waitFor(async () => (await desk.info(T.panel)).visible, 3000);
  check("tray click opens the panel", backByClick);
  await panel.eval("window.livetype.windowClose()");
  await waitFor(async () => !(await desk.info(T.panel)).visible, 3000);
  await mainProc.eval("global.__livetypeTest.menu.items[0].click(); true");
  const backByMenu = await waitFor(async () => (await desk.info(T.panel)).visible, 3000);
  check("tray menu «Открыть панель управления» opens the panel", backByMenu);

  // 14. «Выход» from the tray: the app quits and stops the engine process
  const enginePid = await panel.eval("window.livetype.getState().then(s => s.enginePid)");
  // detach the inspector first: Node keeps a process alive while a debugger client is attached
  await mainProc.eval("setTimeout(() => global.__livetypeTest.menu.items[2].click(), 500); true");
  mainProc.close();
  const code = await Promise.race([uiExit, sleep(8000).then(() => "timeout")]);
  check("tray «Выход» quits the app", code !== "timeout", `exit ${code}`);
  await sleep(300);
  check("engine process stopped on quit", !pidAlive(enginePid), `pid ${enginePid}`);

  for (const s of [panel, capsule]) s.close();
  try {
    target.kill();
  } catch {
    /* ignore */
  }

  // 15. an engine that keeps crashing: 3 restarts, then «Не удалось запустить» + «Повторить» and a toast
  await sleep(800);
  const ui2 = spawn(ELECTRON, [UI, "--mock", "--mock-crash-after=0.2", `--remote-debugging-port=${PORT}`], { stdio: "ignore" });
  const ui2Exit = new Promise((resolve) => ui2.once("exit", (c) => resolve(c)));
  const panel2 = await connectPage(PORT, "panel/index.html");
  const failed = await waitFor(() => panel2.eval("window.livetype.getState().then(s => s.engine === 'failed')"), 15000, 200);
  check("crashing engine: restarted 3 times, then failed", failed);
  await sleep(550);
  const errToast = await desk.info(T.toast);
  check("error toast shown", errToast.visible);
  if (errToast.visible) await desk.grab("ui_toast_error.png", pad(errToast.rect, 4));
  const failText = await panel2.eval(
    '[document.getElementById("headline-title").textContent, !!document.querySelector("#headline-desc .retry")]'
  );
  check("panel offers «Повторить»", failText[0] === "Не удалось запустить" && failText[1], failText);
  await waitFor(async () => !(await desk.info(T.toast)).visible, 6000);
  await desk.grab("ui_panel_failed.png", await panelBox());
  panel2.eval("window.livetype.quitApp()").catch(() => {});
  const code2 = await Promise.race([ui2Exit, sleep(8000).then(() => "timeout")]);
  check("app quits (failure scenario)", code2 !== "timeout", `exit ${code2}`);
  panel2.close();
  desk.close();
  results.finished = new Date().toISOString();
  results.failures = failures;
  fs.writeFileSync(path.join(OUT, "ui_verify.json"), JSON.stringify(results, null, 2));
  console.log(`\n${failures ? `${failures} check(s) failed` : "all checks passed"}; screenshots in ${OUT}`);
  process.exit(failures ? 1 : 0);
}

main().catch((error) => {
  console.error(error);
  results.error = String(error && error.stack);
  try {
    fs.writeFileSync(path.join(OUT, "ui_verify.json"), JSON.stringify(results, null, 2));
  } catch {
    /* ignore */
  }
  process.exit(1);
});
