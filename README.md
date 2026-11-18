# SpotifyConnectBluetoothSplitter

A Python app that plays the same music on **several Bluetooth speakers at once**,
using one of **Spotify's own clients** as the Spotify Connect device.

It has two ways to receive Spotify (set `receiver` in the config):

- **`soloist`** — Spotify's official headless Connect client (Windows or Linux).
  The app runs it, listens to what it plays on this PC and sends that to your
  speakers. See [Using Soloist](#using-soloist).
- **`headless`** — Spotify's *official desktop client* run headless on Linux
  x86_64 by the Rust tool in [`headless/`](headless/). The app reads the decoded
  PCM from the tool's stdout and the web UI handles the one-time sign-in. See
  [Official Spotify client on Linux](#official-spotify-client-on-linux-spotify-headless).

The app (`splitter/`) copies the sound to one buffer per speaker, keeps the
speakers aligned, and serves a web UI to control it. It does not speak Spotify's
protocol, decrypt or decode Spotify audio: the Spotify client does all of that
and we only route the sound it plays. See [Spotify's terms](#spotifys-terms).

```
Spotify app ──Connect──> official Spotify client ──sound──> FanOut ──┬──> buffer -> speaker 1
                         (Soloist or desktop client)                 ├──> buffer -> speaker 2
                                                                     └──> buffer -> ...
```

## Requirements

- Windows 10/11 (the code is cross-platform, but speaker matching is tuned for WASAPI)
- Python 3.10+
- A Spotify **Premium** account
- Bluetooth speakers already **paired and connected** in Windows Settings

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.json config.json
```

## Using Soloist

[Soloist](https://developer.spotify.com/documentation/soloist/tutorials/getting-started)
is Spotify's own headless Connect client. It plays on the Windows default
output; the app captures that with WASAPI loopback and sends it to the
speakers. Nothing is decrypted by this app: Spotify's client does the playback
and we route the sound, like any mixer app.

### 1. Get your Soloist API key

You need a **Spotify Premium** account. The key is tied to that account, and
Spotify says to keep it private and not share it. Soloist sends it to Spotify
when someone selects the device.

1. Go to the Spotify for Developers dashboard,
   <https://developer.spotify.com/dashboard>, and log in with your Spotify account.
2. Open the Soloist API Key page: <https://developer.spotify.com/dashboard/soloist>.
3. If you are asked, read and accept the
   [Spotify Terms and Conditions of Use](https://www.spotify.com/legal/end-user-agreement/).
4. Generate an API key. Spotify's documentation does not name the button, so
   follow what the page shows.
5. Copy the key and store it **in one of these two places** (the app checks them
   in this order):
   - the `SOLOIST_API_KEY` environment variable. In PowerShell, for the current
     window only: `$env:SOLOIST_API_KEY = "<your key>"`; or
   - a plain text file named `.soloist-api-key` next to `config.json`, holding
     only the key. Git ignores this file.

   Do not put the key in `config.json`, in a chat, in an issue or in a commit.
   If it leaks, generate a new one on the same page and replace it.

The source of these steps is Spotify's
[Getting started with Spotify Soloist](https://developer.spotify.com/documentation/soloist/tutorials/getting-started).

### 2. Get Soloist

Download the Windows build from Spotify's
[Downloads and updates](https://developer.spotify.com/documentation/soloist/reference/downloads-and-updates)
page. The file is `soloist_release_windows_x86_64.zip`. Spotify asks that you
link to that page instead of passing the file on, so it is not part of this
repository.

1. Extract the zip. It contains `soloist.exe`.
2. Put `soloist.exe` in `tools\soloist\` next to `config.json` (git ignores
   `tools\`), or set `soloist_path` to its location.
3. Check it runs: `tools\soloist\soloist.exe --version`.

Builds **expire 90 days** after their build date and then exit with code 10.
`--version` shows the build date. When it expires, download a newer build and
replace `soloist.exe`; the app tells you in the web UI when this happens.

### 3. Turn it on

1. In `config.json` set `"receiver": "soloist"` (or pick **Soloist** under
   *Advanced* in the web UI and save).
2. Start the app and press **Start**. The first time, Windows Firewall may ask
   whether `soloist.exe` may accept connections: allow it on private networks,
   or Spotify cannot find the device.
3. In the Spotify app on a phone or PC on the same network, open the device
   picker and choose **Bluetooth Splitter** (your `device_name`).

If it does not work, the web UI shows the reason (missing key, missing
`soloist.exe`, expired build). Soloist's own log is `.soloist-data\soloist.log`.

### 4. Keep the default output free

**The Windows default output must not be one of your speakers.** Soloist plays
on the default output and the app listens to it, so a speaker that is also the
default output would echo. The app skips such a speaker and says so. Pick
another default output in Windows Sound settings, or set only Soloist's output
under *Settings › System › Sound › Volume mixer*. When a Bluetooth speaker
connects, Windows may make it the default, so check this after connecting.

The capture runs at the default output's sample rate (often 48 kHz) and the
app follows it.

## Official Spotify client on Linux (spotify-headless)

[`headless/`](headless/) is a small Rust program that runs Spotify's **official
desktop client** on a headless Linux x86_64 board. The client does all the
decoding and DRM itself and shows up in Spotify's device list like any other
Connect device, so a phone can transfer playback to it. This tool only does the
wiring around it, the way a mixer app would:

```
phone ──Connect──> official Spotify client ──> virtual sink ──monitor──> parec ──PCM──> splitter
                   (on an Xvfb display)        (PULSE_SINK)               (stdout or FIFO)
```

- creates a virtual PulseAudio/PipeWire sink and removes it on exit,
- starts Xvfb (private auth cookie) and the Spotify client pinned to that
  display and sink,
- optionally writes the sink's audio as raw PCM (s16le, stereo, 44.1 kHz) to
  stdout or a FIFO,
- optionally runs x11vnc on the loopback, so you can sign in once,
- keeps everything running and restarts it with a backoff.

### Use it from the splitter

1. Build the tool and install Spotify, Xvfb, `pactl`/`parec` and `x11vnc`:

   ```bash
   sudo apt install xvfb pulseaudio-utils x11vnc
   cd headless && cargo build --release
   ```

2. In `config.json` set `"receiver": "headless"` (or pick **Spotify client**
   under *Advanced* in the web UI and save). The app finds the tool in
   `headless/target/release` next to the config, in `tools/spotify-headless`, or
   on the `PATH`.
3. Open the web UI. The **Spotify client setup** card checks that the tool,
   the Spotify client and a saved login are there. Press **Start setup**: the
   app starts the client on a virtual display with a VNC server that listens on
   this machine only. The card shows the exact SSH tunnel command (for example
   `ssh -L 5900:127.0.0.1:5900 you@board`); connect a VNC viewer to
   `localhost:5900`, sign in to Spotify in the window, then press **Finish
   setup**. The login is saved in `.spotify-headless-data` next to the config
   (it is a credential: git ignores it, do not share it).
4. Press **Start**. The app runs `spotify-headless run --pcm-out -`, reads the
   PCM (s16le, stereo, 44.1 kHz) from its stdout and fans it out to the
   speakers. In Spotify, choose this computer, listed under its host name (the
   client picks that name itself, so the *Device name* setting does not apply).

Setup and streaming cannot run at the same time (they share the client's
config folder): the app asks you to stop one before starting the other. If the
account has no password, set one on spotify.com first, because the client's
browser sign-in cannot open on a machine without a browser. The "saved login"
check is a best guess from the files the client writes.

Full instructions for the tool itself (options, a systemd unit) are in
[headless/README.md](headless/README.md).

**Status:** the logic is unit-tested (including the splitter side, with a stub
standing in for the tool) and the Linux code type-checks, but the whole chain
has not been run yet against a real Spotify client on Linux. The speaker
outputs on Linux are also untested: this app picks Bluetooth speakers through
PortAudio devices, which on Linux exposes ALSA devices and not each Bluetooth
speaker separately.

## Run

```bash
python -m splitter run --config config.json
```

Then:

1. Open **http://localhost:8080** and add your speakers from *Available devices*.
2. Press **Start**. The first time, Windows Firewall may ask whether Soloist may
   accept connections: allow it on private networks, or Spotify cannot find the
   device.
3. In Spotify (phone or PC, on the same network), open the device picker and
   choose **Bluetooth Splitter** (with the `headless` receiver: this computer's
   host name).


Options: `--host 0.0.0.0` and `--port 8080` override the web settings,
`--no-web` runs without the web UI, and `--name` overrides `device_name`.
`python -m splitter list` prints the audio outputs Windows reports.

## Web UI

The web UI is a single-page app. The Python backend serves one page and a JSON
API; the app itself runs in the browser and switches between its views without
reloading, using the URL hash, so every view can be bookmarked and reloaded:

- **Dashboard** (`#/`): the state of the splitter, throughput history and how
  many speakers are connected.
- **Speakers** (`#/speakers`): per-speaker volume, mute and delay, all live
  (nothing restarts, so Spotify keeps playing). The delay has -10/-1/+1/+10 ms
  buttons and an editable field. Press **Sync test** to play a click every
  second on every speaker for 10 seconds. Add speakers from the detected devices
  list, or remove them.
- **Settings** (`#/settings`): device name, buffer, speaker alignment,
  autostart and advanced options (the receiver and its paths). Changes are
  saved to `config.json`; the stream restarts if it is running. Leaving the view
  with unsaved changes asks first.
- **Spotify client** (`#/setup`, only with the `headless` receiver): checks what
  is installed and runs the one-time sign-in.

The header (state, Start/Stop, Restart) and the error banner stay on every view.

The server listens on `127.0.0.1` by default, so only this PC can reach it.
Setting `web_host` to `0.0.0.0` lets other devices on your network control it.
There is no login, so do that only on a trusted network. The page is served with
a strict Content-Security-Policy (scripts and styles only from this server, no
framing) and the API refuses state-changing requests that are not JSON, so other
websites cannot drive it.

### API

| Method | Path | Body |
| --- | --- | --- |
| GET | `/api/status` | Live state and per-speaker metrics |
| GET | `/api/outputs` | Audio devices visible to Windows |
| GET / PUT | `/api/config` | Read or replace the configuration (saved to disk) |
| POST | `/api/control` | `{"action": "start" \| "stop" \| "restart" \| "sync_test"}` |
| PATCH | `/api/speaker` | `{"match": "...", "volume"?, "muted"?, "delay_ms"?}` |
| GET | `/api/setup` | State of the Spotify client setup (`headless` receiver) |
| POST | `/api/setup` | `{"action": "start" \| "stop"}`: the one-time sign-in session |

Requests that change state must send `Content-Type: application/json`.

## Configuration

| Key | Meaning |
| --- | --- |
| `device_name` | Name shown in Spotify's device list |
| `receiver` | `soloist` or `headless` (official desktop client via spotify-headless, Linux). Default: `headless` on Linux, else `soloist`. The old `builtin` and `librespot` values were removed |
| `headless_path`, `headless_args` | The spotify-headless executable (also looked for in `headless/target/release` and `tools/spotify-headless`); extra flags for it |
| `spotify_bin` | The official Spotify executable that spotify-headless runs (default `spotify`) |
| `headless_data_dir` | Folder for the client's config (its saved login), cache and logs; default `.spotify-headless-data` next to the config |
| `headless_vnc_port` | Loopback port of the setup session's VNC server (default 5900) |
| `soloist_path` | Soloist executable (also looked for in `tools/soloist`); `soloist_args` adds flags |
| `soloist_api_key_file` | File holding the Soloist API key (default `.soloist-api-key`); `SOLOIST_API_KEY` wins |
| `buffer_seconds` | Largest per-speaker buffer before old audio is dropped |
| `auto_latency_compensation` | Delays faster speakers so all speakers line up |
| `autostart` | Start the device when the app launches |
| `web_host` / `web_port` | Web UI address (default `127.0.0.1:8080`) |
| `speakers[].match` | Case-insensitive part of the output device name (unique) |
| `speakers[].delay_ms` | Manual extra delay for one speaker (fine-tune by ear) |
| `speakers[].volume` | 0.0–1.0 per-speaker volume, set live from the UI |
| `speakers[].muted` | Mute state, set live from the UI |

Settings that are credentials (the Soloist key, the Spotify client's saved login) are never
stored in `config.json`. Streaming quality is set in the Spotify client itself. Keys from
earlier versions (`bitrate`, `librespot_*`, `cache_dir`) are ignored.

## How the sync works

- All speakers receive identical PCM (16-bit stereo; 44.1 kHz from the `headless`
  receiver, the default output's rate with Soloist).
- Each speaker's output runs on its own realtime stream fed from its own buffer.
- The sound arrives in real time from the Spotify client (captured from the
  default output with Soloist, or from the virtual sink with `headless`).
- Bluetooth latency differs per speaker. With `auto_latency_compensation`, the
  app reads each stream's reported latency and gives the faster speakers a head
  of silence. That lead is restored after every pause and gap in the stream.
  Reported latency for Bluetooth is often approximate, so if you hear an echo,
  adjust `delay_ms` for the speaker that is early.
- **Syncing by ear**: Windows reports the same latency for most Bluetooth
  speakers (for example 42.7 ms for two Sonos Move), so the real difference has
  to be tuned by ear. Press Sync test, and if the click sounds doubled, raise
  the delay of the speaker you hear first (start with 10 ms steps, then 1 ms)
  until it sounds like one click. The change is live and saved.
- A speaker can lead the source by at most 600 ms: if its clock runs slow, its
  oldest audio is dropped so it cannot drift far out of sync with the others.
- If a speaker falls behind, its oldest audio is dropped, and if it runs dry it
  plays silence. Other speakers are never blocked.
- A speaker that is off or unavailable at startup is skipped with a warning.

## Spotify's terms

- This app does **not** implement Spotify's protocol, decrypt, decode or
  otherwise get around Spotify's protections. Earlier versions had such
  receivers (a Python Connect receiver and librespot support); they were
  removed so that only Spotify's own clients remain.
- It runs a Spotify-made client (Soloist or the official desktop client) and
  routes the sound that client plays, the way a mixer app would. You need a
  Premium account, and you are bound by Spotify's
  [User Agreement](https://www.spotify.com/legal/end-user-agreement/) and, for
  Soloist, the terms you accept when you create its API key.
- Use it for your own listening. Do not record, redistribute or share what the
  speakers play.
- Running the desktop client unattended on a virtual display is not a setup that
  Spotify documents. This README is not legal advice: read the terms yourself
  and decide whether your use fits them.

## Known limitations

- **Soloist needs its own API key** and builds that expire after 90 days. The
  loopback capture this app uses for Soloist is Windows only; on Linux use the
  `headless` receiver.
- **The `headless` receiver is untested on a real Linux machine so far**, and the
  speaker outputs on Linux are untested too (see above).
- **Sync is approximate**, usually within tens of milliseconds, but Bluetooth
  does not expose exact timing. Expect a small echo on some speaker pairs.
- **Speaker drop-outs** on Bluetooth are not auto-reconnected; press Restart
  after reconnecting a speaker.

## Development

```bash
pip install pytest
pytest
```

The tests need no Spotify account, network or speakers. They cover the config,
the Soloist and spotify-headless sources (with stand-in programs), the engine,
the fan-out, the sync test and the web API.

- The web UI is plain HTML, CSS and ES modules, with **no build step and no
  dependencies**: `splitter/web/index.html` is the shell and
  `splitter/web/static/` holds the app. `app.js` is the entry (header, tabs,
  polling), `router.js` the hash router, `store.js` the shared state,
  `actions.js` every call to the backend, and `views/` has one module per view
  (`mount`, `update`, `unmount`). Files are read on each request, so edit and
  reload. Only `.js`, `.css`, `.svg`, `.png`, `.ico` and `.woff2` files are
  served from `static/`, and the Content-Security-Policy forbids inline scripts
  and `style=""` attributes (a test checks the views for the latter).
