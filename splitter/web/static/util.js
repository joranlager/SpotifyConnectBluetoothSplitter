// Small DOM and formatting helpers shared by the shell and the views.

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);

export function fmtUptime(seconds) {
  if (!seconds) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

export function fmtMB(bytes) {
  const mb = bytes / 1048576;
  return (mb >= 10 ? mb.toFixed(0) : mb.toFixed(1)) + " MB";
}

export function toast(message, bad = false) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), bad ? 5000 : 2600);
}

// Debounce per key, so a slider drag sends one request at the end.
const timers = {};
export function debounce(key, ms, fn) {
  clearTimeout(timers[key]);
  timers[key] = setTimeout(fn, ms);
}
