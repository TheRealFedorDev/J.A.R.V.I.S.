/* Berta web — статус, WS, камера+скелет, команды, профили, СТОП */
"use strict";

/* Отсутствующий элемент заменяется отсоединённой заглушкой: разметка может быть
   урезана (минимальный HUD), но скрипт продолжает работать без ошибок. */
const $ = (id) => document.getElementById(id) || document.createElement("div");

/* ═══════════ DOM ═══════════ */
const el = {
  statusPill: $("statusPill"),
  statusText: $("statusText"),
  profileTabs: $("profileTabs"),
  micBtn: $("micBtn"),
  ttsBtn: $("ttsBtn"),
  themeBtn: $("themeBtn"),
  contrastBtn: $("contrastBtn"),
  liveCaption: $("liveCaption"),
  liveTag: $("liveTag"),
  liveText: $("liveText"),
  log: $("log"),
  composer: $("composer"),
  cmdInput: $("cmdInput"),
  quickRow: $("quickRow"),
  auditList: $("auditList"),
  auditCount: $("auditCount"),
  camDock: $("camDock"),
  camImg: $("camImg"),
  camStage: $("camStage"),
  camPlaceholder: $("camPlaceholder"),
  camFps: $("camFps"),
  camFace: $("camFace"),
  camGesture: $("camGesture"),
  camCollapse: $("camCollapse"),
  camHead: $("camHead"),
  camResize: $("camResize"),
  skelCanvas: $("skelCanvas"),
  recenterTarget: $("recenterTarget"),
  btnCalibrate: $("btnCalibrate"),
  btnRecenter: $("btnRecenter"),
  btnResetOffset: $("btnResetOffset"),
  btnCursor: $("btnCursor"),
  emergencyBtn: $("emergencyBtn"),
  confirmModal: $("confirmModal"),
  confirmText: $("confirmText"),
  confirmYes: $("confirmYes"),
  confirmNo: $("confirmNo"),
  toast: $("toast"),
  taskToggleBtn: $("taskToggleBtn"),
  taskPanel: $("taskPanel"),
  taskCloseBtn: $("taskCloseBtn"),
  taskList: $("taskList"),
  taskCount: $("taskCount"),
  taskEmpty: $("taskEmpty"),
  taskForm: $("taskForm"),
  taskInput: $("taskInput"),
};

/* ═══════════ СОСТОЯНИЕ ═══════════ */
const state = {
  profile: "home",
  assist: false,
  emergency: false,
  cursor: true,
  mic: true,
  tts: true,
  streaming: null,      // DOM-элемент текущего стрим-пузыря
  pendingConfirm: null, // id
  statusOverride: null, // {state, text, until}
  lastPartialAt: 0,
  finalHoldUntil: 0,    // игнор interim-частичек после финала
  awaitingReply: false, // ждём ответа — не гасить плашку «ФРАЗА»
  tasks: null,          // список задач; null = ещё не приходил по WS
};

/* ═══════════ ТОСТ ═══════════ */
let toastTimer = null;
function toast(text, ms = 3200) {
  el.toast.textContent = text;
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, ms);
}

/* ═══════════ СТАТУС-ПИЛЛ ═══════════ */
function setStatus(stateName, text) {
  el.statusPill.dataset.state = stateName;
  el.statusText.textContent = text;
}
function overrideStatus(stateName, text, ms = 2500) {
  state.statusOverride = { stateName, text, until: Date.now() + ms };
  setStatus(stateName, text);
}
function refreshBaseStatus(tracker) {
  if (state.statusOverride && Date.now() < state.statusOverride.until) return;
  state.statusOverride = null;
  if (state.emergency) { setStatus("stop", "ЭКСТРЕННЫЙ СТОП"); return; }
  const s = (tracker && tracker.state) || "…";
  const map = {
    READY: ["idle", "ГОТОВА"],
    TRACKING: ["idle", "СЛЕЖУ"],
    CALIBRATING: ["do", "КАЛИБРОВКА…"],
    STARTING: ["boot", "ЗАПУСК…"],
    ERROR: ["error", "ОШИБКА"],
    STOPPED: ["error", "ОСТАНОВЛЕНА"],
  };
  const [st, txt] = map[s] || ["idle", s];
  setStatus(st, txt + (tracker && tracker.message ? ` · ${tracker.message}` : ""));
}

