// Spotify client setup (receiver "headless"): checks what is installed and runs the
// one-time sign-in session, a VNC server on this machine's loopback.

import { pollSetup, setupAction } from "../actions.js";
import { $, esc } from "../util.js";

const TEMPLATE = `
  <h1 class="view-title">Spotify client setup</h1>
  <p class="view-sub">The official Spotify client has to be signed in once before it appears in Spotify’s device list.</p>
  <div class="card" id="setupCard">
    <div class="checks" id="setupChecks"></div>
    <div id="setupGuide" class="guide hidden"></div>
    <p class="note hidden" id="setupError"></p>
    <button class="btn" id="setupBtn" disabled>Start setup</button>
    <p class="setup-note">Setup starts the client on a virtual display with a VNC server (this machine only) so you can sign in. The login is saved in the client data folder, so you will not need setup again. In Spotify, the device appears under this computer’s host name, not under the Device name in Settings.</p>
  </div>
  <div class="card empty hidden" id="notHeadless">
    <p class="strong">This receiver is not selected</p>
    <p>Choose <b>Spotify client</b> as the Spotify receiver under Advanced in <a href="#/settings">Settings</a>, and save.</p>
  </div>`;

let root = null;
let timer = null;

function checkRow(ok, label, detail) {
  const mark = ok === true ? ["ok", "✓"] : ok === false ? ["bad", "✕"] : ["unk", "•"];
  return `<div class="check"><span class="mark ${mark[0]}">${mark[1]}</span><div>${esc(label)}${detail ? `<small>${esc(detail)}</small>` : ""}</div></div>`;
}

function guideHTML(port) {
  const host = location.hostname;
  const local = host === "localhost" || host === "127.0.0.1" || host === "[::1]";
  const connect = local
    ? `<li>Open a VNC viewer and connect to <span class="code">localhost:${esc(port)}</span></li>`
    : `<li>On your own computer, open an SSH tunnel to this machine:<span class="code">ssh -L ${esc(port)}:127.0.0.1:${esc(port)} USER@${esc(host)}</span>
       then connect a VNC viewer to <span class="code">localhost:${esc(port)}</span></li>`;
  return `<ol>
    ${connect}
    <li>Sign in to Spotify in the window you see. If your account has no password (you normally use Google, Facebook or Apple), set one on spotify.com first: the client’s browser sign-in cannot open on a machine without a browser.</li>
    <li>When you are signed in, press <b>Finish setup</b>, then press <b>Start</b> at the top.</li>
  </ol>`;
}

export default {
  mount(outlet) {
    root = outlet;
    root.innerHTML = TEMPLATE;
    $("#setupBtn", root).addEventListener("click", async () => {
      const button = $("#setupBtn", root);
      const action = button.dataset.action || "start";
      button.disabled = true;
      await setupAction(action);
    });
    pollSetup();
    timer = setInterval(pollSetup, 1000);
  },

  update(state) {
    if (!root) return;
    const headless = state.config?.receiver === "headless";
    $("#setupCard", root).classList.toggle("hidden", !headless);
    $("#notHeadless", root).classList.toggle("hidden", headless);
    const setup = state.setup;
    if (!headless || !setup) return;

    $("#setupChecks", root).innerHTML = [
      checkRow(setup.supported, "Running on Linux", setup.supported ? "" : "The Spotify client receiver only runs on Linux."),
      checkRow(!!setup.tool_path, "spotify-headless tool", setup.tool_path || "Not found. Build it with “cargo build --release” in the headless folder, or set its path under Advanced."),
      checkRow(setup.spotify_found, "Spotify desktop client", setup.spotify_found ? "" : "Not found. Install Spotify, or set its path under Advanced."),
      checkRow(setup.login_data_found ? true : null, "Saved Spotify login",
        setup.login_data_found ? "Found in the client data folder." : "None found yet. Run the setup and sign in. (This check is a best guess.)"),
    ].join("");

    const guide = $("#setupGuide", root);
    guide.classList.toggle("hidden", !setup.running);
    if (setup.running) {
      const html = guideHTML(setup.vnc_port);
      if (guide.dataset.html !== html) {
        guide.innerHTML = html;
        guide.dataset.html = html;
      }
    }
    const error = $("#setupError", root);
    error.classList.toggle("hidden", !setup.error);
    error.textContent = setup.error || "";

    const streaming = state.status && state.status.state === "running";
    const button = $("#setupBtn", root);
    button.dataset.action = setup.running ? "stop" : "start";
    button.textContent = setup.running ? "Finish setup" : "Start setup";
    button.className = setup.running ? "btn danger" : "btn";
    button.disabled = !setup.running && (streaming || !setup.supported);
    button.title = !setup.running && streaming ? "Stop the splitter first" : "";
  },

  unmount() {
    clearInterval(timer);
    timer = null;
    root = null;
  },
};
