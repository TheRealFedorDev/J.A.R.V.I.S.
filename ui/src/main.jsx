/*  Красивый «ИИ-шар» в центре панели.
    Controlled mode: app.js вызывает window.__BertaOrb.set({state, inputVolume, outputVolume}).
    theme="radial" — пульсирующий шарик с анимацией по состоянию.
    Цвета и размер адаптируются к теме BERTA (data-theme / data-contrast).
*/
import React from "react";
import { createRoot } from "react-dom/client";
import { Orb } from "orb-ui";

const container = document.getElementById("ai-orb");
const root = createRoot(container);

let signal = {
  state: "idle",
  inputVolume: 0,
  outputVolume: 0,
};

// адаптивные цвета под светлую/тёмную тему и режим высокого контраста
function getSurroundColor() {
  const rootEl = document.documentElement;
  if (rootEl.dataset.theme === "light") {
    return "#ffffff";
  }
  if (rootEl.dataset.contrast === "high") {
    return "#000000";
  }
  return "#0b0f14";
}

function render() {
  root.render(
    React.createElement(Orb, {
      theme: "radial",
      interactive: false,
      signal: signal,
      "aria-label": "Berta — голосовой ассистент",
      style: {
        "--orb-ui-radial-control-surround": getSurroundColor(),
        width: "280px",
        height: "280px",
      },
    })
  );
}

// следим за сменой темы (data-theme / data-contrast на <html>) → перерисовать
const _themeObserver = new MutationObserver(() => {
  if (signal.state !== "idle") return; // не перерисовывать во время активности
  render();
});
if (container.isConnected) {
  _themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme", "data-contrast"],
  });
}

render();

// Глобальная «точка входа» для vanilla app.js
window.__BertaOrb = {
  set: (s) => {
    Object.assign(signal, s);
    render();
  },
  get: () => signal,
};