/* ═══════════ ЧАТ ═══════════ */
function addMsg(role, text, opts = {}) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}` + (opts.streaming ? " streaming" : "");
  const av = document.createElement("div");
  av.className = "avatar";
  av.textContent = role === "user" ? "Ф" : "B";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrap.appendChild(av);
  wrap.appendChild(bubble);
  el.log.appendChild(wrap);
  el.log.scrollTop = el.log.scrollHeight;
  return bubble;
}

function sendCommand(text) {
  const t = (text || "").trim();
  if (!t) return;
  addMsg("user", t);
  overrideStatus("think", "ДУМАЮ…", 20000);
  orbThinking();
  fetch("/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: t }),
  })
    .then((r) => r.json())
    .then((data) => {
      if (data.needs_confirm) {
        state.pendingConfirm = data.confirm_id;
        el.confirmText.textContent = data.reply || t;
        el.confirmModal.hidden = false;
        addMsg("bot", data.reply);
        orbIdle();
        return;
      }
      if (data.streaming) {
        // ответ придёт чанками по WS
        state.streaming = addMsg("bot", "", { streaming: true });
        return;
      }
      if (data.reply) addMsg("bot", data.reply);
      if (data.ok === false) toast(data.reply || "Ошибка");
      orbSpeaking();
    })
    .catch(() => addMsg("bot", "Сервер недоступен — переподключаюсь…"));
}

el.composer.addEventListener("submit", (e) => {
  e.preventDefault();
  sendCommand(el.cmdInput.value);
  el.cmdInput.value = "";
});

el.quickRow.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-cmd]");
  if (btn) sendCommand(btn.dataset.cmd);
});

/* ═══════════ ЖИВАЯ ПОДПИСЬ (частичный текст речи) ═══════════ */
function showLive(tag, text) {
  el.liveCaption.hidden = !text;
  el.liveTag.textContent = tag;
  el.liveText.textContent = text || "";
}

/* ═══════════ ИИ-ШАРИК (orb-ui, controlled) ═══════════ */
let _orbIdleTimer = null;
const _orbStateMap = {
  idle: "idle",
  thinking: "thinking",
  listening: "listening",
  speaking: "speaking",
  connecting: "connecting",
  error: "error",
};

const _pipelineEl = $("pipelineBar");

/* Порядок этапов = визуальный поток вокруг орба.
   on  — идёт прямо сейчас, past — этап позади. */
const _ACT_ORDER = ["stt", "search", "llm", "tts"];
const _ACT_ON = {
  listening: ["stt"],
  thinking: ["search", "llm"],
  speaking: ["tts"],
  error: _ACT_ORDER,
};

function _setPipeline(state) {
  // кольца активностей вокруг орба
  const acts = document.querySelectorAll(".act[data-act]");
  if (acts.length) {
    const on = _ACT_ON[state] || [];
    acts.forEach((a) => {
      const i = _ACT_ORDER.indexOf(a.dataset.act);
      a.classList.remove("on", "past");
      if (on.includes(a.dataset.act)) {
        a.classList.add("on");
      } else if (on.length && i >= 0 && i < _ACT_ORDER.indexOf(on[0])) {
        a.classList.add("past");
      }
    });
  }
  if (!_pipelineEl) return;
  _pipelineEl.hidden = state === "idle";
  const steps = _pipelineEl.querySelectorAll(".pipe-step");
  steps.forEach((s) => {
    s.classList.remove("active", "done");
    const step = s.dataset.step;
    if (state === "listening" && step === "stt") {
      s.classList.add("active");
    } else if (state === "thinking") {
      if (step === "stt") s.classList.add("done");
      if (step === "search" || step === "llm") s.classList.add("active");
    } else if (state === "speaking") {
      if (step === "search" || step === "llm") s.classList.add("done");
      if (step === "tts") s.classList.add("active");
    }
  });
}

function updateOrb(state, { inputVolume = 0, outputVolume = 0 } = {}) {
  document.body.dataset.orbState = state;
  _setPipeline(state);
  if (typeof window.__BertaOrb?.set === "function") {
    window.__BertaOrb.set({ state, inputVolume, outputVolume });
  }
}
function orbListening() { updateOrb("listening", { inputVolume: 0.3 }); }
function orbThinking()   { updateOrb("thinking"); }
function orbSpeaking()   {
  updateOrb("speaking", { outputVolume: 0.7 });
  clearTimeout(_orbIdleTimer);
  _orbIdleTimer = setTimeout(() => updateOrb("idle"), 5000);
}
function orbIdle()      { updateOrb("idle"); }

function updateOrbState(stateName, opts = {}) {
  const mapped = _orbStateMap[stateName] || "idle";
  updateOrb(mapped, opts);
  clearTimeout(_orbIdleTimer);
  _orbIdleTimer = setTimeout(() => {
    updateOrb("idle");
  }, 8000);
}

/* ═══════════ АУДИТ ═══════════ */
function renderAudit(entries) {
  el.auditList.innerHTML = "";
  for (const e of entries.slice(0, 40)) {
    const li = document.createElement("li");
    if (!e.ok) li.className = "fail";
    const time = new Date(e.ts * 1000).toLocaleTimeString("ru-RU");
    li.innerHTML = `<span class="src">${e.source}</span><b>${e.action}</b> ` +
      `<span style="float:right">${time}</span>` +
      (e.detail ? `<div>${escapeHtml(e.detail)}</div>` : "");
    el.auditList.appendChild(li);
  }
  el.auditCount.textContent = String(entries.length);
}

function pushAuditOne(entry) {
  const li = document.createElement("li");
  if (!entry.ok) li.className = "fail";
  const time = new Date(entry.ts * 1000).toLocaleTimeString("ru-RU");
  li.innerHTML = `<span class="src">${entry.source}</span><b>${entry.action}</b> ` +
    `<span style="float:right">${time}</span>` +
    (entry.detail ? `<div>${escapeHtml(entry.detail)}</div>` : "");
  el.auditList.prepend(li);
  while (el.auditList.children.length > 40) el.auditList.lastChild.remove();
  el.auditCount.textContent = String(el.auditList.children.length);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ═══════════ ПРОФИЛИ ═══════════ */
function applyProfile(p) {
  state.profile = p.current || p.id;
  state.assist = !!(p.assist);
  document.body.classList.toggle("profile-assist", state.assist);
  el.profileTabs.querySelectorAll("button").forEach((b) => {
    b.classList.toggle("active", b.dataset.profile === state.profile);
  });
}

el.profileTabs.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-profile]");
  if (!btn) return;
  fetch("/api/profile", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: btn.dataset.profile }),
  })
    .then((r) => r.json())
    .then((p) => {
      applyProfile(p);
      toast(`Профиль: ${p.title}`);
      addMsg("bot", `Профиль «${p.title}» включён.`);
    });
});

/* ═══════════ КАМЕРА: КАДР + СКЕЛЕТ ═══════════ */
// skelCanvas может отсутствовать (index.html без камеры): заглушка $() вернёт
// <div> без getContext. Такая строка УБИВАЕТ весь скрипт — ниже не привяжутся
// обработчики задач/микрофона/микши и не подключится WebSocket.
const ctx = typeof el.skelCanvas.getContext === "function"
  ? el.skelCanvas.getContext("2d")
  : null;
let handLandmarks = null;
let camHiddenSince = 0;

const HAND_CONNECTIONS = [
  [0,1],[1,2],[2,3],[3,4],
  [0,5],[5,6],[6,7],[7,8],
  [5,9],[9,10],[10,11],[11,12],
  [9,13],[13,14],[14,15],[15,16],
  [13,17],[17,18],[18,19],[19,20],
  [0,17],
];

function drawSkeleton() {
  const c = el.skelCanvas;
  const stage = el.camStage;
  if (!ctx || !c || !stage) return;
  if (c.width !== stage.clientWidth || c.height !== stage.clientHeight) {
    c.width = stage.clientWidth;
    c.height = stage.clientHeight;
  }
  ctx.clearRect(0, 0, c.width, c.height);
  if (!handLandmarks || handLandmarks.length < 21) return;

  // кадр зеркалился при публикации → x отражаем
  const pts = handLandmarks.map(([x, y]) => [(1 - x) * c.width, y * c.height]);

  ctx.lineWidth = Math.max(2, c.width / 160);
  ctx.strokeStyle = "rgba(0,229,255,0.9)";
  ctx.shadowColor = "rgba(0,229,255,0.8)";
  ctx.shadowBlur = 8;
  for (const [a, b] of HAND_CONNECTIONS) {
    ctx.beginPath();
    ctx.moveTo(pts[a][0], pts[a][1]);
    ctx.lineTo(pts[b][0], pts[b][1]);
    ctx.stroke();
  }
  ctx.shadowBlur = 0;
  for (let i = 0; i < pts.length; i++) {
    const tip = i === 4 || i === 8;
    ctx.beginPath();
    ctx.fillStyle = tip ? "#ffd166" : "#3dff9a";
    ctx.arc(pts[i][0], pts[i][1], tip ? 5 : 3, 0, Math.PI * 2);
    ctx.fill();
  }
}

function handleHand(data) {
  handLandmarks = data.landmarks || null;
  if (data.gesture) {
    const labels = {
      fist: "✊ Кулак", palm: "🖐 Ладонь",
      thumbs_up: "👍", thumbs_down: "👎",
      point: "☝ Указатель", pinch: "🤏 Щипок",
      click: "🖱 ЛКМ", drag: "↔ Drag",
    };
    el.camGesture.textContent = labels[data.gesture] || data.gesture;
    el.camGesture.classList.add("gesture");
  } else {
    el.camGesture.textContent = data.x != null ? "рука ✓" : "—";
    el.camGesture.classList.remove("gesture");
  }
  drawSkeleton();
}

el.camImg.addEventListener("load", () => {
  el.camPlaceholder.classList.add("hidden");
  camHiddenSince = 0;
});
el.camImg.addEventListener("error", () => {
  el.camPlaceholder.classList.remove("hidden");
  // переподключить MJPEG через 2 с
  setTimeout(() => {
    el.camImg.src = "/stream.mjpg?r=" + Date.now();
  }, 2000);
});

el.camCollapse.addEventListener("click", (e) => {
  e.stopPropagation();
  el.camDock.classList.toggle("collapsed");
  el.camCollapse.textContent = el.camDock.classList.contains("collapsed") ? "+" : "–";
});

/* ═══════════ КАМЕРА: DRAG + RESIZE (в рамках viewport) ═══════════ */
const CAM_MIN_W = 300;
const CAM_MIN_H = 240;
const CAM_MARGIN = 8;

function loadCamRect() {
  try {
    const raw = localStorage.getItem("berta-cam-rect");
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveCamRect(rect) {
  try {
    localStorage.setItem("berta-cam-rect", JSON.stringify(rect));
  } catch { /* приватный режим */ }
}

function applyCamRect(rect, persist) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const w = Math.min(Math.max(rect.w, CAM_MIN_W), vw - CAM_MARGIN * 2);
  const h = Math.min(Math.max(rect.h, CAM_MIN_H), vh - CAM_MARGIN * 2);
  const left = Math.min(Math.max(rect.x, CAM_MARGIN), vw - w - CAM_MARGIN);
  const top = Math.min(Math.max(rect.y, CAM_MARGIN), vh - h - CAM_MARGIN);
  const out = { x: left, y: top, w, h };
  el.camDock.classList.add("custom-pos");
  el.camDock.style.left = `${left}px`;
  el.camDock.style.top = `${top}px`;
  el.camDock.style.bottom = "auto";
  el.camDock.style.right = "auto";
  el.camDock.style.width = `${w}px`;
  el.camDock.style.height = `${h}px`;
  if (persist) saveCamRect(out);
  return out;
}

(function initCamRect() {
  const rect = loadCamRect();
  if (rect && Number.isFinite(rect.x) && Number.isFinite(rect.y)
      && Number.isFinite(rect.w) && Number.isFinite(rect.h)) {
    applyCamRect(rect, false);
  }
})();

/* --- drag за шапку --- */
let camDrag = null;
el.camHead.addEventListener("pointerdown", (e) => {
  if (e.target.closest("button")) return;
  if (e.button !== 0) return;
  const r = el.camDock.getBoundingClientRect();
  camDrag = {
    id: e.pointerId,
    ox: e.clientX - r.left,
    oy: e.clientY - r.top,
  };
  el.camHead.setPointerCapture(e.pointerId);
  el.camDock.classList.add("dragging");
  e.preventDefault();
});
el.camHead.addEventListener("pointermove", (e) => {
  if (!camDrag || camDrag.id !== e.pointerId) return;
  applyCamRect(
    {
      x: e.clientX - camDrag.ox,
      y: e.clientY - camDrag.oy,
      w: el.camDock.offsetWidth,
      h: el.camDock.offsetHeight,
    },
    false,
  );
});
function endCamDrag(e) {
  if (!camDrag || camDrag.id !== e.pointerId) return;
  camDrag = null;
  el.camDock.classList.remove("dragging");
  saveCamRect({
    x: el.camDock.offsetLeft,
    y: el.camDock.offsetTop,
    w: el.camDock.offsetWidth,
    h: el.camDock.offsetHeight,
  });
}
el.camHead.addEventListener("pointerup", endCamDrag);
el.camHead.addEventListener("pointercancel", endCamDrag);

/* --- resize за угол --- */
let camResize = null;
el.camResize.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  e.stopPropagation();
  e.preventDefault();
  const r = el.camDock.getBoundingClientRect();
  camResize = {
    id: e.pointerId,
    sx: e.clientX,
    sy: e.clientY,
    w: r.width,
    h: r.height,
  };
  el.camResize.setPointerCapture(e.pointerId);
  el.camDock.classList.add("resizing");
});
el.camResize.addEventListener("pointermove", (e) => {
  if (!camResize || camResize.id !== e.pointerId) return;
  applyCamRect(
    {
      x: el.camDock.offsetLeft,
      y: el.camDock.offsetTop,
      w: camResize.w + (e.clientX - camResize.sx),
      h: camResize.h + (e.clientY - camResize.sy),
    },
    false,
  );
});
function endCamResize(e) {
  if (!camResize || camResize.id !== e.pointerId) return;
  camResize = null;
  el.camDock.classList.remove("resizing");
  saveCamRect({
    x: el.camDock.offsetLeft,
    y: el.camDock.offsetTop,
    w: el.camDock.offsetWidth,
    h: el.camDock.offsetHeight,
  });
}
el.camResize.addEventListener("pointerup", endCamResize);
el.camResize.addEventListener("pointercancel", endCamResize);

/* clamp при смене размера окна */
window.addEventListener("resize", () => {
  if (!el.camDock.classList.contains("custom-pos")) return;
  applyCamRect(
    {
      x: el.camDock.offsetLeft,
      y: el.camDock.offsetTop,
      w: el.camDock.offsetWidth,
      h: el.camDock.offsetHeight,
    },
    true,
  );
});

/* ═══════════ КНОПКИ КАМЕРЫ ═══════════ */
function post(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : "{}",
  }).then((r) => r.json());
}

el.btnCalibrate.addEventListener("click", () =>
  post("/api/calibrate").then((d) => toast(d.reply || "…")));
el.btnRecenter.addEventListener("click", () =>
  post("/api/recenter").then((d) => toast(d.reply || "…")));
el.btnResetOffset.addEventListener("click", () =>
  post("/api/offset/reset").then((d) => toast(d.reply || "…")));
el.btnCursor.addEventListener("click", () => {
  const next = !state.cursor;
  post("/api/pointer", { enabled: next }).then((d) => {
    state.cursor = d.enabled;
    updateCursorBtn();
  });
});
function updateCursorBtn() {
  el.btnCursor.textContent = state.cursor ? "Курсор: ВКЛ" : "Курсор: ВЫКЛ";
  el.btnCursor.classList.toggle("on", state.cursor);
  el.btnCursor.classList.toggle("off", !state.cursor);
}

/* ═══════════ ЭКСТРЕННАЯ ОСТАНОВКА ═══════════ */
el.emergencyBtn.addEventListener("click", () => {
  const next = !state.emergency;
  post("/api/emergency", { enabled: next }).then((d) => {
    state.emergency = d.active;
    updateEmergency();
    addMsg("bot", d.reply);
    toast(d.reply);
  });
});
function updateEmergency() {
  el.emergencyBtn.classList.toggle("active", state.emergency);
  el.emergencyBtn.querySelector("span").textContent =
    state.emergency ? "ПУСК" : "СТОП";
  refreshBaseStatus();
}

/* ═══════════ ЗАДАЧИ ═══════════
   Строки нумеруются с 1 (голос скажет «удали задачу два»). Текст никогда не
   обрезается: white-space/overflow-wrap в CSS, а высота строки растёт вниз. */
function toggleTasks(force) {
  const open = force !== undefined ? force : el.taskPanel.hidden;
  el.taskPanel.hidden = !open;
  el.taskToggleBtn.setAttribute("aria-expanded", String(open));
  if (open) {
    if (state.tasks === null) loadTasks();
    else el.taskInput.focus();
  }
}

function loadTasks() {
  fetch("/api/tasks")
    .then((r) => r.json())
    .then((d) => renderTasks(d.tasks || []))
    .catch(() => toast("Не удалось загрузить задачи"));
}

function renderTasks(tasks) {
  state.tasks = Array.isArray(tasks) ? tasks : [];
  el.taskList.textContent = "";
  el.taskCount.textContent = String(state.tasks.length);
  el.taskEmpty.hidden = state.tasks.length > 0;

  state.tasks.forEach((task, i) => {
    const li = document.createElement("li");
    li.className = "task-row";
    li.dataset.id = String(task.id);

    const idx = document.createElement("span");
    idx.className = "task-idx";
    idx.textContent = `${i + 1}.`;

    const text = document.createElement("span");
    text.className = "task-text";
    text.textContent = task.text;   // не innerHTML: текст приходит снаружи

    const btnEdit = document.createElement("button");
    btnEdit.className = "task-btn";
    btnEdit.type = "button";
    btnEdit.textContent = "изменить";
    btnEdit.addEventListener("click", () => beginTaskEdit(li, i + 1, task));

    const btnDel = document.createElement("button");
    btnDel.className = "task-btn task-btn-danger";
    btnDel.type = "button";
    btnDel.textContent = "удалить";
    btnDel.addEventListener("click", () => deleteTask(task));

    li.append(idx, text, btnEdit, btnDel);
    el.taskList.appendChild(li);
  });
}

function patchTask(id, text) {
  return fetch(`/api/tasks/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  }).then((r) => r.json());
}

