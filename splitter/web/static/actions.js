// Everything that reads from or changes the backend. Views call these; the results
// go into the store, which notifies whoever is on screen.

import { api } from "./api.js";
import { HISTORY, state, update } from "./store.js";
import { toast } from "./util.js";

// ----- status polling ------------------------------------------------------------

let lastBytes = null;
let lastAt = null;

function ingest(status, now) {
  let kbps = 0;
  if (lastBytes !== null && lastAt !== null && status.bytes_in >= lastBytes) {
    const seconds = (now - lastAt) / 1000;
    if (seconds > 0) kbps = ((status.bytes_in - lastBytes) * 8) / 1000 / seconds;
  }
  lastBytes = status.bytes_in;
  lastAt = now;
  if (status.state !== "running") kbps = 0;
  state.history.push(kbps);
  if (state.history.length > HISTORY) state.history.shift();
  state.peak = Math.max(state.peak * 0.995, kbps);
  state.kbps = kbps;
}

let polling = false;

// Only a failed request counts as "offline". Render errors are logged, not hidden.
export async function poll() {
  if (polling) return; // the previous poll is still waiting
  polling = true;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  let status;
  try {
    status = await api("GET", "/api/status", undefined, controller.signal);
  } catch (err) {
    console.warn("status poll failed:", err);
    update({ offline: true });
    return;
  } finally {
    clearTimeout(timer);
    polling = false;
  }
  ingest(status, Date.now());
  update({ status, offline: false });
}

// ----- loading -----------------------------------------------------------------

export async function loadConfig() {
  try {
    update({ config: await api("GET", "/api/config") });
  } catch (err) {
    toast(err.message, true);
  }
}

export async function loadOutputs() {
  try {
    const res = await api("GET", "/api/outputs");
    update({ outputs: res.outputs });
  } catch (err) {
    toast(err.message, true);
  }
}

// ----- changes -------------------------------------------------------------------

export async function saveConfig(config, okMessage = "Settings saved") {
  try {
    const res = await api("PUT", "/api/config", config);
    update({ config: res.config, status: res.status, dirty: false });
    if (res.config.receiver === "headless") pollSetup();
    toast(okMessage);
    return true;
  } catch (err) {
    toast(err.message, true);
    return false;
  }
}

const CONTROL_MESSAGES = { start: "Streaming started", stop: "Streaming stopped", restart: "Restarted" };

export async function control(action) {
  try {
    const res = await api("POST", "/api/control", { action });
    update({ status: res.status });
    if (CONTROL_MESSAGES[action]) toast(CONTROL_MESSAGES[action]);
    return true;
  } catch (err) {
    toast(err.message, true);
    return false;
  }
}

export async function syncTest() {
  try {
    await api("POST", "/api/control", { action: "sync_test" });
    toast("Playing clicks on every speaker for 10 seconds. If a click sounds doubled, raise the delay of the speaker that is early.");
    setTimeout(poll, 300);
  } catch (err) {
    toast(err.message, true);
  }
}

// Volume, mute and delay apply live; nothing restarts, so the music keeps playing.
export async function patchSpeaker(match, changes, okMessage) {
  try {
    const res = await api("PATCH", "/api/speaker", { match, ...changes });
    update({ status: res.status });
    if (okMessage) toast(okMessage);
  } catch (err) {
    toast(err.message, true);
    poll();
  }
}

// ----- Spotify client setup (receiver "headless") ------------------------------------

let setupPolling = false;

export async function pollSetup() {
  if (setupPolling) return;
  setupPolling = true;
  try {
    update({ setup: await api("GET", "/api/setup") });
  } catch (err) {
    console.warn("setup poll failed:", err);
  } finally {
    setupPolling = false;
  }
}

export async function setupAction(action) {
  try {
    const res = await api("POST", "/api/setup", { action });
    update({ setup: res.setup, status: res.status });
    toast(action === "start" ? "Setup started" : "Setup finished");
  } catch (err) {
    toast(err.message, true);
    pollSetup();
  }
}
