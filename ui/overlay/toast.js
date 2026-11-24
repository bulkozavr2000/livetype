// Toast notice for engine "notice" events: slides in, stays ~4 s, slides out, then main hides the window.
(function () {
  "use strict";
  const api = window.livetype;
  const toast = document.getElementById("toast");
  const icon = document.getElementById("icon");
  const title = document.getElementById("title");
  const body = document.getElementById("body");

  // lucide icons as used by Type's toaster: CircleCheck, Info, TriangleAlert, OctagonX (size-4)
  const SVG_OPEN =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">';
  const ICONS = {
    success: `${SVG_OPEN}<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>`,
    info: `${SVG_OPEN}<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>`,
    warning: `${SVG_OPEN}<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>`,
    error: `${SVG_OPEN}<path d="m15 9-6 6"/><path d="M2.586 16.726A2 2 0 0 1 2 15.312V8.688a2 2 0 0 1 .586-1.414l4.688-4.688A2 2 0 0 1 8.688 2h6.624a2 2 0 0 1 1.414.586l4.688 4.688A2 2 0 0 1 22 8.688v6.624a2 2 0 0 1-.586 1.414l-4.688 4.688a2 2 0 0 1-1.414.586H8.688a2 2 0 0 1-1.414-.586z"/><path d="m9 9 6 6"/></svg>`,
  };
  const EXIT_MS = 400;

  let current = 0;
  let hideTimer = null;
  let doneTimer = null;

  function reportSize() {
    if (toast.hidden) return;
    api.toastResize(Math.ceil(toast.getBoundingClientRect().height) + 36);
  }
  new ResizeObserver(reportSize).observe(toast);

  api.on("toast", (msg) => {
    if (!msg) return;
    clearTimeout(hideTimer);
    clearTimeout(doneTimer);
    current = msg.id;
    const kind = ICONS[msg.kind] ? msg.kind : "info";
    toast.dataset.type = kind;
    icon.innerHTML = ICONS[kind];
    title.textContent = msg.title || "";
    body.textContent = msg.body || "";
    toast.hidden = false;
    toast.dataset.removed = "false";
    toast.dataset.mounted = "false";
    reportSize();
    // two frames so the start position is painted before the transition
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (current === msg.id) toast.dataset.mounted = "true";
      })
    );
    const id = msg.id;
    hideTimer = setTimeout(() => {
      toast.dataset.removed = "true";
      doneTimer = setTimeout(() => {
        if (current !== id) return;
        toast.hidden = true;
        toast.dataset.mounted = "false";
        api.toastDone(id);
      }, EXIT_MS);
    }, Number(msg.duration) || 4000);
  });
})();