function deleteTask(task) {
  fetch(`/api/tasks/${task.id}`, { method: "DELETE" })
    .then((r) => r.json())
    .then((d) => {
      if (!d.ok) {
        toast(d.detail || "Не удалось удалить");
        return;
      }
      loadTasks();
      toast("Задача удалена");
    });
}

/* Правка на месте: кнопки строки заменяются на ✓/✕, текст — на растущий вниз
   textarea. renderTasks перестраивает строку, поэтому слушатели не копятся. */
function beginTaskEdit(li, number, task) {
  li.classList.add("editing");
  li.textContent = "";

  const idx = document.createElement("span");
  idx.className = "task-idx";
  idx.textContent = `${number}.`;

  const area = document.createElement("textarea");
  area.className = "task-edit";
  area.value = task.text;
  area.rows = 1;
  area.addEventListener("input", () => autoGrowTask(area));
  area.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      save();
    } else if (e.key === "Escape") {
      e.preventDefault();
      cancel();
    }
  });

  const btnOk = document.createElement("button");
  btnOk.className = "task-btn task-ok";
  btnOk.type = "button";
  btnOk.textContent = "✓";
  btnOk.title = "Сохранить (Enter)";
  btnOk.addEventListener("click", save);

  const btnCancel = document.createElement("button");
  btnCancel.className = "task-btn task-no";
  btnCancel.type = "button";
  btnCancel.textContent = "✕";
  btnCancel.title = "Отменить (Esc)";
  btnCancel.addEventListener("click", cancel);

  function cancel() {
    renderTasks(state.tasks);
  }

  function save() {
    const value = area.value.trim();
    if (!value) {
      toast("Задача не может быть пустой");
      return;
    }
    if (value === task.text) {
      cancel();
      return;
    }
    patchTask(task.id, value).then((d) => {
      if (!d.ok) {
        toast(d.detail || "Не удалось сохранить");
        return;
      }
      loadTasks();
      toast("Сохранено");
    });
  }

  li.append(idx, area, btnOk, btnCancel);
  area.focus();
  area.setSelectionRange(area.value.length, area.value.length);
  autoGrowTask(area);
}

