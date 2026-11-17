# SpotifyConnectBluetoothSplitter

A Python app that makes your PC a **Spotify Connect** device and plays the
same music on **several Bluetooth speakers at once**.

It has three ways to receive Spotify (set `receiver` in the config):

- **`soloist`** — Spotify's official headless Connect client. Works on every
  Premium account. The app runs it, listens to what it plays on this PC and
  sends that to your speakers. See [Using Soloist](#using-soloist).
- **`builtin`** — a Spotify Connect receiver written in Python (`spconnect/`).
  No extra install, but Spotify refuses it the audio keys on some accounts.
- **`librespot`** — an external librespot program.

The app itself (`splitter/`) copies the audio to one buffer per speaker, keeps
the speakers aligned, and serves a web UI to control it. The built-in receiver
is:

- **`spconnect/`** — a Spotify Connect receiver written in Python. It appears in
  Spotify's device list, takes play/pause/skip/seek/queue/volume commands, and
  downloads, decrypts and decodes the audio. No external program is needed.
- **`splitter/`** — the app: it copies the decoded audio to one buffer per
  speaker, keeps the speakers aligned, and serves a web UI to control it.

```
Spotify app ──Connect──> spconnect (receiver + decoder) ──PCM──> FanOut ──┬──> buffer -> speaker 1
                                                                          ├──> buffer -> speaker 2
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

## Run

```bash
python -m splitter run --config config.json
```

Then:

1. Open **http://localhost:8080** and add your speakers from *Available devices*.
2. Press **Start**. The first time, Windows Firewall asks whether Python may
   accept connections: allow it on private networks, or Spotify cannot find the
   device.
3. In Spotify (phone or PC, on the same network), open the device picker and
   choose **Bluetooth Splitter**.

After the first time, the app stays signed in: it saves a login token in
`.spconnect-state.json` next to the config file. Treat that file like a
password and do not share or commit it (it is in `.gitignore`).

Options: `--host 0.0.0.0` and `--port 8080` override the web settings,
`--no-web` runs without the web UI, and `--name` overrides `device_name`.
`python -m splitter list` prints the audio outputs Windows reports.

## Web UI

- **Control**: start, stop or restart the device; play, pause, next and previous.
- **Monitor**: what is playing and its position, throughput history, and per
  speaker latency, buffer level and underruns.
- **Speakers**: per-speaker volume, mute and delay, all live (nothing restarts,
  so Spotify keeps playing). The delay has -10/-1/+1/+10 ms buttons and an
  editable field. Press **Sync test** to play a click every second on every
  speaker for 10 seconds. Add speakers from the detected devices list, or remove them.
- **Settings**: device name, quality, buffer, speaker alignment, autostart and
  advanced options. Changes are saved to `config.json`; the stream restarts if
  it is running.

The UI is one file served by the app, with no external dependencies. The web
server listens on `127.0.0.1` by default, so only this PC can reach it. Setting
`web_host` to `0.0.0.0` lets other devices on your network control it. There is
no login, so do that only on a trusted network.

### API

| Method | Path | Body |
| --- | --- | --- |
| GET | `/api/status` | Live state, now playing and per-speaker metrics |
| GET | `/api/outputs` | Audio devices visible to Windows |
| GET / PUT | `/api/config` | Read or replace the configuration (saved to disk) |
| POST | `/api/control` | `{"action": "start" \| "stop" \| "restart" \| "play" \| "pause" \| "next" \| "prev"}` |
| PATCH | `/api/speaker` | `{"match": "...", "volume"?, "muted"?, "delay_ms"?}` |

Requests that change state must send `Content-Type: application/json`.

## Configuration

| Key | Meaning |
| --- | --- |
| `device_name` | Name shown in Spotify's device list |
| `receiver` | `soloist`, `builtin` (the Python receiver) or `librespot` (external program) |
| `soloist_path` | Soloist executable (also looked for in `tools/soloist`); `soloist_args` adds flags |
| `soloist_api_key_file` | File holding the Soloist API key (default `.soloist-api-key`); `SOLOIST_API_KEY` wins |
| `bitrate` | 96, 160 or 320 kbit/s |
| `buffer_seconds` | Largest per-speaker buffer before old audio is dropped |
| `auto_latency_compensation` | Delays faster speakers so all speakers line up |
| `autostart` | Start the device when the app launches |
| `web_host` / `web_port` | Web UI address (default `127.0.0.1:8080`) |
| `speakers[].match` | Case-insensitive part of the output device name (unique) |
| `speakers[].delay_ms` | Manual extra delay for one speaker (fine-tune by ear) |
| `speakers[].volume` | 0.0–1.0 per-speaker volume, set live from the UI |
| `speakers[].muted` | Mute state, set live from the UI |
| `librespot_path`, `librespot_args`, `cache_dir` | Only for `receiver: "librespot"` |

Settings that are credentials (the Soloist key, the saved Spotify login) are never
stored in `config.json`.

## How the sync works

- All speakers receive identical PCM (16-bit stereo; 44.1 kHz from the built-in
  receiver and librespot, the default output's rate with Soloist).
- Each speaker's output runs on its own realtime stream fed from its own buffer.
- The built-in receiver decodes only as fast as the speakers consume, keeping
  about a quarter of a second queued, so the stream cannot drift away from the
  outputs. With Soloist, the sound arrives in real time from the capture.
- Bluetooth latency differs per speaker. With `auto_latency_compensation`, the
  app reads each stream's reported latency and gives the faster speakers a head
  of silence. That lead is restored after every pause, seek and track change.
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

## The spconnect library

`spconnect` can be used on its own:

```python
from spconnect import Receiver, ReceiverConfig, NullSink

receiver = Receiver(ReceiverConfig(device_name="Living Room", state_path="state.json"), NullSink())
receiver.start()            # now visible in Spotify's device picker
...
receiver.stop()
```

Audio goes to a *sink*: any object with `write(pcm_bytes)`, `pending_ms()` and
`flush()`. `NullSink` discards it in real time.

What it implements: zeroconf discovery and login, the access point connection
(Diffie-Hellman handshake, Shannon cipher), client and access tokens, the dealer
websocket, Connect state (transfer, play, pause, seek, next/previous, queue,
shuffle, repeat, volume, autoplay), track metadata, audio keys, CDN download,
AES decryption, Ogg Vorbis decoding, loudness normalisation and gapless track
changes.

It is pure Python. Its dependencies are ordinary pip packages; three of them
ship compiled code (`cryptography` for AES, `soundfile` for Vorbis decoding,
`numpy`). There is no pure-Python Vorbis decoder fast enough to play in real time.

See [spconnect/NOTICE.md](spconnect/NOTICE.md) for the origin of the protocol
definitions and the licence of the code this is derived from.

## Known limitations

- **The built-in receiver can be refused.** Spotify has started withholding audio
  keys from the older access method that open-source players use, for some
  accounts (it wants its PlayPlay DRM). The log then shows
  `access point refused the audio key`. The same happens in librespot and
  go-librespot ([#1649](https://github.com/librespot-org/librespot/issues/1649),
  [#279](https://github.com/devgianlu/go-librespot/issues/279)). Use `soloist`.
- **Soloist needs a Windows PC (or a Linux machine) and its own API key.** It has
  no phone or Pi-specific limits, but this app's loopback capture is Windows only.

- **Sync is approximate**, usually within tens of milliseconds, but Bluetooth
  does not expose exact timing. Expect a small echo on some speaker pairs.
- **Speaker drop-outs** on Bluetooth are not auto-reconnected; press Restart
  after reconnecting a speaker.
- Podcasts in formats other than Ogg Vorbis or MP3, local files and videos are
  skipped. Jam sessions and the DJ are not supported.
- The receiver uses reverse-engineered protocols. Spotify can change them at any
  time, and using it may be against Spotify's terms of service.

## Development

```bash
pip install pytest
pytest
```

The tests need no Spotify account, network or speakers. They cover the cipher,
login blob handling, audio download/decrypt/decode, the Connect state machine,
the player, discovery, command handling, the fan-out and the web API.

- `scripts/gen_proto.py` regenerates `spconnect/proto/*_pb2.py` from a librespot
  checkout.
- The web UI is a single file, `splitter/web/index.html` (plain HTML, CSS and
  JS, no build step). The server reads it on each page load.
