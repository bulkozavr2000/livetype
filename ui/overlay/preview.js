// Live preview bubble: shows the engine's tentative text above the capsule.
// Like Type's transcription preview, it appears and disappears without animation; main hides the
// window 220 ms after a hide message, by then the page is already empty.
(function () {
  "use strict";
  const api = window.livetype;
  const card = document.getElementById("card");
  const dot = document.getElementById("dot");
  const bars = document.getElementById("bars");
  const label = document.getElementById("label");
  const scroll = document.getElementById("scroll");
  const textEl = document.getElementById("text");
  const fade = document.getElementById("fade");

  const LABELS = { live: "Слушаю...", cleanup: "Проверяю..." };

  // window height = card + the page padding (16 top + 32 bottom, see preview.css)
  function reportSize() {
    if (card.hidden) return;
    api.previewResize(Math.ceil(card.getBoundingClientRect().height) + 48);
  }
  new ResizeObserver(reportSize).observe(card);

  api.on("preview", (msg) => {
    if (!msg || !msg.visible) {
      card.hidden = true;
      textEl.textContent = "";
      return;
    }
    const mode = msg.mode === "cleanup" ? "cleanup" : "live";
    card.dataset.state = mode;
    dot.hidden = mode === "cleanup";
    bars.hidden = mode !== "cleanup";
    label.textContent = LABELS[mode];
    textEl.textContent = msg.text || "";
    const wasHidden = card.hidden;
    card.hidden = false;
    if (wasHidden) reportSize();
    requestAnimationFrame(() => {
      scroll.scrollTop = scroll.scrollHeight;
      fade.hidden = !(scroll.scrollHeight > scroll.clientHeight + 2);
    });
  });
})();