/* Высота под содержимое: строка удлиняется вниз, а не сжимается/обрезается */
function autoGrowTask(area) {
  area.style.height = "auto";
  area.style.height = `${area.scrollHeight}px`;
}

el.taskToggleBtn.addEventListener("click", () => toggleTasks());
el.taskCloseBtn.addEventListener("click", () => toggleTasks(false));
el.taskForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const value = el.taskInput.value.trim();
  if (!value) return;
  fetch("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: value }),
  })
    .then((r) => r.json())
    .then((d) => {
      if (!d.ok) {
        toast(d.detail || "Не удалось добавить");
        return;
      }
      el.taskInput.value = "";
      // Не дописываем d.task локально: сервер уже разослал актуальный список
      // по WS («tasks»), и запоздавший renderTasks из-за гонки дублировал
      // только что добавленную задачу (см. «изменяю — второй исчезает»).
      loadTasks();
      el.taskInput.focus();
    });
});

/* ═══════════ МИКРОФОН / TTS / ТЕМА ═══════════ */
el.micBtn.addEventListener("click", () => {
  post("/api/mic", { enabled: !state.mic }).then((d) => {
    if (!d.ok) { toast("Микрофон недоступен"); return; }
    state.mic = !d.muted;
    el.micBtn.dataset.on = String(state.mic);
    toast(state.mic ? "Микрофон включён" : "Микрофон выключен");
  });
});
el.ttsBtn.addEventListener("click", () => {
  post("/api/tts", { enabled: !state.tts }).then((d) => {
    state.tts = d.enabled;
    el.ttsBtn.dataset.on = String(state.tts);
    toast(state.tts ? "Озвучка включена" : "Озвучка выключена");
  });
});

