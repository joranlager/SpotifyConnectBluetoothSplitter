// Speakers: per-speaker volume, mute and delay (all live), the sync test, and the
// list of audio outputs to add speakers from.

import { loadOutputs, patchSpeaker, saveConfig, syncTest } from "../actions.js";
import { liveSpeakers } from "../store.js";
import { $, debounce, esc, toast } from "../util.js";

const ICON = {
  speaker: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2.5" width="14" height="19" rx="3.5"/><circle cx="12" cy="14.5" r="3.6"/><circle cx="12" cy="6.8" r="1.1"/></svg>',
  volOn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>',
  volOff: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="m22 9-6 6M16 9l6 6"/></svg>',
  plus: '<svg class="plus" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
};

const TEMPLATE = `
  <div class="section-title first">
    <span>Speakers</span>
    <span class="section-actions">
      <button class="btn small plain" id="syncBtn" title="Plays a click on every speaker. If it sounds doubled, raise the delay of the speaker that is early.">Sync test</button>
      <button class="btn small plain" id="scanBtn">Refresh devices</button>
    </span>
  </div>
  <div id="speakers" class="grid"></div>
  <div id="noSpeakers" class="card empty hidden">
    <p class="strong">No speakers yet</p>
    <p>Pair your Bluetooth speakers with this computer, then add them below.</p>
  </div>
  <div class="section-title"><span>Available devices</span></div>
  <div class="card"><div id="outputs" class="outputs"></div></div>`;

const CARD = `
  <div class="speaker-head">
    <div class="avatar"></div>
    <div class="meta">
      <div class="speaker-name"></div>
      <div class="speaker-sub"></div>
    </div>
    <button class="mute" aria-label="Mute"></button>
  </div>
  <div class="row">
    <span class="label">Volume</span>
    <input type="range" class="grow vol" min="0" max="100" step="1" aria-label="Volume">
    <span class="value-tag vol-val"></span>
  </div>
  <div class="row">
    <span class="label">Delay</span>
    <div class="grow"></div>
    <div class="stepper">
      <button class="dec10" aria-label="Less delay by 10 ms" title="−10 ms">−</button>
      <button class="dec1" aria-label="Less delay by 1 ms" title="−1 ms">‹</button>
      <input class="delay-input" type="number" min="0" max="5000" step="1" inputmode="numeric" aria-label="Delay in milliseconds">
      <span class="unit">ms</span>
      <button class="inc1" aria-label="More delay by 1 ms" title="+1 ms">›</button>
      <button class="inc10" aria-label="More delay by 10 ms" title="+10 ms">+</button>
    </div>
  </div>
  <div class="bufbar"><i></i></div>
  <div class="metrics">
    <div>Latency<b class="m-lat">–</b></div>
    <div>Buffer<b class="m-buf">–</b></div>
    <div>Underruns<b class="m-und">–</b></div>
  </div>
  <div class="speaker-foot">
    <span class="note"></span>
    <button class="link remove">Remove</button>
  </div>`;

let root = null;
let cards = new Map(); // match -> element
let outputsShown = null; // the outputs list last rendered, to avoid rebuilding it every second

// ----- speaker cards ---------------------------------------------------------------

function updateCard(el, sp, bufferSeconds) {
  const live = sp.connected;
  const avatar = $(".avatar", el);
  avatar.className = "avatar" + (live ? "" : " off");
  avatar.innerHTML = ICON.speaker;
  $(".speaker-name", el).textContent = sp.match;
  $(".speaker-sub", el).textContent = live ? `${sp.device_name} · ${sp.hostapi}` : sp.note || "Not connected";

  const mute = $(".mute", el);
  mute.className = "mute" + (sp.muted ? " on" : "");
  mute.innerHTML = sp.muted ? ICON.volOff : ICON.volOn;
  mute.setAttribute("aria-pressed", String(sp.muted));
  mute.setAttribute("aria-label", sp.muted ? `Unmute ${sp.match}` : `Mute ${sp.match}`);
  mute.dataset.muted = sp.muted ? "1" : "0";

  const vol = $(".vol", el);
  if (document.activeElement !== vol) {
    vol.value = Math.round(sp.volume * 100);
    vol.style.setProperty("--p", `${Math.round(sp.volume * 100)}%`);
    $(".vol-val", el).textContent = `${Math.round(sp.volume * 100)}%`;
  }
  const delayInput = $(".delay-input", el);
  if (document.activeElement !== delayInput) delayInput.value = Math.round(sp.delay_ms);
  el.dataset.delay = sp.delay_ms;

  $(".m-lat", el).textContent = live ? `${Math.round(sp.latency_ms)} ms` : "–";
  $(".m-buf", el).textContent = live ? `${Math.round(sp.buffered_ms)} ms` : "–";
  $(".m-und", el).textContent = live ? String(sp.underruns) : "–";
  const bar = $(".bufbar > i", el);
  const pct = live ? Math.min(100, (sp.buffered_ms / (bufferSeconds * 1000)) * 100) : 0;
  bar.style.width = pct + "%";
  bar.style.background =
    sp.underruns > 0 && live && sp.underruns > Number(el.dataset.und || 0) ? "var(--orange)" : "var(--green)";
  el.dataset.und = sp.underruns || 0;
  $(".note", el).textContent = live && sp.muted ? "Muted" : "";
}

