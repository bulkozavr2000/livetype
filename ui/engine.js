// Engine process: JSON lines over stdio. stdout lines are events, stdin lines are commands.
// Restarts a crashed engine up to `maxRestarts` times (the count resets after `stableMs` of uptime).
"use strict";
const { spawn, spawnSync } = require("child_process");
const { EventEmitter } = require("events");
const readline = require("readline");

class EngineProcess extends EventEmitter {
  /**
   * @param {object} opts
   * @param {string} opts.command  executable
   * @param {string[]} opts.args
   * @param {string} [opts.cwd]
   * @param {object} [opts.env]
   * @param {number} [opts.maxRestarts=3]
   * @param {number} [opts.restartDelayMs=1000]
   * @param {number} [opts.stableMs=120000]  uptime after which the restart count is forgotten
   * @param {(line: string) => void} [opts.log]
   *
   * Events: "event" (parsed JSON object), "status" (status, info), "stderr" (line)
   * Statuses: starting · running · restarting · failed · stopped
   */
  constructor(opts) {
    super();
    this.opts = { maxRestarts: 3, restartDelayMs: 1000, stableMs: 120000, ...opts };
    this.log = this.opts.log || (() => {});
    this.child = null;
    this.status = "stopped";
    this.restarts = 0;
    this.stopping = false;
    this.startedAt = 0;
    this.restartTimer = null;
  }

  get pid() {
    return this.child ? this.child.pid : null;
  }

  start() {
    this.stopping = false;
    this.restarts = 0;
    this.spawnChild();
  }

  // Manual restart (e.g. «Повторить» in the panel): forgets earlier crashes.
  restart() {
    this.restarts = 0;
    clearTimeout(this.restartTimer);
    this.restartTimer = null;
    if (this.child) {
      this.manualRestart = true;
      this.killTree(this.child);
      return;
    }
    this.stopping = false;
    this.spawnChild();
  }

  setStatus(status, info = {}) {
    this.status = status;
    this.emit("status", status, info);
  }

  spawnChild() {
    const { command, args, cwd, env } = this.opts;
    this.setStatus("starting", { attempt: this.restarts });
    let child;
    try {
      child = spawn(command, args, {
        cwd,
        env,
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
      });
    } catch (error) {
      this.log(`engine spawn threw: ${error.message}`);
      this.onExit(null, null, error);
      return;
    }
    this.child = child;
    this.startedAt = Date.now();
    let exited = false;
    const finish = (code, signal, error) => {
      if (exited) return;
      exited = true;
      if (this.child === child) this.child = null;
      this.onExit(code, signal, error);
    };

    child.stdin.on("error", () => {}); // EPIPE when the engine dies first
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");

    readline.createInterface({ input: child.stdout }).on("line", (line) => {
      const text = line.trim();
      if (!text) return;
      let event;
      try {
        event = JSON.parse(text);
      } catch {
        this.log(`engine stdout (not JSON): ${text.slice(0, 200)}`);
        return;
      }
      if (event && typeof event === "object" && typeof event.event === "string") {
        if (this.status !== "running") this.setStatus("running", { pid: child.pid });
        this.emit("event", event);
      }
    });
    readline.createInterface({ input: child.stderr }).on("line", (line) => {
      this.emit("stderr", line);
    });

    child.on("error", (error) => {
      this.log(`engine process error: ${error.message}`);
      // spawn failure (e.g. ENOENT) never produces "exit"
      if (!child.pid) finish(null, null, error);
    });
    child.on("exit", (code, signal) => finish(code, signal, null));
  }

  onExit(code, signal, error) {
    const uptime = this.startedAt ? Date.now() - this.startedAt : 0;
    this.log(`engine exited code=${code} signal=${signal}${error ? ` error=${error.message}` : ""} uptime=${uptime}ms`);
    if (this.manualRestart) {
      this.manualRestart = false;
      this.stopping = false;
      this.spawnChild();
      return;
    }
    if (this.stopping) {
      this.setStatus("stopped", { code });
      return;
    }
    if (uptime >= this.opts.stableMs) this.restarts = 0;
    if (this.restarts < this.opts.maxRestarts) {
      this.restarts += 1;
      this.setStatus("restarting", { code, attempt: this.restarts, max: this.opts.maxRestarts });
      clearTimeout(this.restartTimer);
      this.restartTimer = setTimeout(() => {
        this.restartTimer = null;
        if (!this.stopping) this.spawnChild();
      }, this.opts.restartDelayMs);
      return;
    }
    this.setStatus("failed", { code, error: error ? error.message : null });
  }

  send(command) {
    const child = this.child;
    if (!child || !child.stdin || !child.stdin.writable) return false;
    try {
      child.stdin.write(`${JSON.stringify(command)}\n`);
      return true;
    } catch (error) {
      this.log(`engine write failed: ${error.message}`);
      return false;
    }
  }

  // Graceful stop: {"cmd":"quit"}, then kill the process tree after `timeoutMs`.
  stop(timeoutMs = 2000) {
    this.stopping = true;
    clearTimeout(this.restartTimer);
    this.restartTimer = null;
    const child = this.child;
    if (!child) {
      this.setStatus("stopped");
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        this.log("engine did not quit in time, killing it");
        this.killTree(child);
        resolve();
      }, timeoutMs);
      child.once("exit", done);
      if (!this.send({ cmd: "quit" })) {
        this.killTree(child);
      }
    });
  }

  // Last resort, synchronous (process "exit" handler).
  killNow() {
    this.stopping = true;
    clearTimeout(this.restartTimer);
    if (this.child) this.killTree(this.child);
  }

  killTree(child) {
    if (!child || child.exitCode !== null || child.signalCode !== null) return;
    if (process.platform === "win32" && child.pid) {
      const r = spawnSync("taskkill", ["/pid", String(child.pid), "/T", "/F"], { windowsHide: true });
      if (r.status === 0) return;
    }
    try {
      child.kill();
    } catch {
      /* already gone */
    }
  }
}

module.exports = { EngineProcess };