const THEMES = ["dark", "light"];
el.themeBtn.addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme;
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  el.themeBtn.textContent = next === "dark" ? "🌙" : "☀️";
  localStorage.setItem("berta-theme", next);
});
el.contrastBtn.addEventListener("click", () => {
  const on = document.documentElement.dataset.contrast === "high";
  document.documentElement.dataset.contrast = on ? "" : "high";
  localStorage.setItem("berta-contrast", on ? "" : "high");
  toast(on ? "Обычный контраст" : "Высокий контраст");
});

/* ═══════════ ПОДТВЕРЖДЕНИЕ ОПАСНОГО ДЕЙСТВИЯ ═══════════ */
el.confirmYes.addEventListener("click", () => {
  if (state.pendingConfirm) {
    post("/api/confirm", { id: state.pendingConfirm, ok: true })
      .then((d) => addMsg("bot", d.reply));
  }
  state.pendingConfirm = null;
  el.confirmModal.hidden = true;
});
el.confirmNo.addEventListener("click", () => {
  if (state.pendingConfirm) {
    post("/api/confirm", { id: state.pendingConfirm, ok: false })
      .then((d) => addMsg("bot", d.reply || "Отменено."));
  }
  state.pendingConfirm = null;
  el.confirmModal.hidden = true;
});

