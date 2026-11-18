// Settings: the saved configuration. Edits are only kept in the form until Save is
// pressed; the save bar shows while there is something to save.

import { saveConfig } from "../actions.js";
import { liveSpeakers, state as appState, update } from "../store.js";
import { $, $$ } from "../util.js";

const TEMPLATE = `
  <div class="group" id="settings">
    <div class="item">
      <div class="k">Device name<small>Shown in Spotify’s device list (Soloist only; the Spotify client uses this computer’s host name)</small></div>
      <input type="text" id="f_device_name" maxlength="60" autocomplete="off">
    </div>
    <div class="item">
      <div class="k">Buffer<small>More buffer survives Wi-Fi hiccups but adds delay</small></div>
      <input type="range" id="f_buffer_seconds" min="0.5" max="10" step="0.5">
      <span class="val" id="f_buffer_val">2.0 s</span>
    </div>
    <div class="item">
      <div class="k">Align speakers<small>Delay faster speakers so they play in sync</small></div>
      <label class="switch"><input type="checkbox" id="f_auto_latency"><span class="track"></span></label>
    </div>
    <div class="item">
      <div class="k">Start automatically<small>Begin streaming when the app launches</small></div>
      <label class="switch"><input type="checkbox" id="f_autostart"><span class="track"></span></label>
    </div>
    <details class="advanced">
      <summary>Advanced</summary>
      <div class="item">
        <div class="k">Spotify receiver<small>Spotify client = the official desktop client, run headless on Linux (set it up on the “Spotify client” tab). Soloist is Spotify’s official headless client; its sound is captured from this PC’s default output</small></div>
        <div class="seg" id="f_receiver">
          <button data-v="headless">Spotify client</button><button data-v="soloist">Soloist</button>
        </div>
      </div>
      <div class="item headless-only">
        <div class="k">Spotify client<small>The official Spotify executable (Linux)</small></div>
        <input type="text" id="f_spotify_bin" autocomplete="off" placeholder="spotify">
      </div>
      <div class="item headless-only">
        <div class="k">spotify-headless path<small>The Rust tool in headless/. Also looked for in headless/target/release next to the config</small></div>
        <input type="text" id="f_headless_path" autocomplete="off" placeholder="spotify-headless">
      </div>
      <div class="item headless-only">
        <div class="k">Client data folder<small>Holds the client’s saved login. Default: .spotify-headless-data next to the config</small></div>
        <input type="text" id="f_headless_data_dir" autocomplete="off" placeholder="(default)">
      </div>
      <div class="item headless-only">
        <div class="k">Setup VNC port<small>Loopback-only port of the one-time sign-in session</small></div>
        <input type="number" id="f_headless_vnc_port" min="1" max="65535">
      </div>
      <div class="item soloist-only">
        <div class="k">Soloist path<small>Looked for in tools/soloist next to the config; API key from SOLOIST_API_KEY or .soloist-api-key</small></div>
        <input type="text" id="f_soloist_path" autocomplete="off" placeholder="soloist">
      </div>
      <div class="item">
        <div class="k">Web address<small>Use 0.0.0.0 to allow other devices (no login)</small></div>
        <input type="text" id="f_web_host" autocomplete="off" placeholder="127.0.0.1">
      </div>
      <div class="item">
        <div class="k">Web port<small>Takes effect after restarting the app</small></div>
        <input type="number" id="f_web_port" min="1" max="65535">
      </div>
    </details>
  </div>
  <p class="setup-note">Changes to devices and the receiver apply when streaming restarts.</p>
  <div class="savebar" id="savebar">
    <div class="savebar-inner">
      <span>You have unsaved settings.</span>
      <button class="btn plain" id="discardBtn">Discard</button>
      <button class="btn" id="saveBtn">Save</button>
    </div>
  </div>`;