function setDelay(match, ms, typing = false) {
  ms = Math.max(0, Math.min(5000, ms));
  const el = cards.get(match);
  if (el) {
    el.dataset.delay = ms;
    if (!typing) $(".delay-input", el).value = ms;
  }
  debounce("delay:" + match, 350, () => patchSpeaker(match, { delay_ms: ms }));
}

async function removeSpeaker(match, state) {
  if (!confirm(`Remove “${match}” from the speakers?`)) return;
  const speakers = liveSpeakers().filter((s) => s.match !== match);
  await saveConfig({ ...state.config, speakers }, "Speaker removed");
}

function wireCard(el, match, getState) {
  const vol = $(".vol", el);
  vol.addEventListener("input", () => {
    vol.style.setProperty("--p", vol.value + "%");
    $(".vol-val", el).textContent = vol.value + "%";
  });
  vol.addEventListener("change", () => patchSpeaker(match, { volume: vol.value / 100 }));
  $(".mute", el).addEventListener("click", (e) => patchSpeaker(match, { muted: e.currentTarget.dataset.muted !== "1" }));
  const delayOf = () => Number(el.dataset.delay || 0);
  $(".dec10", el).addEventListener("click", () => setDelay(match, delayOf() - 10));
  $(".dec1", el).addEventListener("click", () => setDelay(match, delayOf() - 1));
  $(".inc1", el).addEventListener("click", () => setDelay(match, delayOf() + 1));
  $(".inc10", el).addEventListener("click", () => setDelay(match, delayOf() + 10));
  const delayInput = $(".delay-input", el);
  delayInput.addEventListener("input", () => {
    const n = Number(delayInput.value);
    if (Number.isFinite(n) && delayInput.value !== "") setDelay(match, Math.round(n), true);
  });
  $(".remove", el).addEventListener("click", () => removeSpeaker(match, getState()));
}

function renderSpeakers(state) {
  const list = state.status.speakers;
  const box = $("#speakers", root);
  $("#noSpeakers", root).classList.toggle("hidden", list.length > 0);
  const seen = new Set();
  for (const sp of list) {
    seen.add(sp.match);
    let el = cards.get(sp.match);
    if (!el) {
      el = document.createElement("div");
      el.className = "card speaker";
      el.innerHTML = CARD;
      wireCard(el, sp.match, () => latest);
      cards.set(sp.match, el);
      box.appendChild(el);
    }
    updateCard(el, sp, state.config?.buffer_seconds || 2);
  }
  for (const [match, el] of cards) {
    if (!seen.has(match)) {
      el.remove();
      cards.delete(match);
    }
  }
  for (const sp of list) box.appendChild(cards.get(sp.match)); // keep the config's order
}

// ----- available outputs -------------------------------------------------------------

async function addSpeaker(name) {
  const speakers = [...liveSpeakers(), { match: name, delay_ms: 0, volume: 1, muted: false }];
  if (await saveConfig({ ...latest.config, speakers }, "Speaker added")) loadOutputs();
}

function renderOutputs(state) {
  const key = JSON.stringify([state.outputs, (state.config?.speakers || []).map((s) => s.match)]);
  if (key === outputsShown) return;
  outputsShown = key;
  const box = $("#outputs", root);
  const configured = (state.config?.speakers || []).map((s) => s.match.toLowerCase());
  const wasapi = state.outputs.filter((o) => o.hostapi === "Windows WASAPI");
  const shown = wasapi.length ? wasapi : state.outputs;
  if (!shown.length) {
    box.innerHTML = '<div class="empty">No audio devices found. Pair your Bluetooth speakers, then refresh.</div>';
    return;
  }
  box.innerHTML = shown
    .map((o) => {
      const added = configured.some((m) => o.name.toLowerCase().includes(m));
      return `<div class="output">
        <div class="k"><b>${esc(o.name)}</b><small>${esc(o.hostapi)}</small></div>
        ${added ? '<span class="status-tag">Added</span>' : `<button class="btn small" data-add="${esc(o.name)}">${ICON.plus}Add</button>`}
      </div>`;
    })
    .join("");
  box.querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", () => addSpeaker(b.dataset.add)));
}

// ----- the view -----------------------------------------------------------------------

let latest = null; // the newest state, for handlers that run later

export default {
  mount(outlet) {
    root = outlet;
    cards = new Map();
    outputsShown = null;
    root.innerHTML = TEMPLATE;
    $("#syncBtn", root).addEventListener("click", syncTest);
    $("#scanBtn", root).addEventListener("click", async () => {
      await loadOutputs();
      toast("Devices refreshed");
    });
    loadOutputs(); // rescans the devices when nothing is streaming
  },

  update(state) {
    latest = state;
    if (!root || !state.status) return;
    renderSpeakers(state);
    renderOutputs(state);
    const sync = $("#syncBtn", root);
    sync.disabled = state.status.state !== "running" || !!state.status.sync_test;
    sync.textContent = state.status.sync_test ? "Testing…" : "Sync test";
  },

  unmount() {
    root = null;
    cards = new Map();
  },
};