/* ═══════════ WEBSOCKET ═══════════ */
let ws = null;
let wsRetry = 500;

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => { wsRetry = 500; };
  ws.onclose = () => {
    setTimeout(connectWS, wsRetry);
    wsRetry = Math.min(wsRetry * 1.6, 5000);
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    routeWS(msg);
  };
}

function routeWS(msg) {
  const d = msg.data;
  switch (msg.type) {
    case "snapshot":
      applyProfile(d.profile);
      state.emergency = d.emergency;
      state.mic = !d.mic_muted;
      state.tts = d.tts_enabled;
      state.cursor = d.pointer && d.pointer.enabled !== null
        ? d.pointer.enabled : true;
      el.micBtn.dataset.on = String(state.mic);
      el.ttsBtn.dataset.on = String(state.tts);
      updateCursorBtn();
      updateEmergency();
      renderAudit(d.audit || []);
      if (d.tasks) renderTasks(d.tasks);
      if (d.hand) handleHand(d.hand);
      if (d.gaze) handleGaze(d.gaze);
      refreshBaseStatus(d.tracker);
      if (d.voice_partial && d.voice_partial.text) {
        showLive(d.voice_partial.final ? "ФРАЗА" : "СЛУШАЮ", d.voice_partial.text);
        state.lastPartialAt = Date.now();
      }
      if (d.tts_enabled && d.last_command && d.last_command.text) {
        // ранее озвученный ответ — сигнал "speaking" для икона
        updateOrb("speaking", { outputVolume: 0.3 });
        clearTimeout(_orbIdleTimer);
        _orbIdleTimer = setTimeout(() => updateOrb("idle"), 8000);
      }
      break;

    case "tracker_status":
      refreshBaseStatus(d);
      if (d.state === "CALIBRATING") {
        el.recenterTarget.hidden = false;
        overrideStatus("do", "СМОТРИТЕ НА ТОЧКУ…", 60000);
      } else {
        el.recenterTarget.hidden = true;
      }
      el.camFps.textContent = `${Math.round(d.fps || 0)} FPS`;
      break;

    case "gaze":
      handleGaze(d);
      break;

    case "hand":
      handleHand(d);
      break;

    case "tasks":
      renderTasks(d.tasks);
      break;

    case "tasks_open":
      toggleTasks(true);
      loadTasks();
      break;

    case "voice_partial":
      // поздний interim после финала не должен перетирать «ФРАЗА»
      if (!d.final && state.finalHoldUntil && Date.now() < state.finalHoldUntil) {
        break;
      }
      state.lastPartialAt = Date.now();
      showLive(d.final ? "ФРАЗА" : "СЛУШАЮ", d.text);
      if (d.final) {
        state.finalHoldUntil = Date.now() + 4000;
        state.awaitingReply = true;
        overrideStatus("think", "ДУМАЮ…", 20000);
        orbThinking();
      } else {
        setStatus("listen", "СЛУШАЮ…");
        orbListening();
      }
      break;

    case "voice_command":
      if (d.action) overrideStatus("do", `ВЫПОЛНЯЮ: ${d.action}`, 2200);
      // команда — ответа не ждём, плашку можно снять
      state.awaitingReply = false;
      state.finalHoldUntil = 0;
      showLive("", "");
      state.lastPartialAt = 0;
      orbIdle();
      break;

    case "pointer_status":
      if (d.enabled !== null && d.enabled !== undefined) {
        state.cursor = d.enabled;
        updateCursorBtn();
      }
      if (d.message) toast(d.message);
      break;

    case "recentering":
      el.recenterTarget.hidden = !d.active;
      if (d.active) overrideStatus("do", "СМОТРИТЕ В ЦЕНТР 1.5 с…", 5000);
      break;

    case "profile":
      applyProfile(d);
      break;

    case "emergency":
      state.emergency = d.active;
      updateEmergency();
      break;

    case "audit":
      for (const e of d) pushAuditOne(e);
      break;

    case "confirm":
      state.pendingConfirm = d.id;
      el.confirmText.textContent = d.text;
      el.confirmModal.hidden = false;
      break;

    case "router_start":
      state.streaming = addMsg("bot", "", { streaming: true });
      setStatus("think", "ДУМАЮ…");
      orbThinking();
      break;

    case "llm_chunk":
      if (!state.streaming) state.streaming = addMsg("bot", "", { streaming: true });
      state.streaming.textContent += d.chunk;
      el.log.scrollTop = el.log.scrollHeight;
      orbSpeaking();
      break;

    case "router_end":
      if (state.streaming) {
        state.streaming.textContent = d.reply || state.streaming.textContent;
        state.streaming.parentElement.classList.remove("streaming");
      }
      state.streaming = null;
      state.awaitingReply = false;
      state.finalHoldUntil = 0;
      showLive("", "");
      state.lastPartialAt = 0;
      refreshBaseStatus();
      orbSpeaking();
      break;

    case "voice_user":
      if (d.text) addMsg("user", d.text);
      break;

     case "router":
      if (d.reply) addMsg("bot", d.reply);
      // ответ пришёл — убрать плашку и статус ожидания
      state.awaitingReply = false;
      state.finalHoldUntil = 0;
      showLive("", "");
      state.lastPartialAt = 0;
      refreshBaseStatus();
      orbSpeaking();
      if (d.needs_confirm) {
        state.pendingConfirm = d.confirm_id;
        el.confirmText.textContent = d.reply;
        el.confirmModal.hidden = false;
      }
      break;

    case "orb_state":
      updateOrbState(d.state, {});
      break;

    case "mic":
      state.mic = !d.muted;
      el.micBtn.dataset.on = String(state.mic);
      break;

    case "tts":
      state.tts = d.enabled;
      el.ttsBtn.dataset.on = String(state.tts);
      break;
  }
}

