/* Berta web — статус, WS, камера+скелет, команды, профили, СТОП */
"use strict";

const $ = (id) => document.getElementById(id);

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
  overrideStatus("do", "ВЫПОЛНЯЮ…", 2000);

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
        return;
      }
      if (data.streaming) {
        // ответ придёт чанками по WS
        state.streaming = addMsg("bot", "", { streaming: true });
        setStatus("think", "ДУМАЮ…");
        return;
      }
      if (data.reply) addMsg("bot", data.reply);
      if (data.ok === false) toast(data.reply || "Ошибка");
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
const ctx = el.skelCanvas.getContext("2d");
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

el.camCollapse.addEventListener("click", () => {
  el.camDock.classList.toggle("collapsed");
  el.camCollapse.textContent = el.camDock.classList.contains("collapsed") ? "+" : "–";
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
      if (d.hand) handleHand(d.hand);
      if (d.gaze) handleGaze(d.gaze);
      refreshBaseStatus(d.tracker);
      if (d.voice_partial && d.voice_partial.text) {
        showLive(d.voice_partial.final ? "ФРАЗА" : "СЛУШАЮ", d.voice_partial.text);
        state.lastPartialAt = Date.now();
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

    case "voice_partial":
      state.lastPartialAt = Date.now();
      showLive(d.final ? "ФРАЗА" : "СЛУШАЮ", d.text);
      if (d.final) {
        setStatus("do", "РАСПОЗНАНО");
        setTimeout(() => refreshBaseStatus(), 1800);
      } else {
        setStatus("listen", "СЛУШАЮ…");
      }
      break;

    case "voice_command":
      if (d.action) overrideStatus("do", `ВЫПОЛНЯЮ: ${d.action}`, 2200);
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
      break;

    case "llm_chunk":
      if (!state.streaming) state.streaming = addMsg("bot", "", { streaming: true });
      state.streaming.textContent += d.chunk;
      el.log.scrollTop = el.log.scrollHeight;
      break;

    case "router_end":
      if (state.streaming) {
        state.streaming.textContent = d.reply || state.streaming.textContent;
        state.streaming.parentElement.classList.remove("streaming");
      }
      state.streaming = null;
      refreshBaseStatus();
      break;

    case "router":
      if (d.reply) addMsg("bot", d.reply);
      if (d.needs_confirm) {
        state.pendingConfirm = d.confirm_id;
        el.confirmText.textContent = d.reply;
        el.confirmModal.hidden = false;
      }
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

/* подпись «СЛУШАЮ» гаснет, если partial давно не приходил */
setInterval(() => {
  if (state.lastPartialAt && Date.now() - state.lastPartialAt > 2500) {
    showLive("", "");
    state.lastPartialAt = 0;
    refreshBaseStatus();
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