// Form field id -> config key, for the plain text/number/checkbox/range fields.
const FIELDS = {
  f_device_name: "device_name",
  f_buffer_seconds: "buffer_seconds",
  f_auto_latency: "auto_latency_compensation",
  f_autostart: "autostart",
  f_spotify_bin: "spotify_bin",
  f_headless_path: "headless_path",
  f_headless_data_dir: "headless_data_dir",
  f_headless_vnc_port: "headless_vnc_port",
  f_soloist_path: "soloist_path",
  f_web_host: "web_host",
  f_web_port: "web_port",
};

let root = null;
let receiver = "soloist";
let shownConfig = null; // the config object the form was last filled from

const field = (id) => $("#" + id, root);

function setDirty(value) {
  appState.dirty = value;
  $("#savebar", root)?.classList.toggle("show", value);
  window.onbeforeunload = value ? (e) => e.preventDefault() : null;
}

function showBuffer() {
  const input = field("f_buffer_seconds");
  $("#f_buffer_val", root).textContent = Number(input.value).toFixed(1) + " s";
  input.style.setProperty("--p", ((input.value - 0.5) / 9.5) * 100 + "%");
}

function setReceiver(value) {
  receiver = value;
  $$("#f_receiver button", root).forEach((b) => b.classList.toggle("active", b.dataset.v === value));
  $$(".headless-only", root).forEach((el) => el.classList.toggle("hidden", value !== "headless"));
  $$(".soloist-only", root).forEach((el) => el.classList.toggle("hidden", value !== "soloist"));
}

function fillForm(config) {
  shownConfig = config;
  for (const [id, key] of Object.entries(FIELDS)) {
    const input = field(id);
    const value = config[key];
    if (input.type === "checkbox") input.checked = !!value;
    else input.value = value ?? (key === "headless_vnc_port" ? 5900 : "");
  }
  showBuffer();
  setReceiver(config.receiver || "soloist");
  setDirty(false);
}

function readForm() {
  const text = (id, fallback) => field(id).value.trim() || fallback;
  return {
    ...appState.config,
    device_name: text("f_device_name", "Bluetooth Splitter"),
    receiver,
    buffer_seconds: Number(field("f_buffer_seconds").value),
    auto_latency_compensation: field("f_auto_latency").checked,
    autostart: field("f_autostart").checked,
    soloist_path: text("f_soloist_path", "soloist"),
    spotify_bin: text("f_spotify_bin", "spotify"),
    headless_path: text("f_headless_path", "spotify-headless"),
    headless_data_dir: text("f_headless_data_dir", null),
    headless_vnc_port: Number(field("f_headless_vnc_port").value) || 5900,
    web_host: text("f_web_host", "127.0.0.1"),
    web_port: Number(field("f_web_port").value) || 8080,
    speakers: liveSpeakers(), // live values, so a stale copy never overwrites a newer volume
  };
}

export default {
  mount(outlet) {
    root = outlet;
    shownConfig = null;
    root.innerHTML = TEMPLATE;
    for (const id of Object.keys(FIELDS)) {
      const input = field(id);
      input.addEventListener("input", () => {
        if (id === "f_buffer_seconds") showBuffer();
        setDirty(true);
      });
      input.addEventListener("change", () => setDirty(true));
    }
    $$("#f_receiver button", root).forEach((b) =>
      b.addEventListener("click", () => {
        setReceiver(b.dataset.v);
        setDirty(true);
      }),
    );
    $("#saveBtn", root).addEventListener("click", () => saveConfig(readForm()));
    $("#discardBtn", root).addEventListener("click", () => fillForm(appState.config));
  },

  update(state) {
    // Fill the form from the saved config, but never overwrite edits in progress.
    if (root && state.config && !state.dirty && state.config !== shownConfig) fillForm(state.config);
  },

  canLeave() {
    return !appState.dirty || confirm("You have unsaved settings. Leave without saving?");
  },

  unmount() {
    window.onbeforeunload = null;
    update({ dirty: false });
    root = null;
  },
};
