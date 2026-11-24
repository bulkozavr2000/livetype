// Control panel page: renders the UI state from main and sends engine commands.
(function () {
  "use strict";
  const api = window.livetype;
  const $ = (id) => document.getElementById(id);
  const root = $("root");
  const scroll = $("scroll");
  const dragRegion = $("drag-region");

  let S = null; // last state snapshot (see main.js snapshot())
  let view = "main";
  let expanded = false;
  let dialogIndex = null;
  const keys = {}; // render caches

  // ------------------------------------------------------------------------------------------------
  // helpers
  // ------------------------------------------------------------------------------------------------
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === false || v === null || v === undefined) continue;
        node.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const child of children) if (child !== null && child !== undefined) node.append(child);
    return node;
  }

  function onActivate(node, handler) {
    node.addEventListener("click", handler);
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        handler(e);
      }
    });
  }

  function changed(name, value) {
    const key = JSON.stringify(value);
    if (keys[name] === key) return false;
    keys[name] = key;
    return true;
  }

  // Hotkey names as Type shows them ("Right Alt", "Ctrl+Alt+D").
  const SINGLE_KEYS = {
    rightalt: "Right Alt",
    rightoption: "Right Alt",
    rightctrl: "Right Ctrl",
    rightcontrol: "Right Ctrl",
    rightshift: "Right Shift",
    rightwin: "Right Win",
    rightsuper: "Right Win",
    rightmeta: "Right Win",
    rightcmd: "Right Win",
    rightcommand: "Right Win",
  };
  const MODIFIERS = {
    ctrl: "Ctrl",
    control: "Ctrl",
    commandorcontrol: "Ctrl",
    alt: "Alt",
    option: "Alt",
    shift: "Shift",
    win: "Win",
    super: "Win",
    meta: "Win",
    cmd: "Win",
    command: "Win",
  };
  function prettyKey(part) {
    const p = part.trim();
    const low = p.toLowerCase();
    if (SINGLE_KEYS[low]) return SINGLE_KEYS[low];
    if (MODIFIERS[low]) return MODIFIERS[low];
    if (p.length === 1) return p.toUpperCase();
    if (/^f\d{1,2}$/i.test(p)) return p.toUpperCase();
    return p.charAt(0).toUpperCase() + p.slice(1);
  }
  function prettyHotkey(spec) {
    if (!spec) return "—";
    return String(spec).split("+").map(prettyKey).join("+");
  }

  // Type: sizes in МБ, from 1 ГБ with one decimal and a comma
  function formatSize(mb) {
    const n = Number(mb) || 0;
    if (n >= 1024) return `${(n / 1024).toFixed(1).replace(".", ",")} ГБ`;
    return `${Math.round(n)} МБ`;
  }

  function formatTime(t) {
    const d = typeof t === "number" ? new Date(t < 1e12 ? t * 1000 : t) : new Date(String(t));
    if (Number.isNaN(d.getTime())) return String(t ?? "");
    const now = new Date();
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    const hm = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return `Сегодня, ${hm}`;
    if (d.toDateString() === yesterday.toDateString()) return `Вчера, ${hm}`;
    return `${d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" })}, ${hm}`;
  }

  const PLUS_SVG =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="12" x2="11.988" y2="12"/><line x1="11.999" y1="11.995" x2="11.999" y2="4"/><line x1="12" y1="11.998" x2="12" y2="20"/><line x1="19.997" y1="11.999" x2="11.991" y2="11.999"/></svg>';

  // ------------------------------------------------------------------------------------------------
  // actions
  // ------------------------------------------------------------------------------------------------
  function setSetting(key, value) {
    if (!S) return;
    S.settings = { ...S.settings, [key]: value };
    render();
    api.send({ cmd: "set_settings", settings: { [key]: value } });
  }

  function startCapture() {
    if (!S || S.hotkeyCapture || !S.ready) return;
    S.hotkeyCapture = true;
    render();
    api.send({ cmd: "capture_hotkey" });
  }

  function toggleRule(index) {
    const rules = S.settings.text_rules.map((r, i) => (i === index ? { ...r, enabled: !r.enabled } : r));
    setSetting("text_rules", rules);
  }

  function nextCustomId(rules) {
    let n = 0;
    for (const r of rules) {
      const m = /^custom-(\d+)$/.exec(String(r.id));
      if (m) n = Math.max(n, Number(m[1]));
    }
    return `custom-${n + 1}`;
  }

  // ------------------------------------------------------------------------------------------------
  // render
  // ------------------------------------------------------------------------------------------------
  const MODE_LINES = {
    toggle: ["Нажмите хоткей и говорите.", "Нажмите ещё раз, чтобы закончить"],
    hold: ["Держите хоткей, пока говорите.", "Отпустите, чтобы закончить"],
  };

  function headline(s) {
    if (s.engine === "failed") return { title: "Не удалось запустить", retry: true };
    if (!s.ready) {
      const speech = (s.modelProgress || {}).speech;
      if (speech && !speech.done && !speech.error) {
        const pct = speech.total_mb ? Math.floor((100 * speech.downloaded_mb) / speech.total_mb) : 0;
        return { title: "Скачиваем модель", lines: [`Модель распознавания речи • ${pct}%`, "Это нужно один раз — дальше всё без интернета."] };
      }
      return { title: "Подготавливаем модель", lines: ["Запускаем распознавание речи.", "Это может занять несколько секунд."] };
    }
    const progress = s.modelProgress || {};
    const downloading = Object.keys(progress).find((m) => progress[m] && !progress[m].done && !progress[m].error);
    if (downloading) {
      const which = downloading === "light" ? "лёгкую" : downloading === "accurate" ? "точную" : "";
      return {
        title: "Скачиваем модель",
        lines: [`Скачиваем ${which ? `${which} ` : ""}модель обработки.`, "Распознавание речи уже работает."],
      };
    }
    if (s.hotkeyCapture) {
      return {
        title: "Новый хоткей",
        lines: ["Ctrl, Alt, Shift или Win вместе с клавишей,", "один Right Alt или F-клавиша. Esc — отмена"],
      };
    }
    if (s.hotkeyOk === false) {
      return { title: "Хоткей занят", lines: ["Его уже использует другая программа.", "Нажмите на хоткей ниже, чтобы сменить"] };
    }
    return { title: "Всё готово", lines: MODE_LINES[s.settings.activation_mode] || MODE_LINES.toggle };
  }

  function renderHeadline(s) {
    const h = headline(s);
    if (!changed("headline", h)) return;
    $("headline-title").textContent = h.title;
    const desc = $("headline-desc");
    desc.replaceChildren();
    if (h.retry) {
      const button = el("button", { class: "retry appshots-window-no-drag", type: "button" }, "Повторить");
      button.addEventListener("click", () => api.restartEngine());
      desc.append(button);
    } else {
      for (const line of h.lines) desc.append(el("span", { class: "line" }, line));
    }
  }

  function renderSwitches(s) {
    document.querySelectorAll("[role=switch][data-setting]").forEach((cell) => {
      cell.setAttribute("aria-checked", s.settings[cell.dataset.setting] ? "true" : "false");
    });
  }

  function renderHotkey(s) {
    $("hotkey-label").textContent = s.hotkeyCapture ? "Нажмите клавиши…" : prettyHotkey(s.settings.hotkey);
    $("row-hotkey").dataset.capturing = s.hotkeyCapture ? "true" : "false";
    $("row-hotkey").setAttribute("aria-label", `Хоткей: ${prettyHotkey(s.settings.hotkey)}. Нажмите, чтобы сменить`);
  }

  function renderMode(s) {
    const mode = s.settings.activation_mode === "hold" ? "hold" : "toggle";
    $("mode-label").textContent = mode === "hold" ? "Удерживать хоткей" : "Старт-стоп";
    $("mode-select").value = mode;
  }

  function renderMic(s) {
    const names = (Array.isArray(s.devices) ? s.devices : [])
      .map((d) => (typeof d === "string" ? d : d && d.name))
      .filter(Boolean);
    const select = $("mic-select");
    if (changed("mic-options", names)) {
      select.replaceChildren(el("option", { value: "" }, "Системный"), ...names.map((n) => el("option", { value: n }, n)));
    }
    const current = s.settings.microphone;
    const found = Boolean(current) && names.includes(current);
    select.value = found ? current : "";
    const label = found ? current : "Системный";
    $("mic-label").textContent = label;
    $("mic-label").title = label;
    select.title = label;
  }

  function renderModel(s) {
    const on = Boolean(s.settings.text_processing);
    $("row-model").hidden = !on;
    $("chips").hidden = !on;
    // the switch row is the last visible row when processing is off: no separator under it
    document.querySelector('[data-setting="text_processing"]').dataset.last = on ? "false" : "true";

    const models = s.models || {};
    const ids = Object.keys(models);
    const select = $("model-select");
    if (changed("model-options", models)) {
      select.replaceChildren(
        el("option", { value: "", disabled: true, hidden: true }, "Выбрать модель"),
        ...ids.map((id) =>
          el(
            "option",
            { value: id },
            `${models[id].label || id} · ${formatSize(models[id].size_mb)}${models[id].installed ? "" : " — скачать"}`
          )
        )
      );
    }
    const current = s.pendingModel || s.settings.text_model;
    select.value = models[current] ? current : "";

    const progress = s.modelProgress || {};
    const active =
      s.pendingModel && progress[s.pendingModel] && !progress[s.pendingModel].error
        ? s.pendingModel
        : Object.keys(progress).find((m) => progress[m] && !progress[m].done && !progress[m].error);
    const failed = Object.keys(progress).find((m) => progress[m] && progress[m].error);
    const label = $("model-label");
    if (active) {
      const p = progress[active];
      const pct = p.total_mb ? Math.min(100, Math.round((p.downloaded_mb / p.total_mb) * 100)) : 0;
      label.textContent = `Скачиваем • ${pct}%`;
      label.dataset.error = "false";
      label.setAttribute("role", "progressbar");
      label.setAttribute("aria-label", "Скачиваем модель");
      label.setAttribute("aria-valuemin", "0");
      label.setAttribute("aria-valuemax", "100");
      label.setAttribute("aria-valuenow", String(pct));
      select.disabled = true;
    } else {
      label.removeAttribute("role");
      label.removeAttribute("aria-valuenow");
      if (failed) {
        label.textContent = "Не скачалась";
        label.dataset.error = "true";
        label.title = progress[failed].error || "";
      } else {
        const m = models[s.settings.text_model];
        label.textContent = m ? m.label || s.settings.text_model : "Выбрать модель";
        label.dataset.error = "false";
        label.title = "";
      }
      select.disabled = false;
    }
  }

  function renderChips(s) {
    const rules = Array.isArray(s.settings.text_rules) ? s.settings.text_rules : [];
    if (!changed("chips", rules)) return;
    const box = $("chips");
    const left = box.scrollLeft;
    box.replaceChildren();
    rules.forEach((rule, index) => {
      const label = rule.label != null ? String(rule.label) : String(rule.id);
      const chip = el("button", { class: "chip", type: "button" }, label);
      if (rule.custom === true) {
        chip.dataset.variant = rule.enabled === false ? "gray" : "filled";
        chip.setAttribute("aria-label", `Изменить пожелание: ${label}`);
        chip.addEventListener("click", () => openDialog(index));
      } else {
        chip.setAttribute("aria-pressed", rule.enabled ? "true" : "false");
        chip.addEventListener("click", () => toggleRule(index));
      }
      box.append(chip);
    });
    const add = el("button", {
      class: "chip chip-add",
      type: "button",
      "aria-label": "Добавить пожелание",
      title: "Добавить пожелание",
    });
    add.innerHTML = PLUS_SVG;
    add.addEventListener("click", () => openDialog(null));
    box.append(add);
    box.scrollLeft = left;
  }

  function renderHistory(s) {
    const list = $("history-list");
    if (!changed("history", [s.historyLoading, s.history])) return;
    list.replaceChildren();
    const items = s.history;
    if (!Array.isArray(items)) {
      list.append(el("div", { class: "history-empty" }, "Загружаем…"));
      return;
    }
    if (!items.length) {
      list.append(el("div", { class: "history-empty" }, "Пока пусто"));
      return;
    }
    for (const item of items) {
      const time = el("span", { class: "history-time" }, formatTime(item.time));
      const text = el("span", { class: "history-text" }, String(item.text ?? ""));
      const cell = el("div", { class: "cell history-entry", role: "button", tabindex: "0" }, el("div", { class: "cell-body" }, time, text));
      onActivate(cell, () => copyEntry(cell, time, item));
      list.append(cell);
    }
  }

  async function copyEntry(cell, time, item) {
    try {
      await api.copyText(item.text);
    } catch {
      return;
    }
    const before = formatTime(item.time);
    cell.dataset.copied = "true";
    time.textContent = "Скопировано";
    setTimeout(() => {
      cell.dataset.copied = "false";
      time.textContent = before;
    }, 1400);
  }

  function render() {
    if (!S) return;
    renderHeadline(S);
    renderHotkey(S);
    renderMode(S);
    renderMic(S);
    renderSwitches(S);
    renderModel(S);
    renderChips(S);
    if (view === "history") renderHistory(S);
    const build = $("build");
    build.title = [S.version ? `движок ${S.version}` : "", S.mock ? "демо-режим" : ""].filter(Boolean).join(" · ");
  }

  // ------------------------------------------------------------------------------------------------
  // events
  // ------------------------------------------------------------------------------------------------
  $("wc-min").addEventListener("click", () => api.windowMinimize());
  $("wc-close").addEventListener("click", () => api.windowClose());

  onActivate($("row-hotkey"), startCapture);
  $("mode-select").addEventListener("change", (e) => setSetting("activation_mode", e.target.value === "hold" ? "hold" : "toggle"));
  $("mic-select").addEventListener("change", (e) => setSetting("microphone", e.target.value || null));
  onActivate($("row-history"), openHistory);

  document.querySelectorAll("[role=switch][data-setting]").forEach((cell) => {
    onActivate(cell, () => {
      if (!S || cell.getAttribute("aria-disabled") === "true") return;
      const key = cell.dataset.setting;
      setSetting(key, !S.settings[key]);
    });
  });

  $("model-select").addEventListener("change", (e) => {
    const model = e.target.value;
    if (!S || !S.models[model]) return;
    if (S.models[model].installed) {
      setSetting("text_model", model);
      return;
    }
    S.pendingModel = model;
    S.modelProgress = {
      ...S.modelProgress,
      [model]: { downloaded_mb: 0, total_mb: S.models[model].size_mb || 0, done: false, error: null },
    };
    render();
    api.send({ cmd: "download_model", model });
  });

  // Type: vertical wheel scrolls the chip row sideways
  $("chips").addEventListener(
    "wheel",
    (e) => {
      const t = e.currentTarget;
      if (e.ctrlKey || Math.abs(e.deltaX) >= Math.abs(e.deltaY) || t.scrollWidth <= t.clientWidth) return;
      e.preventDefault();
      const n = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? t.clientWidth : 1;
      t.scrollLeft += e.deltaY * n;
    },
    { passive: false }
  );

  // appearance and sounds
  onActivate($("row-appearance"), toggleStack);
  function toggleStack() {
    const body = $("stack-body");
    const stack = $("stack");
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!expanded) {
      expanded = true;
      stack.dataset.expanded = "true";
      $("row-appearance").setAttribute("aria-expanded", "true");
      if (!reduce) {
        const h = body.scrollHeight;
        body.animate([{ height: "0px", opacity: 0 }, { height: `${h}px`, opacity: 1 }], { duration: 220, easing: "ease-out" });
      }
      return;
    }
    const finish = () => {
      expanded = false;
      stack.dataset.expanded = "false";
      $("row-appearance").setAttribute("aria-expanded", "false");
    };
    if (reduce) {
      finish();
      return;
    }
    const h = body.scrollHeight;
    const anim = body.animate([{ height: `${h}px`, opacity: 1 }, { height: "0px", opacity: 0 }], {
      duration: 180,
      easing: "ease-in",
    });
    anim.onfinish = finish;
  }

  // history
  function openHistory() {
    view = "history";
    root.dataset.view = "history";
    keys.history = undefined;
    $("view-main").hidden = true;
    $("view-history").hidden = false;
    scroll.scrollTop = 0;
    dragRegion.hidden = false;
    if (S) {
      S.historyLoading = true;
      renderHistory(S);
    }
    api.send({ cmd: "get_history" });
    setTimeout(() => $("history-back").focus(), 0);
  }
  function closeHistory() {
    view = "main";
    root.dataset.view = "main";
    $("view-history").hidden = true;
    $("view-main").hidden = false;
    scroll.scrollTop = 0;
    dragRegion.hidden = false;
  }
  $("history-back").addEventListener("click", closeHistory);

  // custom rule dialog
  const dialog = $("dialog");
  const input = $("dialog-input");
  function openDialog(index) {
    if (!S) return;
    dialogIndex = index;
    const rule = index === null ? null : S.settings.text_rules[index];
    $("dialog-title").textContent = index === null ? "Добавить правило" : "Изменить пожелание";
    input.value = rule ? String(rule.label ?? "") : "";
    $("dialog-error").hidden = true;
    $("dialog-delete").hidden = index === null;
    dialog.hidden = false;
    updateDialogButtons();
    setTimeout(() => input.focus(), 0);
  }
  function closeDialog() {
    dialog.hidden = true;
    dialogIndex = null;
  }
  function updateDialogButtons() {
    const empty = !input.value.trim();
    const isNew = dialogIndex === null;
    // Type: an empty new rule shows only «Закрыть»; otherwise «Добавить»/«Сохранить» (+ «Удалить» when editing)
    $("dialog-close").hidden = !(empty && isNew);
    $("dialog-submit").hidden = empty && isNew;
    $("dialog-submit").textContent = isNew ? "Добавить" : "Сохранить";
    $("dialog-submit").disabled = empty;
  }
  function saveRule(remove) {
    if (!S) return;
    const text = input.value.trim();
    if (!remove && !text) return;
    const rules = [...(S.settings.text_rules || [])];
    if (remove) {
      if (dialogIndex !== null) rules.splice(dialogIndex, 1);
    } else if (dialogIndex === null) {
      if (!rules.some((r) => r.custom === true && r.label === text)) {
        rules.push({ id: nextCustomId(rules), label: text, enabled: true, custom: true });
      }
    } else {
      rules[dialogIndex] = { ...rules[dialogIndex], label: text };
    }
    setSetting("text_rules", rules);
    closeDialog();
  }
  input.addEventListener("input", updateDialogButtons);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !input.value) {
      e.preventDefault();
      input.value = input.placeholder;
      updateDialogButtons();
    }
  });
  $("dialog-form").addEventListener("submit", (e) => {
    e.preventDefault();
    saveRule(false);
  });
  $("dialog-close").addEventListener("click", closeDialog);
  $("dialog-delete").addEventListener("click", () => saveRule(true));
  dialog.addEventListener("mousedown", (e) => {
    if (e.target === dialog) closeDialog();
  });

  // clicking away while a new hotkey is being recorded cancels the recording
  window.addEventListener("blur", () => {
    if (!S || !S.hotkeyCapture) return;
    S.hotkeyCapture = false;
    render();
    api.send({ cmd: "cancel_capture" });
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!dialog.hidden) closeDialog();
    else if (view === "history") closeHistory();
  });

  // Type shows the caption drag strip only while the page is scrolled to the top
  scroll.addEventListener("scroll", () => {
    dragRegion.hidden = scroll.scrollTop > 0;
  });

  // window height follows the content (Type: resizeControlPanelToContent)
  function applyMaxHeight() {
    const avail = window.screen && window.screen.availHeight ? window.screen.availHeight : 900;
    scroll.style.maxHeight = `${Math.max(360, avail - 48)}px`;
  }
  let sizeFrame = 0;
  function reportSize() {
    if (sizeFrame) return;
    sizeFrame = requestAnimationFrame(() => {
      sizeFrame = 0;
      api.panelResize(Math.ceil(root.getBoundingClientRect().height));
    });
  }
  applyMaxHeight();
  window.addEventListener("resize", applyMaxHeight);
  new ResizeObserver(reportSize).observe(root);

  window.addEventListener("focus", () => {
    if (S && S.ready) api.send({ cmd: "list_devices" });
  });

  api.on("state", (snapshot) => {
    const wasReady = S && S.ready;
    S = snapshot;
    render();
    if (S.ready && !wasReady) api.send({ cmd: "list_devices" });
  });
  api.getState().then((snapshot) => {
    if (!S) S = snapshot;
    render();
    reportSize();
    if (S.ready) api.send({ cmd: "list_devices" });
  });
})();
