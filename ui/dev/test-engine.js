// Unit checks for engine.js (no Electron needed):   node ui/dev/test-engine.js
// Fake engines are small Node scripts run with this same node binary.
"use strict";
const assert = require("assert");
const { EngineProcess } = require("../engine");

const NODE = process.execPath;
const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

function engineFor(script, extra = {}) {
  return new EngineProcess({ command: NODE, args: ["-e", script], env, restartDelayMs: 50, ...extra });
}

function collect(engine) {
  const statuses = [];
  const events = [];
  engine.on("status", (s, info) => statuses.push([s, info]));
  engine.on("event", (e) => events.push(e));
  return { statuses, events };
}

const until = (fn, ms = 5000) =>
  new Promise((resolve, reject) => {
    const end = Date.now() + ms;
    const tick = () => {
      if (fn()) return resolve();
      if (Date.now() > end) return reject(new Error("timeout"));
      setTimeout(tick, 20);
    };
    tick();
  });

const tests = {
  async "crash: restarts 3 times, then failed"() {
    const e = engineFor("process.exit(1)");
    const { statuses } = collect(e);
    e.start();
    await until(() => statuses.some(([s]) => s === "failed"));
    const names = statuses.map(([s]) => s);
    assert.deepStrictEqual(names, [
      "starting", "restarting", "starting", "restarting", "starting", "restarting", "starting", "failed",
    ]);
    assert.deepStrictEqual(
      statuses.filter(([s]) => s === "restarting").map(([, i]) => i.attempt),
      [1, 2, 3]
    );
  },

  async "events: JSON lines parsed, other lines ignored, status running"() {
    const e = engineFor(
      'console.log("not json"); console.log(JSON.stringify({event:"ready",version:"t"})); console.log(JSON.stringify({event:"level",value:0.5})); setTimeout(()=>{}, 2000)'
    );
    const { statuses, events } = collect(e);
    e.start();
    await until(() => events.length >= 2);
    assert.deepStrictEqual(events.map((x) => x.event), ["ready", "level"]);
    assert.ok(statuses.some(([s]) => s === "running"));
    await e.stop(500);
  },

  async "commands go to stdin; quit stops without a restart"() {
    const script = `
      const rl = require("readline").createInterface({ input: process.stdin });
      rl.on("line", (l) => { const m = JSON.parse(l);
        if (m.cmd === "ping") console.log(JSON.stringify({ event: "pong", n: m.n }));
        if (m.cmd === "quit") process.exit(0); });`;
    const e = engineFor(script);
    const { statuses, events } = collect(e);
    e.start();
    await until(() => e.child);
    assert.strictEqual(e.send({ cmd: "ping", n: 7 }), true);
    await until(() => events.length === 1);
    assert.deepStrictEqual(events[0], { event: "pong", n: 7 });
    const t = Date.now();
    await e.stop(3000);
    assert.ok(Date.now() - t < 2500, "quit should be quick");
    await until(() => statuses.some(([s]) => s === "stopped"));
    assert.ok(!statuses.some(([s]) => s === "restarting"));
    assert.strictEqual(e.send({ cmd: "ping" }), false);
  },

  async "stop kills an engine that ignores quit"() {
    const e = engineFor("setInterval(() => {}, 1000)");
    const { statuses } = collect(e);
    e.start();
    await until(() => e.child);
    const pid = e.pid;
    const t = Date.now();
    await e.stop(400);
    assert.ok(Date.now() - t >= 350, "waited for the timeout");
    await until(() => statuses.some(([s]) => s === "stopped"));
    let alive = true;
    try {
      process.kill(pid, 0);
    } catch {
      alive = false;
    }
    assert.strictEqual(alive, false, "process killed");
  },

  async "missing executable: restarts, then failed"() {
    const e = new EngineProcess({ command: "Z:\\nope\\python.exe", args: [], env, restartDelayMs: 20 });
    const { statuses } = collect(e);
    e.start();
    await until(() => statuses.some(([s]) => s === "failed"));
    assert.strictEqual(statuses.filter(([s]) => s === "restarting").length, 3);
  },

  async "manual restart after failed spawns again"() {
    const e = engineFor("process.exit(2)");
    const { statuses } = collect(e);
    e.start();
    await until(() => statuses.some(([s]) => s === "failed"));
    statuses.length = 0;
    e.restart();
    await until(() => statuses.some(([s]) => s === "failed"));
    assert.strictEqual(statuses.filter(([s]) => s === "restarting").length, 3);
  },
};

(async () => {
  let failed = 0;
  for (const [name, fn] of Object.entries(tests)) {
    try {
      await fn();
      console.log(`PASS  ${name}`);
    } catch (error) {
      failed += 1;
      console.log(`FAIL  ${name}\n      ${error.stack}`);
    }
  }
  console.log(failed ? `${failed} failed` : "all passed");
  process.exit(failed ? 1 : 0);
})();
