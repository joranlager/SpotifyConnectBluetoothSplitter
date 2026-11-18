// The app shell: header, tabs, banner and the live polling loop. The views are routed
// into #view by router.js and get their data from the store.

import { control, loadConfig, loadOutputs, poll } from "./actions.js";
import { startRouter } from "./router.js";
import { state, subscribe } from "./store.js";
import { $ } from "./util.js";
import dashboard from "./views/dashboard.js";
import settings from "./views/settings.js";
import setup from "./views/setup.js";
import speakers from "./views/speakers.js";

const ROUTES = [
  { path: "/", title: "Dashboard", view: dashboard },
  { path: "/speakers", title: "Speakers", view: speakers },
  { path: "/settings", title: "Settings", view: settings },
  { path: "/setup", title: "Spotify client", view: setup },
];

// ----- header, banner, tabs -------------------------------------------------------

function renderChrome(s) {
  const pill = $("#statePill");
  const text = $("#stateText");
  const toggle = $("#toggleBtn");
  document.body.classList.toggle("offline", s.offline);
  $("#navSetup").classList.toggle("hidden", s.config?.receiver !== "headless");

  pill.className = "pill";
  if (!s.status || s.offline) {
    pill.classList.add("offline");
    text.textContent = s.offline ? "Offline" : "Connecting…";
    toggle.disabled = true;
    return;
  }
  const status = s.status;
  toggle.disabled = false;
  if (status.state === "running") {
    pill.classList.add("running");
    const sp = status.spotify;
    text.textContent = sp && sp.playing ? "Playing" : "Running";
    toggle.textContent = "Stop";
    toggle.className = "btn danger";
    toggle.dataset.action = "stop";
  } else {
    if (status.state === "error") {
      pill.classList.add("error");
      text.textContent = "Error";
    } else {
      text.textContent = "Stopped";
    }
    toggle.textContent = "Start";
    toggle.className = "btn";
    toggle.dataset.action = "start";
  }
  $("#brandSub").textContent = status.device_name;

  const banner = $("#banner");
  const spotifyError = status.spotify && status.spotify.error;
  if (status.last_error) {
    banner.classList.remove("hidden", "info");
    $("#bannerText").textContent = status.last_error;
  } else if (spotifyError) {
    banner.classList.remove("hidden");
    banner.classList.add("info");
    $("#bannerText").textContent = spotifyError;
  } else {
    banner.classList.add("hidden");
  }
}

$("#toggleBtn").addEventListener("click", async () => {
  const button = $("#toggleBtn");
  button.disabled = true;
  try {
    await control(button.dataset.action || "start");
  } finally {
    renderChrome(state);
  }
});
$("#restartBtn").addEventListener("click", () => control("restart"));
subscribe(renderChrome);

// ----- start ------------------------------------------------------------------------

async function init() {
  await loadConfig();
  await Promise.all([loadOutputs(), poll()]);
  renderChrome(state);
  startRouter({ routes: ROUTES, outlet: $("#view"), nav: $("#tabs") });
  setInterval(poll, 1000);
}

init();
