// Minimal Chrome DevTools Protocol client (Node 22+: global WebSocket). Dev/test use only.
"use strict";
const http = require("http");

function getJson(port, path) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: "127.0.0.1", port, path, timeout: 2000 }, (res) => {
      let data = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          resolve(JSON.parse(data));
        } catch (error) {
          reject(error);
        }
      });
    });
    req.on("timeout", () => req.destroy(new Error("timeout")));
    req.on("error", reject);
  });
}

class Session {
  constructor(ws) {
    this.ws = ws;
    this.seq = 0;
    this.pending = new Map();
    ws.onmessage = (event) => {
      const msg = JSON.parse(typeof event.data === "string" ? event.data : event.data.toString());
      const waiter = msg.id && this.pending.get(msg.id);
      if (!waiter) return;
      this.pending.delete(msg.id);
      if (msg.error) waiter.reject(new Error(msg.error.message));
      else waiter.resolve(msg.result);
    };
  }

  static connect(url) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      ws.onopen = () => resolve(new Session(ws));
      ws.onerror = () => reject(new Error(`cannot connect ${url}`));
    });
  }

  send(method, params = {}) {
    const id = ++this.seq;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error(`${method} timed out`));
      }, 15000);
    });
  }

  async eval(expression) {
    const r = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) {
      const d = r.exceptionDetails;
      throw new Error((d.exception && d.exception.description) || d.text);
    }
    return r.result.value;
  }

  close() {
    try {
      this.ws.close();
    } catch {
      /* ignore */
    }
  }
}

// Waits for a page target whose URL contains `urlPart` and for its document to finish loading;
// returns a connected session.
async function connectPage(port, urlPart, timeoutMs = 20000) {
  const end = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < end) {
    try {
      const targets = await getJson(port, "/json/list");
      const t = targets.find((x) => x.type === "page" && x.url.includes(urlPart));
      if (t) {
        const session = await Session.connect(t.webSocketDebuggerUrl);
        while (Date.now() < end) {
          if ((await session.eval("document.readyState")) === "complete") return session;
          await new Promise((r) => setTimeout(r, 100));
        }
        session.close();
        break;
      }
    } catch (error) {
      lastError = error;
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`page ${urlPart} not ready (${lastError ? lastError.message : "timeout"})`);
}

// The Electron main process, started with --inspect=<port>.
async function connectMain(port, timeoutMs = 20000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try {
      const targets = await getJson(port, "/json/list");
      if (targets.length) return await Session.connect(targets[0].webSocketDebuggerUrl);
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error("main-process inspector not reachable");
}

module.exports = { getJson, Session, connectPage, connectMain };