function handleGaze(d) {
  const face = d.face && d.x != null;
  el.camFace.textContent = d.blink ? "моргание" : (face ? "лицо ✓" : "лицо ✗");
  el.camFace.classList.toggle("off", !face && !d.blink);
}

/* подпись гаснет, если partial давно не приходил;
   финальная «ФРАЗА» держится дольше, пока ждём ответа */
setInterval(() => {
  if (!state.lastPartialAt) return;
  const age = Date.now() - state.lastPartialAt;
  const limit = state.awaitingReply ? 20000 : 2500;
  if (age > limit) {
    showLive("", "");
    state.lastPartialAt = 0;
    if (!state.awaitingReply) refreshBaseStatus();
  }
}, 600);

/* ═══════════ ТЕМА ИЗ localStorage ═══════════ */
(function initTheme() {
  const t = localStorage.getItem("berta-theme");
  if (t) {
    document.documentElement.dataset.theme = t;
    el.themeBtn.textContent = t === "dark" ? "🌙" : "☀️";
  }
  if (localStorage.getItem("berta-contrast") === "high") {
    document.documentElement.dataset.contrast = "high";
  }
})();

/* ═══════════ СТАРТ ═══════════ */
connectWS();
fetch("/api/status")
  .then((r) => r.json())
  .then((d) => {
    applyProfile(d.profile);
    renderAudit(d.audit || []);
    refreshBaseStatus(d.tracker);
    if (d.hand) handleHand(d.hand);
    state.emergency = d.emergency;
    state.cursor = d.pointer && d.pointer.enabled != null ? d.pointer.enabled : true;
    updateCursorBtn();
    updateEmergency();
  })
  .catch(() => setStatus("error", "СЕРВЕР НЕ ОТВЕЧАЕТ"));
