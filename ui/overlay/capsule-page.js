// Capsule window: mounts Type's capsule while the engine is listening/processing, plays the cues.
(function () {
  "use strict";
  const api = window.livetype;
  const host = document.getElementById("host");
  const view = new window.TypeCapsule.CapsuleView(host, {
    onLog: (entry) => api.log(entry.level || "warn", entry.message, entry.meta),
  });

  // main -> {visible, phase, gen}. Main shows the window right after a visible message and waits
  // for capsuleHidden(gen) before hiding it, so the last frame on screen is an empty one.
  api.on("capsule", (msg) => {
    if (!msg) return;
    if (msg.visible) {
      view.mount(msg.phase);
      return;
    }
    view.unmount();
    requestAnimationFrame(() => requestAnimationFrame(() => api.capsuleHidden(msg.gen)));
  });

  api.on("level", (value) => view.setLevel(value));

  // Warm-up (not in Type): compile the five WebGL programs once at start-up on a throwaway canvas,
  // so the first dictation does not wait for the shader compiler; the GPU process keeps the binaries.
  setTimeout(() => {
    if (view.mounted) return;
    const { Lt, _n, vn } = window.TypeCapsule;
    const canvas = document.createElement("canvas");
    canvas.style.cssText = "position:fixed;left:0;top:0;width:168px;height:119px;opacity:0;pointer-events:none";
    document.body.appendChild(canvas);
    try {
      const renderer = new Lt(canvas);
      const machine = _n();
      for (const state of ["listening", "thinking"]) {
        machine.select(state);
        for (let i = 0; i < 30; i += 1) machine.tick(1 / 60, vn);
        renderer.render(machine, vn, 1 / 60);
      }
      const gl = renderer.gl;
      renderer.dispose();
      const lose = gl && gl.getExtension("WEBGL_lose_context");
      if (lose) lose.loseContext();
    } catch (error) {
      api.log("warn", "Capsule warm-up failed", { error: String(error && error.message) });
    }
    canvas.remove();
  }, 400);

  // Dictation cues, as in Type's src/utils/dictationCues.js: start = tap_01, stop = tap_02, volume 0.5.
  const CUE_VOLUME = 0.5;
  const CUES = { start: "../assets/sounds/tap_01.wav", stop: "../assets/sounds/tap_02.wav" };
  const cueAudio = new Map();

  function getCueAudio(url) {
    if (!cueAudio.has(url)) {
      const audio = new Audio(url);
      audio.preload = "auto";
      cueAudio.set(url, audio);
    }
    return cueAudio.get(url);
  }

  async function playCue(url) {
    try {
      const audio = getCueAudio(url);
      audio.pause();
      audio.currentTime = 0;
      audio.volume = CUE_VOLUME;
      await audio.play();
    } catch (error) {
      api.log("debug", "Failed to play dictation cue", { error: String(error && error.message) });
    }
  }

  Object.values(CUES).forEach(getCueAudio); // preload
  api.on("sound", (name) => {
    if (CUES[name]) playCue(CUES[name]);
  });
})();
