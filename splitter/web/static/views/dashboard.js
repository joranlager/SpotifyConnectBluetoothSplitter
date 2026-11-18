// Dashboard: what the splitter is doing right now.

import { HISTORY } from "../store.js";
import { $, fmtMB, fmtUptime } from "../util.js";

const TEMPLATE = `
  <div class="grid">
    <div class="card hero">
      <div class="hero-top">
        <div class="hero-title">
          <h1 id="deviceName">Bluetooth Splitter</h1>
          <p id="deviceHint"></p>
        </div>
        <div class="hero-actions">
          <span id="uptimeTag" class="pill outline">Not streaming</span>
        </div>
      </div>
      <div class="stats">
        <div class="stat"><div class="label">Throughput</div><div class="value" id="kbps">0 kbps</div></div>
        <div class="stat"><div class="label">Received</div><div class="value" id="received">0 MB</div></div>
        <div class="stat"><div class="label">Speakers</div><div class="value" id="speakerCount">0 / 0</div></div>
      </div>
      <svg id="spark" class="spark" viewBox="0 0 600 72" preserveAspectRatio="none" role="img" aria-label="Throughput history">
        <defs>
          <linearGradient id="sparkFill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0" stop-color="#0a84ff" stop-opacity="0.35"/>
            <stop offset="1" stop-color="#0a84ff" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <path id="sparkArea" fill="url(#sparkFill)" d=""/>
        <path id="sparkLine" fill="none" stroke="#0071e3" stroke-width="2" vector-effect="non-scaling-stroke" d=""/>
      </svg>
      <div class="spark-caption"><span>Last 60 seconds</span><span id="peak">peak 0 kbps</span></div>
    </div>
  </div>
  <div id="noSpeakers" class="card empty hidden">
    <p class="strong">No speakers yet</p>
    <p>Add your Bluetooth speakers on the <a href="#/speakers">Speakers</a> page.</p>
  </div>`;

let root = null;

function hint(status) {
  if (status.state !== "running") return "Press Start to make this device visible in Spotify";
  if (status.spotify && status.spotify.playing) return "Playing";
  if (status.receiver === "headless") {
    return "In Spotify, choose this computer (listed under its host name) as the device";
  }
  return `Open Spotify on this network and choose “${status.device_name}” as the device`;
}

function drawSpark(history) {
  const W = 600, H = 72, pad = 4;
  if (!history.length) return;
  const max = Math.max(320, ...history) * 1.1;
  const n = history.length;
  const points = history.map((v, i) => [
    n <= 1 ? W : (i / (HISTORY - 1)) * W,
    H - pad - (v / max) * (H - pad * 2),
  ]);
  const line = points.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const last = points[points.length - 1];
  $("#sparkLine", root).setAttribute("d", line);
  $("#sparkArea", root).setAttribute("d", `${line} L${last[0].toFixed(1)} ${H} L${points[0][0].toFixed(1)} ${H} Z`);
}

export default {
  mount(outlet) {
    root = outlet;
    root.innerHTML = TEMPLATE;
  },

  update(state) {
    const status = state.status;
    if (!status || !root) return;
    const connected = status.speakers.filter((s) => s.connected).length;
    $("#deviceName", root).textContent = status.device_name;
    $("#deviceHint", root).textContent = hint(status);
    $("#uptimeTag", root).textContent = status.state === "running" ? `Up ${fmtUptime(status.uptime_s)}` : "Not streaming";
    $("#kbps", root).textContent = `${Math.round(state.kbps)} kbps`;
    $("#received", root).textContent = fmtMB(status.bytes_in || 0);
    $("#speakerCount", root).textContent = `${connected} / ${status.speakers.length}`;
    $("#peak", root).textContent = `peak ${Math.round(state.peak)} kbps`;
    $("#noSpeakers", root).classList.toggle("hidden", status.speakers.length > 0);
    drawSpark(state.history);
  },

  unmount() {
    root = null;
  },
};
